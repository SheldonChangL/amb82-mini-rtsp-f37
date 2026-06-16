#!/usr/bin/env python3
"""
AMB82-mini · UDP → MQTT Bridge(常開 Linux 主機上跑)
====================================================

韌體不動。攝影機照舊以 UDP 廣播在席事件(255.255.255.255:48555)。本橋接程式
跑在一台「常開的 Linux 主機」(同機也跑 mosquitto broker),負責:

  1. 綁定 UDP 0.0.0.0:48555 收板子廣播。
  2. 每收到一筆 → 解析 JSON;若 dev=="amb82-office",**補上 "ip" 欄位**
     (= UDP 來源位址,讓訂閱端知道相機 IP 可拉 RTSP/快照),再 publish 到
     MQTT topic `amb82/office/presence`(qos 0, retain False)。
  3. 訂閱 `amb82/office/cmd`;收到訊息 → 把原始 payload 以 UDP sendto 轉送到
     「最近一次看到的板子 IP」:48556(讓 MQTT 來的 enroll/led 指令能回到板子)。

韌體 → 主機事件協定(UDP 廣播 255.255.255.255:48555):
    {"dev":"amb82-office","ts":<uint ms>,"faces":<int>,
     "known":[str],"unknown":<int>}

相依:paho-mqtt(`pip install paho-mqtt`)。

執行:
    python3 udp_mqtt_bridge.py --broker localhost

設計:broker 斷線自動重連(reconnect_delay_set + loop_start);單一壞封包絕不
弄垮程式;全部 log 到 stdout 帶時間戳;收到 SIGINT 乾淨退出。
"""

from __future__ import annotations

import argparse
import json
import signal
import socket
import sys
import threading
import time
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("FATAL: paho-mqtt not installed. Run: pip install paho-mqtt",
          file=sys.stderr, flush=True)
    sys.exit(1)


DEV_FILTER = "amb82-office"
_STOP = threading.Event()


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        print(f"{ts} [{level}] {msg}", flush=True)
    except Exception:
        pass


class Bridge:
    def __init__(self, broker, port, topic_prefix, udp_port, cmd_udp_port):
        self.broker = broker
        self.port = port
        self.presence_topic = f"{topic_prefix}/presence"
        self.cmd_topic = f"{topic_prefix}/cmd"
        self.udp_port = udp_port
        self.cmd_udp_port = cmd_udp_port

        self.board_ip = None              # 最近一次看到的板子 IP
        self._lock = threading.Lock()
        self._rx = None                   # UDP 接收 socket
        self._tx = None                   # UDP 送指令 socket
        self.client = None

    # ---- MQTT ----
    def _make_client(self):
        # 相容 paho-mqtt v1 與 v2 的 callback API
        try:
            c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
        except (AttributeError, TypeError):
            c = mqtt.Client()
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        return c

    def _on_connect(self, client, userdata, flags, rc, *args):
        if rc == 0:
            log(f"MQTT connected to {self.broker}:{self.port}")
            client.subscribe(self.cmd_topic, qos=0)
            log(f"subscribed cmd topic: {self.cmd_topic}")
        else:
            log(f"MQTT connect failed rc={rc}", "WARN")

    def _on_disconnect(self, client, userdata, rc, *args):
        if rc != 0:
            log(f"MQTT disconnected (rc={rc}); auto-reconnecting…", "WARN")

    def _on_message(self, client, userdata, msg):
        # MQTT cmd → 轉送 UDP 給板子
        try:
            ip = self.get_board_ip()
            if not ip:
                log("cmd received but board IP unknown yet → dropped", "WARN")
                return
            self._tx.sendto(msg.payload, (ip, self.cmd_udp_port))
            log(f"cmd MQTT→UDP {ip}:{self.cmd_udp_port}: "
                f"{msg.payload.decode('utf-8', 'replace')}")
        except Exception as e:
            log(f"cmd forward failed: {e}", "WARN")

    # ---- 狀態 ----
    def get_board_ip(self):
        with self._lock:
            return self.board_ip

    def _set_board_ip(self, ip):
        with self._lock:
            if self.board_ip != ip:
                self.board_ip = ip
                log(f"board IP learned: {ip}")

    # ---- UDP ----
    def _open_udp(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  # 非所有平台都有 SO_REUSEPORT
        rx.bind(("0.0.0.0", self.udp_port))
        rx.settimeout(1.0)
        self._rx = rx
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._tx = tx
        log(f"listening UDP 0.0.0.0:{self.udp_port}")

    def _handle_datagram(self, data, addr):
        try:
            evt = json.loads(data.decode("utf-8", "replace"))
            if not isinstance(evt, dict):
                return
        except Exception:
            log(f"malformed datagram from {addr[0]} ({len(data)}B), ignored", "WARN")
            return
        if evt.get("dev") != DEV_FILTER:
            return
        self._set_board_ip(addr[0])
        # 補上來源 IP,讓訂閱端能拉 RTSP/快照
        evt["ip"] = addr[0]
        try:
            payload = json.dumps(evt)
            self.client.publish(self.presence_topic, payload, qos=0, retain=False)
        except Exception as e:
            log(f"publish failed: {e}", "WARN")

    # ---- 主迴圈 ----
    def run(self):
        self._open_udp()
        self.client = self._make_client()
        try:
            self.client.connect(self.broker, self.port, keepalive=60)
        except Exception as e:
            log(f"initial MQTT connect failed ({e}); will retry in background", "WARN")
            # connect_async 之後 loop_start 會持續嘗試
            try:
                self.client.connect_async(self.broker, self.port, keepalive=60)
            except Exception:
                pass
        self.client.loop_start()
        log(f"bridge up: UDP:{self.udp_port} ↔ MQTT {self.presence_topic} / {self.cmd_topic}")

        while not _STOP.is_set():
            try:
                data, addr = self._rx.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                continue
            except Exception as e:
                log(f"recv error (continuing): {e}", "WARN")
                time.sleep(0.2)
                continue
            try:
                self._handle_datagram(data, addr)
            except Exception as e:
                log(f"datagram handling error (continuing): {e}", "WARN")

    def close(self):
        try:
            if self.client:
                self.client.loop_stop()
                self.client.disconnect()
        except Exception:
            pass
        for s in (self._rx, self._tx):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def _install_signal_handlers():
    def handler(signum, frame):
        log(f"signal {signum} received → shutting down")
        _STOP.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except Exception:
            pass


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="AMB82-mini UDP→MQTT bridge (runs on always-on Linux box)")
    ap.add_argument("--broker", default="localhost", help="MQTT broker host")
    ap.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    ap.add_argument("--topic-prefix", default="amb82/office",
                    help="topic 前綴;presence=<prefix>/presence, cmd=<prefix>/cmd")
    ap.add_argument("--udp-port", type=int, default=48555,
                    help="接收板子廣播的 UDP port")
    ap.add_argument("--cmd-udp-port", type=int, default=48556,
                    help="送指令回板子的 UDP port")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    _install_signal_handlers()
    log("=" * 60)
    log(f"UDP→MQTT bridge starting: broker={args.broker}:{args.port} "
        f"prefix={args.topic_prefix} udp={args.udp_port} cmd_udp={args.cmd_udp_port}")
    log("=" * 60)
    bridge = Bridge(args.broker, args.port, args.topic_prefix,
                    args.udp_port, args.cmd_udp_port)
    try:
        bridge.run()
    except Exception as e:
        log(f"FATAL: {e}", "ERROR")
        return 1
    finally:
        log("cleaning up …")
        bridge.close()
        log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
