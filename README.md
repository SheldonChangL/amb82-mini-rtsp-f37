# AMB82-mini (RTL8735B) — macOS build + RTSP + 配網筆記

讓 Realtek **AMB82-mini (Ameba RTL8735B)** 的官方 standalone SDK 在 **macOS (Apple Silicon)** 原生編譯、RTSP 影像正常輸出，並加上 Wi-Fi 配網的筆記與 patch。

> ⚠️ 本 repo **只含我自己的改動（patch）與筆記**，不含任何 Realtek SDK 原始碼或 binary。
> Realtek SDK 為專有授權，請自行從官方取得：<https://github.com/Ameba-AIoT/ameba-rtos-pro2>

## 文件索引

| 主題 | 說明 |
|------|------|
| [docs/f37-rtsp.md](docs/f37-rtsp.md) | **核心**：macOS 原生 build、F37 sensor RTSP 無畫面的根因與修正、build/燒錄/看影像、踩雷整理 |
| [docs/office.md](docs/office.md) | **Office 應用**：人臉辨識 → 鎖定/解鎖(Linux 真解鎖 / macOS presence-hold) + 在場自動化(出勤/番茄鐘/陌生臉/偷看…)；薄韌體 UDP + Python daemon |
| [docs/gesture.md](docs/gesture.md) | **手勢控制**：揮手 → 鍵盤方向鍵(簡報翻頁);patch `gesture-keys.patch` + daemon/GUI 的 GestureKeys |
| [docs/mqtt.md](docs/mqtt.md) | **MQTT**(跨網段/多機/通知):UDP→MQTT 橋接 + daemon `--mqtt` + 通知到 Mac |
| [docs/ble-provisioning.md](docs/ble-provisioning.md) | **BLE 配網（預設）**：開機無 Wi-Fi → 廣播 `Ameba_AMB82` → 經 BLE 下發帳密；含 Python 測試工具 |
| [docs/ble-provision-protocol.md](docs/ble-provision-protocol.md) | BLE 配網的完整 GATT 協定（UUID、寫入順序、狀態碼、Web Bluetooth 範例） |
| [docs/qr-provisioning.md](docs/qr-provisioning.md) | QR 掃碼配網 + 狀態 LED（備用；因定焦鏡頭近距離難對焦已退為次選） |
| [docs/scenarios.md](docs/scenarios.md) | 多應用架構：家庭/辦公室不同 AI 的 scenario 機制與記憶體限制（規劃） |

## 快速開始

1. 取得官方 SDK 與 darwin toolchain
2. 套用 [`patches/amb82-mini-f37-rtsp.patch`](patches/amb82-mini-f37-rtsp.patch)
3. `cmake .. -DVIDEO_EXAMPLE=ON ...` → `cmake --build . --target flash -j`
4. 燒錄 → 配網 → `ffplay rtsp://<板子IP>:554`

完整步驟見 [docs/f37-rtsp.md](docs/f37-rtsp.md)。

## 桌面應用 / 工具

- **[`tools/amb82_office.py`](tools/amb82_office.py)** — **完整桌面應用**(PySide6):BLE 配網 + RTSP 預覽 + 在場監看 + **臉部鎖定控制台**(GUI 勾選啟用、調參數、即時狀態、DISARM)。重用 office_daemon 的引擎。**一般使用首選這個。**
- [`tools/office_daemon.py`](tools/office_daemon.py)：無 GUI 的常駐版(Ubuntu server / 開機自動跑),純 stdlib;支援 **MQTT 模式**(`--mqtt`)同時控制多台機器,與桌面 app 共用同一套 PresenceEngine
- **MQTT(多機/通知)**:[`tools/udp_mqtt_bridge.py`](tools/udp_mqtt_bridge.py)(Linux 上把板子 UDP 轉 MQTT)+ [`tools/mqtt_notify.py`](tools/mqtt_notify.py)(發通知到 Mac);設定見 [docs/mqtt.md](docs/mqtt.md)
- [`tools/ble_wifi_tester.py`](tools/ble_wifi_tester.py)：早期精簡版(僅 BLE 配網 + RTSP),已被 amb82_office 取代
- **[`tools/build_firmware.py`](tools/build_firmware.py)** — **功能模式選擇 build**:`python3 build_firmware.py <office|gesture|rtsp>` 一鍵切換(自動設好 active 範例 + FWFS NN 模型清單,避免漏改)→ build flash_nn → 印出 bin 與燒錄指令。新增功能在 `MODES` 加一筆即可。
- [`update-patch.sh`](update-patch.sh)：重新產生 F37 patch 並 push
- patch：[`patches/office-facelock.patch`](patches/office-facelock.patch)（office 韌體:UDP 事件廣播 + face 範例掛勾）

## 授權

本 repo 內容（patch + 筆記 + 工具）由我撰寫，可自由參考使用。
Realtek SDK、toolchain、binary 等均為 Realtek 所有，依其 [Disclaimer](https://github.com/Ameba-AIoT/ameba-rtos-pro2) 條款，本 repo 不重新散布。
