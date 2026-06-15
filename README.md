# AMB82-mini (RTL8735B) — macOS 原生 build + RTSP + BLE 配網

讓 Realtek **AMB82-mini (Ameba RTL8735B)** 的官方 standalone SDK 在 **macOS (Apple Silicon)** 原生編譯、RTSP 影像串流正常輸出，並加上**用 BLE 設定 Wi-Fi**(含測試工具與協定)的筆記與 patch。

功能總覽：

- **macOS 原生 build**（不需 Docker）
- **F37 sensor RTSP 串流修正**（VOE 無畫面根因）
- **BLE 配網(預設)**：開機無 Wi-Fi → 廣播 `Ameba_AMB82` → 手機/工具經 BLE 下發帳密 → 連上 → 存 flash → 重開跑 RTSP。協定見 [BLE-PROVISION-PROTOCOL.md](BLE-PROVISION-PROTOCOL.md),測試工具 [tools/ble-wifi-tester.html](tools/ble-wifi-tester.html)
- **QR 掃碼設定 Wi-Fi(備用)**：原本的相機掃碼配網;因板載定焦鏡頭近距離對不到焦、實機難用,改以 BLE 為主。程式仍在(build flag 切換)
- **狀態 LED（QR 用,可選,預設關閉）**

> ⚠️ **鏡頭對焦限制**:AMB82-mini 板載 F37 為定焦鏡頭,近距離 QR 容易糊 → 掃不出。故配網改用 BLE。

> ⚠️ 本 repo **只包含我自己的改動 (patch) 與筆記**，不含任何 Realtek SDK 原始碼或 binary。
> Realtek SDK 為專有授權 (proprietary)，請自行從官方取得：
> <https://github.com/Ameba-AIoT/ameba-rtos-pro2>

---

## 環境

- Mac (Apple Silicon, arm64)，macOS 26
- 不需要 Docker。Realtek 的 `V10.3.0-amebe-rtos-pro2` toolchain 有 darwin 版（x86_64，透過 Rosetta 2 執行）
- `cmake`（brew 安裝即可）
- AMB82-mini 板載 sensor 為 **JXF37P**（官方確認），對應 SDK 的 `SENSOR_F37`

## 遇到的問題

照官方流程 build + 燒錄後，板子開機正常、Wi-Fi 連得上、RTSP server 起得來、VLC 也連得上，但**完全沒有畫面**：

```
VOE command 0x206 fail ret 0x0
ch = 0 sf:0 df:0 l:0%          <- sent frame = 0
```

而且板子約每 30 秒被看門狗 (no video → sys_reset) 自動重開。

### 根因

打包進韌體的 sensor IQ / 校正資料清單，是來自 `project/realtek_amebapro2_v0_example/inc/sensor.h` 的 **`sen_id[]` 陣列**（`GENSNRLST` 工具讀它產生 `amebapro2_sensor_set.json`），**不是** `USE_SENSOR`。

預設 `sen_id[]` 只有 `DUMMY / GC2053 / GC4653 / GC4023 / SC2333`，**沒有 F37**。所以即使把 `USE_SENSOR` 設成 `SENSOR_F37`，韌體裡仍然沒有 F37 的 IQ 資料 → VOE 找不到對應 sensor 校正 → `VOE command 0x206 fail`、無畫面。

## 修正

見 [`amb82-mini-f37-rtsp.patch`](amb82-mini-f37-rtsp.patch)，核心是 `sensor.h`：

- `SENSOR_MAX` 5 → 6
- `sen_id[]` 加入 `SENSOR_F37`
- `manual_iq[]` 加入 `"iq_f37"`
- `USE_SENSOR` 改為 `SENSOR_F37`

另外兩處是除錯 FCS 時改的，**非必要**（`sensor.h` 的改動才是關鍵），列出供參考：

- `component/video/driver/RTL8735B/video_api.c`：`voe_boot_fsc_status()` 強制回傳 0（強制走正常 sensor 初始化）
- `component/video/driver/RTL8735B/video_user_boot.c`：channel 0 `.fcs = 0`

