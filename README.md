# ACTi Auto HOI

ONVIF 連線、每秒 1 張快照、滑鼠框選定位、依 bbox 大小自動變焦，以及 60 秒後恢復原始 PTZ 位置的本機操作台。若上一張仍在傳輸，不會堆疊下一個請求。

## 執行

雙擊 `start.bat`，第一次會自動建立 Python 虛擬環境並安裝套件。瀏覽器會開啟：

`http://127.0.0.1:8087`

介面預設使用者為 `admin`、ONVIF port 為 `80`。請在本機輸入相機 IP 與密碼；密碼僅由瀏覽器送到本機程式，不寫入日誌或版本控制。

## 操作

1. 按「連接 Speed Dome」。
2. 在快照上按住滑鼠並框出範圍。
3. 後端優先使用 ONVIF `TranslationSpaceFov` 將框選中心移到畫面中央，持續檢查 `MoveStatus.PanTilt`；連續確認鏡頭停止後再固定等待 3 秒，最後送出不含 Pan/Tilt 的獨立 Zoom 指令。Zoom 依 bbox 比例計算，限制邊約占畫面 76%。
4. 60 秒後用事件前保存的 ONVIF AbsoluteMove 位置恢復；也能按「立即恢復原位」。

## 相容性備註

- 相機必須開啟 ONVIF，且帳號具 PTZ 權限。
- ACTi `center` CGI 使用 `http://IP/cgi-bin/com/ptz.cgi` 與 Digest Authentication。
- 若 ONVIF 使用非 80 port，請在畫面修改。
- 程式刻意在無法讀取原始 PTZ 位置時拒絕開始，避免 60 秒後無法恢復。

## 診斷紀錄

- 結構化操作紀錄：`logs/ptz.log`（5 MB 輪替，保留 3 份）
- 診斷快照：`logs/captures/<操作編號>_before.jpg`、`centered_before_zoom.jpg`、`after_zoom.jpg`
- 紀錄不包含密碼，並包含解析度、bbox、座標比例、ONVIF 位移及移動前後 PTZ 位置。
