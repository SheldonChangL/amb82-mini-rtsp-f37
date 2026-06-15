# 多應用架構：家庭 / 辦公室不同 AI（scenario 機制）

當「不同應用」差在 **AI 辨識邏輯/模型**（例：家裡偵測人/寵物、辦公室人臉辨識）時的擴展方式。重點：用 SDK 內建的 **scenario** 系統，**不改任何 Realtek 檔**。

## 分層：哪些共用、哪些分家

| 層 | 內容 | 處理方式 |
|----|------|---------|
| **Base（共用）** | F37 修正 + Wi-Fi/網路設定 | 同一套。Wi-Fi 用 [BLE 配網](ble-provisioning.md) 在 **runtime** 設，不分韌體 |
| **App（分家）** | NN 模型 + 偵測/告警邏輯 | 用 scenario 在 **build 時** 選，home / office 各一份韌體 |

## 機制：`-DSCENARIO=`

`application.cmake`（約 652–667 行）：有指定 `-DSCENARIO=xxx` 時載入 `scenario/<xxx>/scenario.cmake`，否則用預設 `scenario.cmake`。SDK 內附 `ai_glass / doorbell_chime / pir_sensor / tof_sensor` 即此模式的範例。自訂 scenario 全放 `project/.../scenario/` 底下。

```bash
cmake .. -DSCENARIO=home    -DCMAKE_TOOLCHAIN_FILE=../toolchain.cmake   # 家裡的模型+邏輯
cmake .. -DSCENARIO=office  -DCMAKE_TOOLCHAIN_FILE=../toolchain.cmake   # 辦公室的
```

每個 `scenario/<name>/scenario.cmake` 自己挑：要編入哪個 `model_*.c`（偵測後處理）、把哪些 NN 模型打包進 FWFS。

## NN 模型怎麼選 / 怎麼打包

- 模型 `.nb` 檔在 `project/.../src/test_model/model_nb/`
- **打包哪些模型**由 `GCC-RELEASE/mp/amebapro2_fwfs_nn_models.json` 的 `files` 陣列決定
- build target `flash_nn` → 產出 `flash_ntz.nn.bin`（含 NN 模型分區 `PT_NN_MDL`）
- runtime：`module_vipnn` 的 `CMD_VIPNN_SET_MODEL` 指定模型，從 FWFS（`NN_MDL/<model>.nb`）或 SD 卡載入

## 記憶體現實（決定架構的關鍵）

- NN 模型分區 **`PT_NN_MDL` 只有 ~5.88MB**（`0x920000`，長度 `0x5E0000`）
- 一個 fp16 偵測模型可能就 ~5MB → **塞不下多個大模型**
- INT8 變體小很多（`mobilefacenet_int8` 0.88MB vs int16 3.4MB）

三條路，**推薦第 1 條**：

1. **一份韌體一個用途**（home firmware / office firmware），各自只打包自己的模型 → 最乾淨，符合「不同地點燒不同韌體」
2. 一份韌體跑多模型 → 用 **INT8 模型**塞 2~3 個小的，runtime 切換（需加 ~50 行 `CMD_VIPNN_SWITCH_MODEL`，切換約 1~2 秒）
3. 模型放 **SD 卡** runtime 載入（SDK 有 VFS 路徑，範例預設關閉，需自行開啟 `nn_file_op.c` 的 SD 路徑）

> 單一 NN 硬體（`vip_lite`），多模型是**串行**執行（cascade：偵測→辨識），無法真正並行。

## 規劃中的 repo 結構

```
scenarios/
  home/    scenario.cmake + 你的偵測邏輯 + 指定模型
  office/  scenario.cmake + 你的偵測邏輯 + 指定模型
```

scenario 幾乎都是你**自己的程式碼** → 可直接放完整檔（不必 patch），版權單純。

> 狀態：架構已調查確認可行，scenario 骨架尚未建立。要動工前先決定 home / office 各自要辨識什麼，再依需求把骨架連模型選擇一起搭。
