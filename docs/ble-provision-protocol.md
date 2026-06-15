# AMB82-mini BLE Wi-Fi 配網協定 (v1)

讓手機 App 透過 BLE 把 Wi-Fi 帳密下發給 AMB82-mini (RTL8735B),裝置連上後存 flash 並啟動 RTSP。

> 測試工具:[`tools/ble_wifi_tester.py`](tools/ble_wifi_tester.py)(Python:`pip install PySide6 qasync bleak`)—— 真正掃描/連線 + 內嵌 RTSP 預覽。

---

## 1. 廣播 (Advertising)

- 裝置以 BLE peripheral 廣播,**名稱前綴 `Ameba_`**,完整為 `Ameba_XXYYZZ`(XXYYZZ = BT MAC 後三 bytes)。
- 廣播時機:**未設定過 Wi-Fi(或上次連線失敗)時**。已成功連上並啟動 RTSP 後,BLE 預設關閉以節省 coex 資源。
- App 端建議用 **name prefix `Ameba_`** 或 **service UUID** 過濾掃描結果。

## 2. GATT Service / Characteristics

Service UUID:`0000a100-0000-1000-8000-00805f9b34fb`

| 名稱 | UUID | 屬性 | 內容 |
|------|------|------|------|
| **SSID** | `0000a101-0000-1000-8000-00805f9b34fb` | Write | Wi-Fi SSID,UTF-8,**1–32 bytes** |
| **PASS** | `0000a102-0000-1000-8000-00805f9b34fb` | Write | 密碼,UTF-8,**0–64 bytes**(空 = 開放網路) |
| **CTRL** | `0000a103-0000-1000-8000-00805f9b34fb` | Write | 1 byte 指令,`0x01` = 開始連線 |
| **STATUS** | `0000a104-0000-1000-8000-00805f9b34fb` | Read / Notify | 1 byte 狀態碼(見下) |
| **IP** | `0000a105-0000-1000-8000-00805f9b34fb` | Read / Notify | 連上後的 IPv4,**ASCII 字串**(如 `192.168.62.48`);未連上為空 |

## 3. 配網流程 (App 端)

1. 連線後**先協商 MTU ≥ 100**(iOS/Android 預設通常已 185+,SSID/密碼才放得進單次寫入)。
2. 訂閱 **STATUS** 的 notify。
3. 寫 **SSID**(UTF-8 bytes)。
4. 寫 **PASS**(UTF-8 bytes;開放網路寫 0 byte)。
5. 寫 **CTRL** = `0x01` → 裝置開始連線。
6. 收 **STATUS** notify:
   - `0x02` → 成功(已取得 IP);裝置存 flash 後啟動 RTSP。此時可讀 **IP** characteristic 取得 IP，組出 `rtsp://<ip>:554`。
   - `0x04` → 密碼錯誤;可重輸入密碼後重寫 PASS + CTRL。
   - `0x03` → 失敗(找不到 AP / DHCP 失敗等);可重試。

> **RTSP 串流**:裝置連上後在 `rtsp://<ip>:554` 提供 H.264 串流(port 554)。瀏覽器無法直接播 RTSP，
> 需經 relay(如 `ffmpeg` 轉 HLS 或 go2rtc 轉 WebRTC);測試工具已內建 HLS 預覽流程。原生 App 可直接用 RTSP client 或改用裝置的 WebRTC/KVS 範例。

> **安全性自動判斷**:韌體依密碼長度決定 —— 有密碼 → WPA2-AES;無密碼 → 開放網路。
> (v1 不需 App 指定 security;未來如需 WEP / 指定加密,再加一個 byte 欄位。)

## 4. STATUS 狀態碼

| 值 | 意義 | App 建議顯示 |
|----|------|------|
| `0x00` | IDLE / 待命 | 灰 |
| `0x01` | CONNECTING / 連線中 | 黃,可顯示 spinner |
| `0x02` | CONNECTED / 已連上(取得 IP) | 綠,完成 |
| `0x03` | FAIL / 連線失敗 | 紅,可重試 |
| `0x04` | WRONG_PASSWORD / 密碼錯誤 | 橘,提示重輸密碼 |

## 5. 重新設定 Wi-Fi

裝置已連上後預設關 BLE。要重設:

- 目前:console 指令 **`QRCLR`** → 清 flash Wi-Fi + 重開 → 重新進入 BLE 廣播。
- (可選後續)做成「BLE 常駐」或「按鍵觸發」,讓 App 不需 console 也能重配。

## 6. Web Bluetooth 範例片段

```js
const SVC='0000a100-0000-1000-8000-00805f9b34fb';
const dev=await navigator.bluetooth.requestDevice({
  filters:[{namePrefix:'Ameba_'}], optionalServices:[SVC]
});
const s=await (await dev.gatt.connect()).getPrimaryService(SVC);
const ssid =await s.getCharacteristic('0000a101-0000-1000-8000-00805f9b34fb');
const pass =await s.getCharacteristic('0000a102-0000-1000-8000-00805f9b34fb');
const ctrl =await s.getCharacteristic('0000a103-0000-1000-8000-00805f9b34fb');
const stat =await s.getCharacteristic('0000a104-0000-1000-8000-00805f9b34fb');
await stat.startNotifications();
stat.addEventListener('characteristicvaluechanged',e=>console.log('STATUS',e.target.value.getUint8(0)));
const enc=new TextEncoder();
await ssid.writeValueWithResponse(enc.encode('MyWiFi'));
await pass.writeValueWithResponse(enc.encode('secret123'));
await ctrl.writeValueWithResponse(Uint8Array.of(0x01));
```

## 7. 平台支援(手機端注意)

| 平台 | 原生 BLE | Web Bluetooth |
|------|----------|---------------|
| Android | ✓ | ✓ Chrome |
| iOS | ✓(需原生 App,用 CoreBluetooth) | ✗ Safari/Chrome 不支援 |
| 桌機(測試) | — | ✓ Chrome / Edge(需 localhost 或 https) |

> iOS App 用 CoreBluetooth 照上表 UUID / 流程實作即可。
