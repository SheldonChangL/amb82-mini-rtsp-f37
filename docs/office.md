# Office 應用：人臉辨識 → 鎖定/解鎖 + 在場自動化

把 AMB82-mini 當桌面人臉感測器：認得你 → Mac/Linux 保持喚醒/解鎖；你離開 → 鎖屏；陌生臉 → 即鎖+存證。外加出勤、番茄鐘、偷看警告等一系列在場自動化。

## 架構：薄韌體 + 厚 daemon

```
[AMB82-mini]  SCRFD 偵測臉 → MobileFaceNet 辨識(認得是誰)
     │  UDP 廣播 255.255.255.255:48555（~3Hz / 變化時）
     │  {"dev":"amb82-office","ts":..,"faces":N,"known":["sheldon"],"unknown":M}
     ▼
[Mac/Linux daemon]  tools/office_daemon.py — 收事件，做全部 easy 功能
```

韌體只負責 AI + 廣播結果（改動極小、好維護）；所有行為邏輯在 Python daemon（好改好測、不用重燒）。

## 韌體：build + 燒錄

依賴 [F37 RTSP 修正](f37-rtsp.md)（先套那個 patch）。office 是額外一層。

```bash
cd ameba-rtos-pro2
git apply /path/to/patches/amb82-mini-f37-rtsp.patch     # 先：F37 base
git apply /path/to/patches/office-facelock.patch          # 再：office（UDP 事件 + face 掛勾）
```

**手動一步**（patch 不含此檔，因與各人的配網程式碼交纏）：把預設範例切成 face recognition。
編輯 `project/.../src/mmfv2_video_example/video_example_media_framework.c` 的 `example_mmf2_video_surport()`：

```c
// 把這行
mmf2_video_example_v1_init();
// 換成
mmf2_video_example_face_rtsp_init();
```

build（人臉辨識要 NN 模型 → target 用 **`flash_nn`**，產出 `flash_ntz.nn.bin`）：

```bash
cd project/realtek_amebapro2_v0_example/GCC-RELEASE
mkdir build && cd build
cmake .. -G"Unix Makefiles" -DCMAKE_TOOLCHAIN_FILE=../toolchain.cmake -DVIDEO_EXAMPLE=ON
cmake --build . --target flash_nn -j
# 燒錄 build/flash_ntz.nn.bin（同 F37 的 uartfwburn 流程）
```

> 預設打包的模型含 `scrfd320p`（偵測）+ `mobilefacenet_i8`（辨識），office 即用這兩個。

## 註冊你的臉（一次）

開 console（`tio /dev/cu.usbserial-XXX -b 115200`），對著鏡頭，輸入 AT 指令：

```
FREG sheldon     # 進註冊模式，名字叫 sheldon（對準鏡頭數秒）
FRFS             # 存到 flash（重開不掉）
FRRM             # 回到辨識模式
```

辨識到你時 RTSP 畫面會框綠色 + 名字；陌生臉框紅色 "unknown"。**daemon 的 `--owner` 要跟這裡的名字一致。**

## 跑 daemon（Mac / Linux）

```bash
python3 tools/office_daemon.py --owner sheldon          # 安全預設：只記錄、不會真的鎖（先看判斷對不對）
python3 tools/office_daemon.py --owner sheldon --arm    # 真的鎖/解鎖（確認 log 判斷正確後才加 --arm）
```

- 純 Python 標準庫，免裝 pip 套件。快照功能需要 `ffmpeg`（沒裝會略過、不會掛）。

### 桌面應用(GUI,首選)

`tools/amb82_office.py` 是完整桌面應用:在一個視窗裡 BLE 配網 + RTSP 預覽 + 在場監看 + **臉部鎖定控制台**。不用碰終端機:

```bash
python3 -m pip install PySide6 qasync bleak
python3 tools/amb82_office.py
```

控制台可直接操作:
- **啟用鎖定**主開關(預設關 = 只監看不鎖;打開才會真的鎖/解鎖)
- 主人名字、離開上鎖秒數、解鎖去抖秒數
- 各功能勾選(陌生臉、偷看、門鈴、出勤、番茄鐘、縮時、稽核、DnD)
- 即時狀態(在席/螢幕已鎖)、紅色 **立即停用 (DISARM)** 鈕

> 它與 `office_daemon.py` 共用同一套 PresenceEngine(冪等鎖、不會鎖死)。要無 GUI 常駐(Ubuntu server / 開機自動)才用 `office_daemon.py`。

### 純監看測試（GUI,舊版）

`tools/ble_wifi_tester.py`（PySide6 GUI）已內建 **Office 在場事件** 卡片：即時顯示 UDP 廣播的 `臉數/已辨識/陌生` + 狀態，並自動從廣播學到板子 IP 帶進 RTSP URL —— 搭配內建 RTSP 預覽，可同時看臉框(綠=已辨識/紅=unknown)與辨識結果，最適合一邊註冊臉一邊驗證。

