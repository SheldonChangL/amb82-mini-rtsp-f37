# BLE 配網（預設）

用 BLE 把 Wi-Fi 帳密下發給裝置，不靠相機（避開定焦鏡頭限制）。由 `platform_opts_bt.h` 的 `CONFIG_BT=1` + `CONFIG_BT_PERIPHERAL=1` 啟用。

> ⚠️ **鏡頭對焦限制**：AMB82-mini 板載 F37 為定焦鏡頭，近距離 QR 容易糊 → 掃不出。故配網改用 BLE 為主，[QR 配網](qr-provisioning.md) 退為備用。

## 開機流程

```
1. 開機 → flash 有 Wi-Fi?
   ├─ 有 → 直接連 → 跑 RTSP(不開 BLE)
   └─ 無 → 啟動 BLE,廣播 "Ameba_AMB82"
2. 手機/工具經 BLE 寫入 SSID / PASS / CTRL=0x01
3. 韌體連線 → 透過 STATUS / IP characteristic 回報 → 存 flash
4. sys_reset() 重開 → 下次開機直接連 → 跑 RTSP
```

## 自訂 GATT service（16-bit UUID）

| 角色 | UUID | 屬性 |
|------|------|------|
| Service | `0xA100` | — |
| SSID | `0xA101` | Write |
| PASS | `0xA102` | Write |
| CTRL | `0xA103` | Write(`0x01`=連線) |
| STATUS | `0xA104` | Read/Notify(0x00~0x04) |
| IP | `0xA105` | Read/Notify(ASCII IPv4) |

完整協定（寫入順序、狀態碼、平台支援、Web Bluetooth 範例）見 **[ble-provision-protocol.md](ble-provision-protocol.md)**。

## 測試工具（Python 桌面程式）

**[../tools/ble_wifi_tester.py](../tools/ble_wifi_tester.py)** — `bleak` + `PySide6`：真正掃描/列裝置/連線（無瀏覽器選單）、深色 UI、**內嵌 RTSP 預覽**（QMediaPlayer）+ ffplay 後備。

```bash
python3 -m pip install PySide6 qasync bleak
cd tools && python3 ble_wifi_tester.py
```

流程：掃描 → 選 `Ameba_AMB82` → 連線 → 填 SSID/密碼 → 送出 → 看 STATUS；連上後自動帶出 `rtsp://<ip>:554`，按「內嵌播放」或「ffplay」看畫面。

> **macOS 藍牙權限**：第一次掃描會要求權限 —— 到「系統設定 → 隱私權與安全性 → 藍牙」把**終端機**打勾，否則掃不到裝置。
>
> 舊的 [`../tools/ble-wifi-tester.html`](../tools/ble-wifi-tester.html)（Web Bluetooth）已淘汰：瀏覽器選裝置一定跳原生選單、且不能直接播 RTSP。

## 重設 Wi-Fi

console 打 **`QRCLR`** 清 flash Wi-Fi + 重開 → 重新進入 BLE 廣播配網。

> 韌體側：`wifi_prov_service.c`（自訂 GATT service + 連線/存檔/回報），`ble_app_main.c`（改廣播名 `Ameba_AMB82` + 註冊 service），開機由 `video_example_media_framework.c` 在無 Wi-Fi 時呼叫 `ble_app_init()`。
