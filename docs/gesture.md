# 手勢控制：揮手 → 鍵盤方向鍵

用板子的手部偵測,左右揮手 → 主機注入 ←/→ 方向鍵(簡報翻頁、媒體上一首/下一首)。和臉部在場/鎖定共用同一條 UDP 廣播管道,host 端靠 `dev` 欄位區分。

## 架構

```
板子(vipnn_handgesture 範例:手掌偵測 → 判斷 left/right 揮動)
   │  UDP 廣播 :48555  {"dev":"amb82-gesture","gesture":"left"|"right"}
   ▼
host(office_daemon.py 的 GestureKeys / 桌面 app 的手勢開關)→ 注入 ←/→
```

- 在席事件是 `dev":"amb82-office"`、手勢是 `dev":"amb82-gesture"` → 同一個 :48555 廣播,host 端分流。
- 韌體端:`gesture_detect.c`(由手掌中心位移判斷揮動方向)、`gesture_event.c`(UDP 廣播,仿 `office_event.c`),掛在 `mmf2_video_example_vipnn_handgesture_init.c`。

## 韌體 build

手勢用的是 handgesture(palm + 21 點)NN 範例,需 NN 模型 → `flash_nn`。

```bash
cd ameba-rtos-pro2
git apply /path/to/patches/gesture-keys.patch     # 手勢韌體(自寫檔 + handgesture init 掛勾)
# 1) active 範例設為 handgesture(video_example_media_framework.c 的 example_mmf2_video_surport):
#      mmf2_video_example_vipnn_handgesture_init();
# 2) ★關鍵★ 把手勢模型列進 FWFS 打包清單,否則開機會 nbg bad / vipnn not applied:
#    編 GCC-RELEASE/mp/amebapro2_fwfs_nn_models.json 的 "files" 改成:
#      "files":[ "palm_detection_lite_int16", "hand_landmark_lite_int16" ]
cd project/realtek_amebapro2_v0_example/GCC-RELEASE
mkdir build && cd build
cmake .. -G"Unix Makefiles" -DCMAKE_TOOLCHAIN_FILE=../toolchain.cmake -DVIDEO_EXAMPLE=ON
cmake --build . --target flash_nn -j
# 產出 build/flash_ntz.nn.bin,照常 uartfwburn 燒錄
```

> **NN 分區只有 5.88MB,模型放不下全部** —— palm(2.5MB)+hand(2.0MB)=4.4MB 剛好;但臉部要的 scrfd+mobilefacenet 加上 yolo 等就爆掉。所以 **FWFS 打包清單是「手勢」或「臉鎖」二選一**:
> - 手勢模式:`"files":["palm_detection_lite_int16","hand_landmark_lite_int16"]`
> - 臉鎖模式:`"files":[...,"scrfd320p","mobilefacenet_i8"]`
>
> 切換模式時要同時改(a)active 範例 (b)這個 files 清單,再重 build `flash_nn`。手勢與臉部辨識是不同 NN 範例(單 NPU 一次一個),無法同一顆韌體並用。

## host 端

桌面 app `amb82_office.py`:臉部鎖定卡有「**手勢控制方向鍵**」勾選(預設開)。或無 GUI 跑 daemon —— 它會自動分流手勢事件。

`office_daemon.py` 的 `CONFIG`:
- `GESTURE_TO_KEYS`(預設 True):總開關,關閉只記 log 不送鍵
- `GESTURE_DEBOUNCE_SEC`(預設 0.3):同方向去抖,避免連發/心跳灌爆按鍵
- `GESTURE_DEV`(預設 `amb82-gesture`):手勢事件的 dev 欄位

按鍵注入:macOS 用 osascript(key code 123/124)、Linux 用 `loginctl`-同層的方式(見 `GestureKeys.press_arrow`)。失效保險(DISARM 檔)與鎖屏一致。

## 事件格式

```json
{"dev":"amb82-gesture","ts":<ms>,"gesture":"left"}   // 或 "right"
```
