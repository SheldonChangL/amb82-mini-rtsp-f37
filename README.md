# AMB82-mini (RTL8735B) — macOS 原生 build + RTSP 影像修正

讓 Realtek **AMB82-mini (Ameba RTL8735B)** 的官方 standalone SDK 在 **macOS (Apple Silicon)** 原生編譯，並讓 RTSP 影像串流正常輸出的筆記與 patch。

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

看 log + 連 Wi-Fi（console 指令）：

```bash
tio /dev/cu.usbserial-XXX -b 115200      # 或 screen ... 115200
# 在 $8735b> 後輸入：
#   ATW0=你的SSID
#   ATW1=你的密碼
#   ATWC
```

連上後，用 VLC / ffplay 開（port 預設 554）：

```bash
ffplay -rtsp_transport tcp rtsp://<板子IP>:554
```

## 踩雷重點整理

1. **一定要 `-DVIDEO_EXAMPLE=ON`**：`scenario.cmake` 用此旗標選 example，不加就走 `else()` 完全不編 example，`app_example()` 是 weak 空函式 → 開機停在 `init_thread` 後沒有任何 video/sensor 訊息。
2. **sensor 要加進 `sen_id[]`**：只改 `USE_SENSOR` 不夠（見上方根因）。
3. **燒錄/看 log 不能同時開**：同一序列埠不能兩個程式佔用，否則燒錄會卡在 `Uart boot`。
4. 驗證韌體有沒有真的改到：加唯一字串 `printf`，build 後 `strings -a flash_ntz.bin | grep` 確認；`Build @` 時間戳不可靠。

## 授權

本 repo 內容（patch + README）由我撰寫，可自由參考使用。
Realtek SDK、toolchain、binary 等均為 Realtek 所有，依其 [Disclaimer](https://github.com/Ameba-AIoT/ameba-rtos-pro2) 條款，本 repo 不重新散布。
