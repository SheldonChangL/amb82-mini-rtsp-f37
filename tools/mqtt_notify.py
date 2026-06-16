#!/usr/bin/env python3
"""
AMB82-mini · MQTT Notify CLI
============================

把一則桌面通知透過 MQTT 推給 Mac(或任一台訂閱的主機)。office_daemon.py 的
MQTT 模式會訂閱 `amb82/notify` 與 `amb82/notify/<platform>`,收到就跳桌面通知。

用法:
    python3 mqtt_notify.py --broker <linux_ip> --target mac "下班囉"
    python3 mqtt_notify.py --broker <linux_ip> "全體通知"          # target 預設 all

target → topic 對照:
    all   → amb82/notify
    mac   → amb82/notify/mac
    linux → amb82/notify/linux

相依:paho-mqtt(`pip install paho-mqtt`)。publish 完即離開。
"""

from __future__ import annotations

import argparse
import sys

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("FATAL: paho-mqtt not installed. Run: pip install paho-mqtt",
          file=sys.stderr, flush=True)
    sys.exit(1)


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Push a desktop notification via MQTT to office daemons")
    ap.add_argument("--broker", required=True, help="MQTT broker host")
    ap.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    ap.add_argument("--target", choices=["mac", "linux", "all"], default="all",
                    help="送給哪台:mac / linux / all(預設 all)")
    ap.add_argument("--notify-base", default="amb82/notify",
                    help="通知 topic 前綴(預設 amb82/notify)")
    ap.add_argument("message", help="通知內容")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.target == "all":
        topic = args.notify_base
    else:
        topic = f"{args.notify_base}/{args.target}"

    try:
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        client = mqtt.Client()

    try:
        client.connect(args.broker, args.port, keepalive=10)
        client.loop_start()
        info = client.publish(topic, args.message, qos=1, retain=False)
        # 確保訊息送出(qos1)再離開
        info.wait_for_publish(timeout=5)
        client.loop_stop()
        client.disconnect()
    except Exception as e:
        print(f"FATAL: publish failed: {e}", file=sys.stderr, flush=True)
        return 1

    print(f"sent → {topic}: {args.message}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
