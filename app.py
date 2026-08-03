from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
from flask import Flask, Response, jsonify, render_template, request
from onvif import ONVIFCamera
from requests.auth import HTTPDigestAuth


app = Flask(__name__)

# ACTi Z952 is a 25x optical dome. Keep a 12% margin on each limiting edge.
OPTICAL_ZOOM_FACTOR = 25.0
TARGET_BOX_FILL = 0.76

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
CAPTURE_DIR = LOG_DIR / "captures"
LOG_DIR.mkdir(exist_ok=True)
CAPTURE_DIR.mkdir(exist_ok=True)
ptz_logger = logging.getLogger("acti.ptz")
ptz_logger.setLevel(logging.INFO)
if not ptz_logger.handlers:
    handler = RotatingFileHandler(LOG_DIR / "ptz.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    ptz_logger.addHandler(handler)


def log_event(operation_id: str, event: str, **fields: Any) -> None:
    ptz_logger.info(json.dumps({"operationId": operation_id, "event": event, **fields}, ensure_ascii=False, default=str))


@dataclass
class CameraSession:
    ip: str
    username: str
    password: str
    port: int
    camera: Any
    media: Any
    ptz: Any
    profile: Any
    snapshot_uri: str
    original_position: Any = None
    restore_timer: threading.Timer | None = None
    restore_at: float | None = None
    busy: bool = False
    message: str = "已連線"


state_lock = threading.RLock()
camera_state: CameraSession | None = None


def auth_for(session: CameraSession) -> HTTPDigestAuth:
    return HTTPDigestAuth(session.username, session.password)


def normalized_snapshot_uri(uri: str, ip: str) -> str:
    parsed = urlparse(uri)
    if parsed.hostname in {"0.0.0.0", "127.0.0.1", "localhost", None}:
        port = f":{parsed.port}" if parsed.port else ""
        return urlunparse(parsed._replace(netloc=f"{ip}{port}"))
    return uri


def position_payload(position: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if position is not None and getattr(position, "PanTilt", None) is not None:
        payload["PanTilt"] = {
            "x": float(position.PanTilt.x),
            "y": float(position.PanTilt.y),
        }
    if position is not None and getattr(position, "Zoom", None) is not None:
        payload["Zoom"] = {"x": float(position.Zoom.x)}
    return payload


def absolute_move(session: CameraSession, position: dict[str, Any]) -> None:
    req = session.ptz.create_type("AbsoluteMove")
    req.ProfileToken = session.profile.token
    req.Position = position
    session.ptz.AbsoluteMove(req)


def absolute_zoom(session: CameraSession, zoom: float) -> None:
    """Send a zoom-only move so the settled Pan/Tilt position is not rewritten."""
    req = session.ptz.create_type("AbsoluteMove")
    req.ProfileToken = session.profile.token
    req.Position = {"Zoom": {"x": zoom}}
    session.ptz.AbsoluteMove(req)


def onvif_center(session: CameraSession, x: int, y: int, width: int, height: int, options: Any, operation_id: str) -> bool:
    """Center an image point using ONVIF's field-of-view relative translation space."""
    spaces = getattr(options, "Spaces", None)
    relative_spaces = getattr(spaces, "RelativePanTiltTranslationSpace", None) if spaces else None
    fov_space = next((s for s in (relative_spaces or []) if str(s.URI).endswith("TranslationSpaceFov")), None)
    if fov_space is None:
        log_event(operation_id, "center_space_missing")
        return False

    # ONVIF FOV coordinates span -1..+1 across the complete image dimension.
    dx = max(float(fov_space.XRange.Min), min(float(fov_space.XRange.Max), 2.0 * x / width - 1.0))
    dy = max(float(fov_space.YRange.Min), min(float(fov_space.YRange.Max), 1.0 - 2.0 * y / height))
    log_event(operation_id, "center_translation", centerX=x, centerY=y, imageWidth=width,
              imageHeight=height, dx=dx, dy=dy, space=str(fov_space.URI),
              xRange=[float(fov_space.XRange.Min), float(fov_space.XRange.Max)],
              yRange=[float(fov_space.YRange.Min), float(fov_space.YRange.Max)])
    if abs(dx) < 0.002 and abs(dy) < 0.002:
        return True
    req = session.ptz.create_type("RelativeMove")
    req.ProfileToken = session.profile.token
    req.Translation = {"PanTilt": {"x": dx, "y": dy, "space": str(fov_space.URI)}}
    session.ptz.RelativeMove(req)
    return True


def wait_pan_tilt_stopped(session: CameraSession, operation_id: str, timeout: float = 15.0) -> Any:
    """Wait until the dome has really finished centering before zooming."""
    deadline = time.monotonic() + timeout
    idle_samples = 0
    last_status = None
    # Let the camera accept the move before an initial status read can report stale IDLE.
    time.sleep(0.35)
    while time.monotonic() < deadline:
        last_status = session.ptz.GetStatus({"ProfileToken": session.profile.token})
        move_status = getattr(last_status, "MoveStatus", None)
        pan_tilt = str(getattr(move_status, "PanTilt", "")).upper()
        log_event(operation_id, "move_status", panTilt=pan_tilt,
                  position=position_payload(getattr(last_status, "Position", None)))
        if pan_tilt.endswith("IDLE"):
            idle_samples += 1
            if idle_samples >= 3:
                return last_status
        else:
            idle_samples = 0
        time.sleep(0.25)
    raise TimeoutError("等待 Speed Dome 移至框選中心逾時，未執行 Zoom")


def save_diagnostic_snapshot(session: CameraSession, operation_id: str, stage: str) -> None:
    try:
        result = requests.get(session.snapshot_uri, auth=auth_for(session), timeout=6, verify=False)
        result.raise_for_status()
        path = CAPTURE_DIR / f"{operation_id}_{stage}.jpg"
        path.write_bytes(result.content)
        log_event(operation_id, "snapshot_saved", stage=stage, path=str(path), bytes=len(result.content))
    except Exception as exc:
        log_event(operation_id, "snapshot_failed", stage=stage, error=str(exc))


def bbox_target_zoom(
    current_zoom: float,
    zoom_min: float,
    zoom_max: float,
    box_width: int,
    box_height: int,
    image_width: int,
    image_height: int,
) -> tuple[float, float]:
    """Convert bbox occupancy into a normalized optical zoom position."""
    occupancy = max(box_width / image_width, box_height / image_height)
    required_scale = max(1.0, TARGET_BOX_FILL / max(occupancy, 0.001))
    normalized = max(0.0, min(1.0, (current_zoom - zoom_min) / max(zoom_max - zoom_min, 1e-9)))
    current_factor = 1.0 + normalized * (OPTICAL_ZOOM_FACTOR - 1.0)
    target_factor = min(OPTICAL_ZOOM_FACTOR, current_factor * required_scale)
    target_normalized = (target_factor - 1.0) / (OPTICAL_ZOOM_FACTOR - 1.0)
    target_zoom = zoom_min + target_normalized * (zoom_max - zoom_min)
    return target_zoom, target_factor


def restore_camera(expected: CameraSession | None = None) -> None:
    global camera_state
    with state_lock:
        session = camera_state
        if session is None or (expected is not None and session is not expected):
            return
        position = copy.deepcopy(session.original_position)
        session.restore_timer = None
        session.restore_at = None
        session.message = "正在恢復原始位置"
    try:
        if not position:
            raise RuntimeError("沒有可用的原始 PTZ 位置")
        absolute_move(session, position)
        message = "已恢復原始位置"
    except Exception as exc:
        message = f"恢復失敗：{exc}"
    with state_lock:
        if camera_state is session:
            session.busy = False
            session.original_position = None
            session.message = message


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.post("/api/connect")
def connect() -> Response:
    global camera_state
    data = request.get_json(force=True)
    ip = str(data.get("ip", "")).strip()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    port = int(data.get("port", 80))
    if not ip or not username:
        return jsonify(ok=False, error="IP 與使用者名稱不可空白"), 400
    try:
        camera = ONVIFCamera(ip, port, username, password)
        media = camera.create_media_service()
        profiles = media.GetProfiles()
        if not profiles:
            raise RuntimeError("相機沒有 ONVIF Media Profile")
        profile = profiles[0]
        snapshot = media.GetSnapshotUri({"ProfileToken": profile.token})
        snapshot_uri = normalized_snapshot_uri(snapshot.Uri, ip)
        ptz = camera.create_ptz_service()
        ptz.GetStatus({"ProfileToken": profile.token})
        new_session = CameraSession(ip, username, password, port, camera, media, ptz, profile, snapshot_uri)
        with state_lock:
            old = camera_state
            if old and old.restore_timer:
                old.restore_timer.cancel()
            camera_state = new_session
        return jsonify(ok=True, message="ONVIF 連線成功", snapshotUri=snapshot_uri)
    except Exception as exc:
        return jsonify(ok=False, error=f"ONVIF 連線失敗：{exc}"), 502


@app.get("/api/snapshot")
def snapshot() -> Response:
    with state_lock:
        session = camera_state
    if session is None:
        return jsonify(ok=False, error="尚未連線"), 409
    try:
        # ACTi cameras commonly expose ONVIF snapshots through a self-signed HTTPS endpoint.
        result = requests.get(session.snapshot_uri, auth=auth_for(session), timeout=5, verify=False)
        result.raise_for_status()
        return Response(result.content, content_type=result.headers.get("Content-Type", "image/jpeg"), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return jsonify(ok=False, error=f"快照取得失敗：{exc}"), 502


@app.post("/api/focus")
def focus() -> Response:
    data = request.get_json(force=True)
    operation_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    with state_lock:
        session = camera_state
        if session is None:
            return jsonify(ok=False, error="尚未連線"), 409
        if session.busy:
            return jsonify(ok=False, error="目前正在倒數；請先恢復原位"), 409
        session.busy = True
        session.message = "正在對準框選區域"
    original = None
    try:
        width = max(1, int(data["imageWidth"]))
        height = max(1, int(data["imageHeight"]))
        box_width = max(1, min(width, int(data["boxWidth"])))
        box_height = max(1, min(height, int(data["boxHeight"])))
        x = max(0, min(width, int(data["centerX"])))
        y = max(0, min(height, int(data["centerY"])))

        status = session.ptz.GetStatus({"ProfileToken": session.profile.token})
        original = position_payload(status.Position)
        if not original:
            raise RuntimeError("相機未回傳目前 PTZ 位置，無法保證恢復")
        with state_lock:
            session.original_position = copy.deepcopy(original)
        log_event(operation_id, "focus_start", cameraIp=session.ip, imageWidth=width, imageHeight=height,
                  boxWidth=box_width, boxHeight=box_height, centerX=x, centerY=y,
                  client=data.get("client", {}), originalPosition=original)
        save_diagnostic_snapshot(session, operation_id, "before")

        options = session.ptz.GetConfigurationOptions({"ConfigurationToken": session.profile.PTZConfiguration.token})
        centered_by_onvif = onvif_center(session, x, y, width, height, options, operation_id)
        center_url = f"http://{session.ip}/cgi-bin/com/ptz.cgi"
        if not centered_by_onvif:
            center_result = requests.get(
                center_url,
                params={"center": f"{x},{y}", "imagewidth": width, "imageheight": height, "stream": "h264"},
                auth=auth_for(session), timeout=8,
            )
            center_result.raise_for_status()
        with state_lock:
            session.message = "正在等待 Speed Dome 對準框選中心"
        centered = wait_pan_tilt_stopped(session, operation_id)
        target = position_payload(centered.Position)
        log_event(operation_id, "center_idle", centeredPosition=target)
        if "PanTilt" not in target and "PanTilt" in original:
            target["PanTilt"] = original["PanTilt"]

        with state_lock:
            session.message = "中心定位完成，等待 3 秒後開始 Zoom"
        time.sleep(3.0)
        settled = session.ptz.GetStatus({"ProfileToken": session.profile.token})
        target = position_payload(settled.Position)
        log_event(operation_id, "center_settled_after_3s", settledPosition=target)
        save_diagnostic_snapshot(session, operation_id, "centered_before_zoom")

        spaces = getattr(options, "Spaces", None)
        zoom_spaces = getattr(spaces, "AbsoluteZoomPositionSpace", None) if spaces else None
        if zoom_spaces:
            with state_lock:
                session.message = "中心定位完成，正在依框選範圍調整倍率"
            zoom_min = float(zoom_spaces[0].XRange.Min)
            zoom_max = float(zoom_spaces[0].XRange.Max)
            current_zoom = float(target.get("Zoom", {}).get("x", zoom_min))
            target_zoom, target_factor = bbox_target_zoom(
                current_zoom, zoom_min, zoom_max, box_width, box_height, width, height
            )
            log_event(operation_id, "zoom_command", currentZoom=current_zoom, targetZoom=target_zoom,
                      targetFactor=target_factor, boxWidth=box_width, boxHeight=box_height)
            absolute_zoom(session, target_zoom)
        else:
            raise RuntimeError("相機未提供 ONVIF 絕對 Zoom 範圍，無法依 bbox 計算倍率")

        with state_lock:
            if camera_state is not session:
                raise RuntimeError("連線已被更換")
            session.original_position = original
            session.restore_at = time.time() + 60
            session.message = f"已置中並調整至約 {target_factor:.1f}×，60 秒後恢復"
            timer = threading.Timer(60, restore_camera, args=(session,))
            timer.daemon = True
            session.restore_timer = timer
            timer.start()
        time.sleep(0.5)
        save_diagnostic_snapshot(session, operation_id, "after_zoom")
        return jsonify(ok=True, message=session.message, restoreIn=60, operationId=operation_id)
    except Exception as exc:
        log_event(operation_id, "focus_failed", error=str(exc))
        if original:
            try:
                absolute_move(session, original)
            except Exception:
                pass
        with state_lock:
            if camera_state is session:
                session.busy = False
                session.original_position = None
                session.message = f"操作失敗：{exc}"
        return jsonify(ok=False, error=f"PTZ 操作失敗：{exc}"), 502


@app.post("/api/restore")
def restore() -> Response:
    with state_lock:
        session = camera_state
        if session is None:
            return jsonify(ok=False, error="尚未連線"), 409
        if session.restore_timer:
            session.restore_timer.cancel()
            session.restore_timer = None
    restore_camera(session)
    with state_lock:
        ok = camera_state is session and not session.busy
        message = session.message
    return jsonify(ok=ok, message=message), (200 if ok else 502)


@app.get("/api/status")
def status() -> Response:
    with state_lock:
        session = camera_state
        if session is None:
            return jsonify(connected=False, busy=False, remaining=0, message="尚未連線")
        remaining = max(0, int((session.restore_at or 0) - time.time() + 0.999)) if session.restore_at else 0
        return jsonify(connected=True, busy=session.busy, remaining=remaining, message=session.message, ip=session.ip)


@app.get("/api/diagnostics/latest")
def latest_diagnostics() -> Response:
    path = LOG_DIR / "ptz.log"
    if not path.exists():
        return jsonify(ok=True, operationId=None, events=[])
    parsed: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]:
        if " INFO " not in line:
            continue
        try:
            parsed.append(json.loads(line.split(" INFO ", 1)[1]))
        except json.JSONDecodeError:
            continue
    if not parsed:
        return jsonify(ok=True, operationId=None, events=[])
    operation_id = parsed[-1].get("operationId")
    return jsonify(ok=True, operationId=operation_id,
                   events=[item for item in parsed if item.get("operationId") == operation_id])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8087, threaded=True)
