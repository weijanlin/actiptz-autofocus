# ACTi PTZ AutoFocus

以 ONVIF 控制 Speed Dome 的本機 Web 操作台。使用者在快照上框選目標後，系統會先把 bbox 的下邊中心點移至畫面中央，再依 bbox 大小計算光學變焦，並於 60 秒後恢復原始 PTZ 位置。

> ONVIF-based PTZ camera controller for bounding-box targeting, proportional optical zoom, timed position restoration, and diagnostics.

## 功能

- 使用 IP、ONVIF port、帳號及密碼連接攝影機
- 每秒取得一張 HTTPS Snapshot，不堆疊尚未完成的請求
- 在影像上拖曳建立 bounding box
- 使用 bbox 下邊中心點作為 Pan/Tilt 定位基準
- 等待 Pan/Tilt 停止，再額外等待 3 秒後執行 Zoom
- 依 bbox 佔畫面的比例計算 Zoom，保留畫面邊界
- 60 秒後透過 ONVIF `AbsoluteMove` 恢復原始位置
- 支援手動立即恢復
- 顯示 bbox、FOV 位移及移動前後 PTZ 座標
- 保存操作前、置中後及變焦後的診斷快照

## 運作流程

```text
連接 ONVIF 相機
  → 取得 Snapshot 與目前 PTZ 位置
  → 使用者框選 bbox
  → 將 bbox 下邊中心點換算成 ONVIF TranslationSpaceFov
  → 執行 Pan/Tilt RelativeMove
  → 等待 MoveStatus.PanTilt = IDLE
  → 再等待 3 秒
  → 依 bbox 大小執行 Zoom-only AbsoluteMove
  → 倒數 60 秒
  → 恢復原始 Pan/Tilt/Zoom
```

## 系統需求

- Windows 10/11
- Python 3.10 以上
- 支援 ONVIF Media 與 PTZ 的 Speed Dome
- 相機帳號需具備 Media、PTZ 與 Snapshot 權限

主要 Python 套件：

- Flask
- requests
- onvif-zeep

## 安裝與啟動

### Windows 快速啟動

雙擊：

```text
start.bat
```

第一次啟動會建立 `.venv` 並安裝 `requirements.txt`。瀏覽器會開啟：

```text
http://127.0.0.1:8087
```

### 手動啟動

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## 操作方式

1. 輸入相機 IP、ONVIF port、使用者名稱與密碼。
2. 按下「連接 Speed Dome」。
3. 等待快照畫面出現。
4. 在畫面上拖曳框選目標。
5. 黃色圓點代表實際使用的 bbox 下邊中心定位點。
6. 相機完成置中後會依 bbox 大小調整 Zoom。
7. 60 秒後自動恢復；也可按「立即恢復原位」。

## Zoom 計算

目前針對 ACTi Z952 的 25× 光學變焦範圍換算。系統以 bbox 寬高占畫面的最大比例計算需要倍率，目標限制邊約占畫面 76%，保留約 12% 邊界，避免目標貼邊或遭裁切。

ONVIF 對外提供的是連續的標準化 Zoom 位置 `0.0–1.0`，不是固定 25 個檔位。

## 定位診斷

網頁左側「定位診斷」會顯示：

- 原始影像解析度
- bbox 左上及右下座標
- bbox 下邊中心定位點
- ONVIF FOV `dx`、`dy`
- 移動前 PTZ 座標
- Pan/Tilt 停止時的 PTZ 座標
- 等待 3 秒後的 PTZ 座標
- Zoom 起點與目標值

本機診斷檔案：

```text
logs/ptz.log
logs/captures/<operation-id>_before.jpg
logs/captures/<operation-id>_centered_before_zoom.jpg
logs/captures/<operation-id>_after_zoom.jpg
```

`ptz.log` 最大 5 MB，保留三份輪替檔案。`logs/` 已加入 `.gitignore`。

## 相容性與限制

- 目前主要以 ACTi Z952 進行測試。
- 不同品牌對 `TranslationSpaceFov` 的方向與比例可能不同，需要校正。
- 相機 Snapshot API 的回應速度會限制實際更新率；需要高 FPS 時應改用 RTSP 持續解碼。
- 若相機無法回傳原始 PTZ 位置，系統會拒絕開始操作，避免無法復位。
- 部分舊 ACTi 機型可使用 `/cgi-bin/com/ptz.cgi`；支援 ONVIF FOV 的機型會優先使用 ONVIF。
- 相機重新連線會取消前一個連線的倒數計時器。

## 安全注意事項

- 不要把攝影機密碼、真實公網 IP 或診斷影像提交到 Git。
- 本專案不會將密碼寫入 log，但密碼會由瀏覽器傳送到本機 Flask 服務。
- Web 服務預設監聽所有本機介面；請使用防火牆限制未授權存取。
- 不建議將 ONVIF 或攝影機管理介面直接暴露於公網。

## Project structure

```text
actiptz-autofocus/
├── app.py
├── requirements.txt
├── start.bat
├── templates/
│   └── index.html
└── static/
    ├── app.css
    ├── app.js
    └── diagnostics.css
```

## License

尚未指定開源授權。若要公開供他人使用，建議依需求加入 MIT、Apache-2.0 或其他適合的 License。
