# QR 掃碼設定 Wi-Fi + 狀態 LED（備用）

> ⚠️ 因板載 **F37 定焦鏡頭**近距離對不到焦，QR 易糊掉解不出 → 實機難用，**已改以 [BLE 配網](ble-provisioning.md) 為主**。
> 以下保留供參考；QR 程式仍在 tree，把 `CONFIG_BT` 關掉即回退此流程。

把原本「每次開機都要在 console 手打 `ATW0/ATW1/ATWC`」的設定方式，換成**用相機掃一張 Wi-Fi QR code**。

## 開機流程

```
1. 開機（patch 讓 voe_boot_fsc_status() 回 0，固定走此流程）
2. flash 有沒有存過的 Wi-Fi？
   ├─ 有 → 等 Wi-Fi 自動連上（不主動連、不掃描）→ 跑 RTSP
   └─ 無 → 進入「自動持續掃描」模式
3. 把手機的 Wi-Fi QR 對著鏡頭（手機分享 Wi-Fi 產生的 QR 即可）
4. 掃到 → 連上 → 存 flash → 自動 sys_reset() 重開
5. 重開後 flash 已有 Wi-Fi → 自動連 → 跑 RTSP
```

> **關鍵**：有憑證時只「被動等待」Wi-Fi stack 自己連上（`qr_prov_connect_saved()`），
> **不可**自己再 `wifi_connect()` —— 否則會跟開機時 stack 正在進行的連線打架
> （`there is ongoing wifi connect`），失敗後誤入掃描模式 → RTSP 起不來。
>
> 那次自動重開**一輩子只發生一次**（首次設定）。之後每次開機都直接連、不掃描。
> 掃描（NV12）與 RTSP（H264）永遠在不同次開機 → 避開 video 重複 `video_init()`。

**重設 Wi-Fi / 測試掃碼**：在 console 打 **`QRCLR`** → 清掉 flash 的 Wi-Fi（只清 `FAST_RECONNECT_DATA` 那個 sector）並重開 → 進掃描模式。
（重燒韌體**不會**清掉 Wi-Fi，因為它存在獨立的 system-data 區，不在韌體分割區。）

**掃描畫質**：掃描用 **1080p**（F37 原生，QR 格子像素最多，對小 QR / 摩爾紋最有利），掃描失敗時 console 會印原因：
- `no QR found` → 對焦/距離/沒對準（定焦糊掉或 QR 太小）
- `QR found but decode failed` → 畫質太差（摩爾紋/反光）→ 改用**印出來的 QR**或把螢幕稍微傾斜最有效

Wi-Fi QR 格式即手機/網路工具通用格式：
`WIFI:S:<SSID>;T:<WPA|WEP|nopass>;P:<password>;H:<true|false>;;`

## 狀態 LED（預設關閉）

LED 預設 **關閉**（`qr_wifi_provision.c` 開頭 `QR_LED_ENABLE 0`），因為腳位必須是該板**空閒的 GPIO**。
**注意：`PA_5`（Arduino D13）在 AMB82-mini 不是空閒的**（log 會出現 `Pin 0[5] is conflicted ... using by peripheral F00`）。

要啟用：把 `QR_LED_ENABLE` 改成 `1`，`QR_LED_PIN` 設成一支確認空閒的 GPIO，外接 `<pin> → 330Ω → LED → GND`（active-high）。
判斷腳位有沒有被佔用：燒錄後看 log 有沒有那行 `Pin ... is conflicted`，沒有就是空閒的。

啟用後燈號：

| 狀態 | LED |
|------|-----|
| 掃描中（等 QR） | 慢閃（~0.8s 週期） |
| 掃到、連線中 | 快閃（~0.24s 週期） |
| 連上（即將重開 / RTSP 上線） | 恆亮 |
| 連線失敗（繼續掃描） | 雙閃 |

不論 LED 開不開，console 都會印 `[QR-PROV] ...` 狀態訊息，可從序列埠看進度。

## 相關檔案

- `project/.../src/mmfv2_video_example/qr_wifi_provision.{c,h}`：掃描 + 解析 + 連線 + 存 flash + LED（新增）
- `video_example_media_framework.c`：開機流程接上 provisioning（取代手打 ATW 的等待）
- `video_example_media_framework.cmake`：加 `qr_code_scanner/inc` include path（`libqrcode.a` 本來就會連）
