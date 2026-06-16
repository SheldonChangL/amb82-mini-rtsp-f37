# AMB82-mini 辦公室在席系統 · MQTT 模式

把單機的 UDP 在席系統升級成「一台相機 → 多台主機」的架構,**完全不改韌體**。

```
  AMB82-mini 板子
        │  UDP 廣播 255.255.255.255:48555
        ▼
  常開 Linux 機
  ├── mosquitto broker          (MQTT 1883)
  └── udp_mqtt_bridge.py         UDP → MQTT 橋接 + MQTT → UDP 指令回送
        │  MQTT  amb82/office/presence
        ├──────────────► Linux daemon (office_daemon.py --mqtt …)  鎖/解鎖自己
        └──────────────► Mac   daemon (office_daemon.py --mqtt …)  鎖/解鎖自己 + 桌面通知
```

板子照舊只會 UDP 廣播。橋接程式收廣播後補上相機來源 IP(`ip` 欄位)再 publish
到 MQTT;各台 daemon 訂閱同一個在席 topic,依**同一台相機**的在席狀態各自鎖/
解鎖自己的螢幕。

---

## 1. 在 Linux 裝並開啟 mosquitto

```bash
sudo apt install mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto
```

預設 mosquitto 只聽 localhost。要讓區網其他機器(Mac / 另一台 Linux)連得到,
新增一個最小設定檔讓它聽所有介面:

```bash
sudo tee /etc/mosquitto/conf.d/local.conf >/dev/null <<'EOF'
listener 1883 0.0.0.0
allow_anonymous true
EOF
sudo systemctl restart mosquitto
```

> ⚠️ `allow_anonymous true` 只適用**可信任的內網**。若網段不可信,請改加帳密
> (見最後的安全性說明)。

確認 broker 正常(在 Linux 上開兩個終端機):

```bash
mosquitto_sub -h localhost -t 'amb82/#' -v        # 終端 A:訂閱全部
mosquitto_pub -h localhost -t 'amb82/notify' -m hi # 終端 B:發一則,A 應收到
```

---

## 2. 在會跑 bridge / daemon 的機器裝 paho-mqtt

bridge、以及 MQTT 模式的 daemon 都需要 paho-mqtt(UDP 模式不需要):

```bash
pip install paho-mqtt
```

Linux(跑 bridge)與 Mac/Linux(跑 daemon 的每一台)都裝。

---

## 3. 在 Linux 跑 bridge

```bash
python3 tools/udp_mqtt_bridge.py --broker localhost
```

它會:綁 UDP 48555 收板子廣播 → 補 `ip` 欄位 → publish `amb82/office/presence`;
同時訂閱 `amb82/office/cmd`,把 MQTT 指令以 UDP 轉送回最近看到的板子 IP:48556。
broker 斷線會自動重連,壞封包不會弄垮它。

可調參數:`--broker --port --topic-prefix --udp-port --cmd-udp-port`。

---

## 4. 在 Linux 與 Mac 各跑 daemon(MQTT 模式)

兩台都這樣跑(把 `<linux_ip>` 換成跑 broker 那台的 IP):

```bash
python3 tools/office_daemon.py --mqtt <linux_ip> --owner sheldon
```

**先不要加 `--arm`**,觀察 log 確認在席判斷正確(誰來、誰走、何時鎖/解鎖)後,
再加 `--arm` 才會真的鎖/解鎖螢幕:

```bash
python3 tools/office_daemon.py --mqtt <linux_ip> --owner sheldon --arm
```

兩台都會依**同一台相機**回報的在席狀態,各自鎖/解鎖自己的螢幕。`--dry-run`、
`--owner`、`--arm` 等行為與 UDP 模式完全相同。MQTT 模式相關參數:
`--mqtt HOST --mqtt-port --mqtt-topic --notify-topic`。

> macOS 解鎖限制不變:第三方程式無法繞過登入密碼,daemon 只能喚醒螢幕。要做到
> 「人到即用」需自行到系統設定關閉喚醒密碼。鎖屏與睡眠則完全正常。

---

## 5. 發桌面通知到 Mac

Mac daemon 在 MQTT 模式會訂閱 `amb82/notify` 與 `amb82/notify/mac`。從任一台用
小工具推一則通知:

```bash
python3 tools/mqtt_notify.py --broker <linux_ip> --target mac "下班囉"
```

`--target` 可選 `mac` / `linux` / `all`(預設 `all`)。

---

## 6. Topic 一覽

| Topic                   | 方向                  | 用途 |
|-------------------------|-----------------------|------|
| `amb82/office/presence` | bridge → daemon       | 在席事件(含相機 `ip` 欄位) |
| `amb82/office/cmd`      | daemon → bridge → 板子 | enroll / led 指令(轉成 UDP 回送) |
| `amb82/notify`          | mqtt_notify → 全部 daemon | 廣播桌面通知(所有平台) |
| `amb82/notify/mac`      | mqtt_notify → Mac daemon  | 只給 Mac 的桌面通知 |
| `amb82/notify/linux`    | mqtt_notify → Linux daemon | 只給 Linux 的桌面通知 |

在席事件 JSON 範例(bridge 補上 `ip` 後):

```json
{"dev":"amb82-office","ts":1718500000000,"faces":1,
 "known":["sheldon"],"unknown":0,"ip":"192.168.1.50"}
```

---

## 為什麼用橋接,而不是改韌體?

- **韌體零改動、零風險**:板子原本就會 UDP 廣播;不必重燒、不會把已驗證的韌體弄壞。
- **一對多**:UDP 廣播一台機器收完就沒了;經 MQTT 後,任意數量的 Linux/Mac
  都能同時訂閱同一台相機的在席狀態,各自鎖/解鎖自己。
- **跨網段 / 跨子網**:UDP 廣播出不了區網;MQTT 是 TCP 點對點,可跨子網甚至遠端
  (搭 TLS)使用。
- **解耦**:通知、指令回送、未來其他訂閱者都掛在 broker 上,彼此互不影響。
- bridge 同時把 MQTT 指令以 UDP 轉回板子(`:48556`),所以 enroll/led 也走得通。

## 安全性

- 以上設定(`allow_anonymous true` + 聽 0.0.0.0)**僅適用可信任的內網**。
- 不可信網段請啟用帳密:

  ```bash
  sudo mosquitto_passwd -c /etc/mosquitto/passwd <username>   # 建第一個帳號
  sudo tee /etc/mosquitto/conf.d/local.conf >/dev/null <<'EOF'
  listener 1883 0.0.0.0
  allow_anonymous false
  password_file /etc/mosquitto/passwd
  EOF
  sudo systemctl restart mosquitto
  ```

  之後 bridge / daemon / mqtt_notify 連線時需提供帳密(可再擴充對應參數),並
  視需要加上 TLS。建議至少把 broker 限制在內網、不要對外開放 1883。