## BLE 配網(預設)

用 BLE 把 Wi-Fi 帳密下發給裝置,不靠相機(避開定焦鏡頭限制)。由 `platform_opts_bt.h` 的 `CONFIG_BT=1` + `CONFIG_BT_PERIPHERAL=1` 啟用(patch 已開)。

### 開機流程

```
1. 開機 → flash 有 Wi-Fi?
   ├─ 有 → 直接連 → 跑 RTSP(不開 BLE)
   └─ 無 → 啟動 BLE,廣播 "Ameba_AMB82"
2. 手機/工具經 BLE 寫入 SSID / PASS / CTRL=0x01
3. 韌體連線 → 透過 STATUS / IP characteristic 回報 → 存 flash
4. sys_reset() 重開 → 下次開機直接連 → 跑 RTSP
```

### 自訂 GATT service(16-bit UUID)

| 角色 | UUID | 屬性 |
|------|------|------|
| Service | `0xA100` | — |
| SSID | `0xA101` | Write |
| PASS | `0xA102` | Write |
| CTRL | `0xA103` | Write(`0x01`=連線) |
| STATUS | `0xA104` | Read/Notify(0x00~0x04) |
| IP | `0xA105` | Read/Notify(ASCII IPv4) |

完整協定(寫入順序、狀態碼、平台支援、Web Bluetooth 範例)見 **[BLE-PROVISION-PROTOCOL.md](BLE-PROVISION-PROTOCOL.md)**。

### 測試工具(Python 桌面程式)

**[tools/ble_wifi_tester.py](tools/ble_wifi_tester.py)** — `bleak` + `PySide6`:真正掃描/列裝置/連線(無瀏覽器選單)、深色 UI、**內嵌 RTSP 預覽**(QMediaPlayer)+ ffplay 後備。

```bash
python3 -m pip install PySide6 qasync bleak
cd tools && python3 ble_wifi_tester.py
```

流程:掃描 → 選 `Ameba_AMB82` → 連線 → 填 SSID/密碼 → 送出 → 看 STATUS;連上後自動帶出 `rtsp://<ip>:554`,按「內嵌播放」或「ffplay」看畫面。

> **macOS 藍牙權限**:第一次掃描會要求權限 —— 到「系統設定 → 隱私權與安全性 → 藍牙」把**終端機**打勾,否則掃不到裝置。
>
> 舊的 `tools/ble-wifi-tester.html`(Web Bluetooth)已淘汰:瀏覽器選裝置一定跳原生選單、且不能直接播 RTSP。

### 重設 Wi-Fi

console 打 **`QRCLR`** 清 flash Wi-Fi + 重開 → 重新進入 BLE 廣播配網。

> 韌體側:`wifi_prov_service.c`(自訂 GATT service + 連線/存檔/回報),`ble_app_main.c`(改廣播名 `Ameba_AMB82` + 註冊 service),開機由 `video_example_media_framework.c` 在無 Wi-Fi 時呼叫 `ble_app_init()`。

## QR 掃碼設定 Wi-Fi + 狀態 LED(備用)

> ⚠️ 因板載 **F37 定焦鏡頭**近距離對不到焦,QR 易糊掉解不出 → 實機難用,**已改以 BLE 為主**。
> 以下保留供參考;QR 程式仍在 tree,把 `CONFIG_BT` 關掉即回退此流程。

把原本「每次開機都要在 console 手打 `ATW0/ATW1/ATWC`」的設定方式，換成**用相機掃一張 Wi-Fi QR code**。

### 開機流程

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

### 狀態 LED（預設關閉）

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

### 相關檔案（都在 patch 內）

- `project/.../src/mmfv2_video_example/qr_wifi_provision.{c,h}`：掃描 + 解析 + 連線 + 存 flash + LED（新增）
- `video_example_media_framework.c`：開機流程接上 provisioning（取代手打 ATW 的等待）
- `video_example_media_framework.cmake`：加 `qr_code_scanner/inc` include path（`libqrcode.a` 本來就會連）