```bash
python3 -m pip install PySide6 qasync bleak
python3 tools/ble_wifi_tester.py     # 看左下「Office 在場事件」卡 + 右側 RTSP 預覽
```

> GUI 與 `office_daemon.py` 都綁 UDP 48555 → 一次只能跑一個。GUI 用來「看」、daemon 用來「真的鎖」。

> 韌體已把 `SCRFD tick / FPS` 的 per-inference debug 從 `LOG_MSG` 降到 `LOG_INF`（預設不顯示），console 不再洗版。
- Linux 桌面通知需 `notify-send`。
- 所有可調參數 + 每個功能開關在檔案開頭的 `CONFIG` dict（繁中註解）。

### 防鎖死安全機制（重要）

presence-lock 的經典坑:鎖屏後你低頭打密碼時鏡頭認不到你 → 反覆鎖 → 永遠登不進去。本 daemon 已加三道防線:

1. **預設不武裝**:不加 `--arm` 時只記錄、不真的鎖。確認 log 判斷對了才加 `--arm`。
2. **冪等鎖定**:已鎖就不再鎖(`_screen_locked` 守門),所以就算辨識失敗,你也能**正常打密碼登入**——daemon 不會跟你搶。
3. **DISARM 失效保險**:`touch ~/amb82-office/DISARM` 立即停止所有鎖定(不必殺程式)。

**萬一被鎖死的救援**(SSH 進該機,順序:先停 daemon 再解鎖):

```bash
pkill -9 -f office_daemon.py            # 先停掉 daemon
pkill -f 'systemd-inhibit'              # 停掉 keep-awake
loginctl show-user $USER -p Display     # 查圖形 session id(印 Display=<sid>)
loginctl unlock-session <sid>           # 解鎖那個 session
```

### 鎖定/解鎖的真實能力

| 平台 | 鎖 | 解鎖 |
|---|---|---|
| **Linux** | `loginctl lock-session` ✅ | **`loginctl unlock-session` 真解鎖** ✅（需 systemd-logind + PolicyKit 允許）|
| **macOS** | `pmset displaysleepnow` + `Ctrl+Cmd+Q` ✅ | ⚠️ 第三方**無法繞密碼解鎖**。在場時用 `caffeinate` 保持不鎖 + 回來喚醒螢幕；要全自動則需自行關閉「喚醒需密碼」 |

精髓（macOS）：你在場時它**根本不鎖**，所以幾乎不用解鎖；離開才鎖，回來 Touch ID 一碰。

## 涵蓋的 easy 功能（CONFIG 內各自可開關）

| 功能 | 行為 |
|---|---|
| presence_lock | 主打：在場保持喚醒/解鎖、離開 N 秒鎖（含 hysteresis 防抖） |
| foreign_face_lock | 你不在 + 出現陌生臉 → 即鎖 + 快照 + 通知 |
| shoulder_surfer | 你在用 + 出現第二張臉 → 偷看警告 |
| visitor_doorbell | 有人走近（0→≥1 臉）→ 通知 + 快照 |
| attendance_log | 到離場寫 CSV，每日在座時數統計 |
| pomodoro | 連續在座 X 分鐘 → 提醒休息；離開暫停 |
| dnd_status | present / meeting(2人) / away → 呼叫自訂 shell hook（接 Slack/Teams） |
| timelapse | 在場時定時快照 → 縮時日記 |
| audit_photos | 每次鎖/解鎖/陌生臉都存證快照 |
| led(選配) | 推狀態回板子點 LED（指令通道，韌體端待實作） |

輸出檔在 `~/amb82-office/`（snapshots / audit / timelapse / attendance.csv）。

## UDP 事件格式

```json
{"dev":"amb82-office","ts":<開機毫秒>,"faces":<總臉數>,"known":["已辨識名字",...],"unknown":<陌生臉數>}
```

daemon 從封包來源學到板子 IP（用來抓 RTSP 快照、回送指令）。回送指令（未來）：UDP → `<板子IP>:48556` `{"cmd":"enroll","name":"x"}`。

## 已知限制 / 待辦

- 無活體偵測：一張你的照片可能騙過辨識（方便鎖 OK、非高安全）。可用 BLE proximity 當第二因子補強（見 [scenarios.md](scenarios.md)）。
- 韌體端的指令接收（enroll/LED over UDP :48556）尚未實作，目前只有 daemon→板子的送出 helper。
- macOS 無法真自動解鎖（見上表）。
- 尚未實機長時間驗證；先用 `--dry-run` 觀察事件與判斷是否正確再正式啟用。