## 套用 + build + 燒錄 + 看影像

```bash
# 0. 取得官方 SDK 與 toolchain（自官方 repo / release）
git clone https://github.com/Ameba-AIoT/ameba-rtos-pro2.git
cd ameba-rtos-pro2

# 1. 套用本 patch
git apply /path/to/amb82-mini-f37-rtsp.patch

# 2. 下載並解壓 darwin toolchain
#    https://github.com/Ameba-AIoT/ameba-toolchain/releases/tag/V10.3.0-amebe-rtos-pro2
#    asdk-10.3.0-darwin-newlib-build-3659-x86_64.tar.bz2
export PATH=/path/to/asdk-10.3.0/darwin/newlib/bin:$PATH

# 3. build —— 重點：一定要加 -DVIDEO_EXAMPLE=ON，否則只編出沒有 app 的空殼
cd project/realtek_amebapro2_v0_example/GCC-RELEASE
mkdir build && cd build
cmake .. -G"Unix Makefiles" -DCMAKE_TOOLCHAIN_FILE=../toolchain.cmake -DVIDEO_EXAMPLE=ON
cmake --build . --target flash -j

# 產出 build/flash_ntz.bin
```

燒錄（在 macOS 原生跑，板子先進燒錄模式：按住 UART_DOWNLOAD → 點一下 RESET → 放開 UART_DOWNLOAD）：

```bash
cd ../../../../tools/Pro2_PG_tool\ _v1.4.3
./uartfwburn.darwin -p /dev/cu.usbserial-XXX \
  -f /path/to/build/flash_ntz.bin -b 2000000 -U -x 32 -r
```

設定 Wi-Fi（掃 QR）+ 看 log：

```bash
tio /dev/cu.usbserial-XXX -b 115200      # 或 screen ... 115200
# 首次開機會看到 [QR-PROV] scanning for a Wi-Fi QR code...
# 此時把手機的 Wi-Fi 分享 QR 對著鏡頭（LED 慢閃 = 掃描中）
# 掃到後 log 會出現 [QR-PROV] connected + saved, rebooting into RTSP...
# 板子自動重開，之後就直接連網（LED 恆亮），不需再掃。
```

> 想重新設定 Wi-Fi（換網路）/ 測試掃碼：在 console 打 **`QRCLR`** → 清 Wi-Fi 並重開進掃描模式
> （之後可由 BLE 下指令觸發同一動作，目前為第二階段尚未實作）。

連上後（重開後），用 VLC / ffplay 開（port 預設 554）：

```bash
ffplay -rtsp_transport tcp rtsp://<板子IP>:554
```

## 踩雷重點整理

1. **一定要 `-DVIDEO_EXAMPLE=ON`**：`scenario.cmake` 用此旗標選 example，不加就走 `else()` 完全不編 example，`app_example()` 是 weak 空函式 → 開機停在 `init_thread` 後沒有任何 video/sensor 訊息。
2. **sensor 要加進 `sen_id[]`**：只改 `USE_SENSOR` 不夠（見上方根因）。
3. **燒錄/看 log 不能同時開**：同一序列埠不能兩個程式佔用，否則燒錄會卡在 `Uart boot`。
4. 驗證韌體有沒有真的改到：加唯一字串 `printf`，build 後 `strings -a flash_ntz.bin | grep` 確認；`Build @` 時間戳不可靠。
5. **新增 `.c/.h` 要進 patch**：`git diff` 不含未追蹤檔，`update-patch.sh` 已改為先 `git add -N`（排除 `build/`）再 diff。新增檔還會讓 cmake 的 `file(GLOB ...)` 失效 → build 前要重跑 `cmake`（或砍掉 `build/` 重來）才會收到。

## 授權

本 repo 內容（patch + README）由我撰寫，可自由參考使用。
Realtek SDK、toolchain、binary 等均為 Realtek 所有，依其 [Disclaimer](https://github.com/Ameba-AIoT/ameba-rtos-pro2) 條款，本 repo 不重新散布。
