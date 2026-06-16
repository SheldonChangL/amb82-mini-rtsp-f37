#!/usr/bin/env python3
"""
AMB82-mini · Office Presence Daemon (跨平台 macOS + Linux,純 stdlib)
====================================================================

把 AMB82-mini AI 攝影機當成「辦公桌在席感測器」。攝影機韌體會在區網以 UDP
廣播 JSON(port 48555),本程式接收後驅動鎖屏 / 解鎖 / 快照 / 通知等一系列
「在席自動化」功能。

事件協定(韌體 → 主機,UDP 廣播 255.255.255.255:48555,約 3 次/秒):
    {"dev":"amb82-office","ts":<uint ms>,"faces":<int>,
     "known":["sheldon",...],"unknown":<int>}
  - faces  = 偵測到的人臉總數
  - known  = 已辨識出的「登錄者」名字清單(目前在畫面中)
  - unknown= 未辨識人臉數量
主機 → 韌體指令(UDP 單播 <camera_ip>:48556,韌體端為未來功能):
    {"cmd":"enroll","name":"<n>"} 或 {"cmd":"led","state":"green|blue|red|off"}

----------------------------------------------------------------------
連線模式(二選一):
  - 預設 UDP 模式:直接綁 UDP 0.0.0.0:48555 收板子廣播(純 stdlib,免裝套件)。
  - MQTT 模式(--mqtt HOST):改向 MQTT broker 訂閱在席與通知 topic。適合
    「板子在區網廣播 → 常開 Linux 機跑 udp_mqtt_bridge.py 橋接到 MQTT →
    多台(Linux/Mac)各自訂閱、各自鎖/解鎖自己」的架構。MQTT 模式才需要
    `pip install paho-mqtt`(延遲匯入;UDP-only 使用者完全不需要)。

----------------------------------------------------------------------
相依套件(只用 Python stdlib,其餘為「選用」的外部指令):
  - Python 3.9+
  - paho-mqtt         (選用)僅 MQTT 模式(--mqtt)才需要;延遲匯入
  - ffmpeg            (選用)抓 RTSP 快照;沒有就略過快照,不會崩潰
  - macOS:           pmset / osascript / caffeinate(系統內建)
  - Linux:           loginctl(systemd 登入工作階段)、notify-send(桌面通知)

執行:
    python3 office_daemon.py                       # UDP 模式,正式執行(會真的鎖機器!)
    python3 office_daemon.py --dry-run             # 只記錄「打算做什麼」,不真的執行
    python3 office_daemon.py --owner alice         # 覆寫 OWNER_NAMES
    python3 office_daemon.py --mqtt 192.168.1.10   # MQTT 模式(向該 broker 訂閱)

⚠️ macOS 解鎖限制(務必理解):
  第三方程式「無法」繞過 macOS 登入密碼自動解鎖。本程式在「主人回來」時只能
  喚醒螢幕(caffeinate -u),真正的密碼解鎖需要你自己到
  「系統設定 → 鎖定畫面 → 螢幕關閉後立即要求密碼:永不」把喚醒密碼關掉,
  才能達到「人到桌前螢幕亮即可用」的體感。鎖屏(Ctrl+Cmd+Q + 螢幕睡眠)則完全正常。

ℹ️ Linux 解鎖:需要 systemd-logind。loginctl unlock-session 可真正解鎖
  (前提:工作階段支援、且 PolicyKit 允許該使用者)。找不到 loginctl 時退而
  改用 DBus org.freedesktop.login1。

設計:純 socket + threading + subprocess。主迴圈絕不會因單一壞封包或失敗的
子程序而崩潰;收到 SIGINT 會關掉 keep-awake 後乾淨退出。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, date
from pathlib import Path

# ======================================================================
# CONFIG —— 所有可調參數與功能開關都在這裡(human-facing 註解用繁中)
# ======================================================================
CONFIG = {
    # --- 身份與在席判定 ---
    "OWNER_NAMES": ["sheldon"],     # 視為「主人」的登錄名字(任一出現即在席)
    "ABSENCE_LOCK_SEC": 20,         # 主人消失多久才判定「離開」並鎖機(去抖)
    "PRESENCE_DEBOUNCE_SEC": 0.6,   # 主人需連續出現多久才判定「到達」並解鎖(越小越快解鎖;含板子辨識總延遲約 1~1.5s,在 2s 內)
    "RELOCK_GRACE_SEC": 8,          # 剛解鎖後這段時間內不准再鎖(避免回來瞬間又被鎖)

    # --- 網路 ---
    "LISTEN_PORT": 48555,           # 接收韌體廣播的 UDP port
    "CMD_PORT": 48556,              # 送指令回攝影機的 UDP port
    "PACKET_TIMEOUT_SEC": 5.0,      # 超過這秒數沒封包 → 視為攝影機失聯
    "DEV_FILTER": "amb82-office",   # 只接受 dev 欄位相符的封包(None=不過濾)

    # --- RTSP / 快照 ---
    "RTSP_PORT": 554,
    "FFMPEG_TIMEOUT_SEC": 12,       # 單張快照的 ffmpeg 逾時
    "BASE_DIR": "~/amb82-office",   # 所有輸出(快照、log、csv)的根目錄

    # --- 功能總開關(逐項可關)---
    "FEAT_PRESENCE_LOCK": True,     # 1. 在席鎖屏(核心)
    "FEAT_FOREIGN_FACE_LOCK": True, # 2. 主人不在卻有陌生人 → 立即鎖 + 快照 + 通知
    "FEAT_SHOULDER_SURFER": True,   # 3. 主人在 + 多張臉 → 偷看肩膀警告
    "FEAT_VISITOR_DOORBELL": True,  # 4. 0→有人 且主人在 → 訪客通知 + 快照
    "FEAT_ATTENDANCE_LOG": True,    # 5. 出勤 CSV + 每日在席分鐘
    "FEAT_POMODORO": True,          # 6. 連續專注過久 → 該休息了
    "FEAT_DND_STATUS": True,        # 7. 狀態變化呼叫 dnd_hook(present/meeting/away)
    "FEAT_TIMELAPSE": True,         # 8. 主人在席時定時快照(縮時)
    "FEAT_AUDIT_PHOTOS": True,      # 9. 每次鎖/解鎖/陌生事件都存證快照

    # --- 各功能參數 ---
    "SHOULDER_SEC": 3,              # 多張臉持續幾秒才警告偷看
    "SHOULDER_COOLDOWN_SEC": 60,    # 偷看警告冷卻(每分鐘最多一次)
    "VISITOR_COOLDOWN_SEC": 30,     # 訪客通知冷卻
    "FOCUS_MIN": 50,                # 連續專注幾分鐘 → 提醒休息
    "FOCUS_BREAK_GAP_SEC": 120,     # 離席超過幾秒才算「中斷專注」並重置計時
    "TIMELAPSE_MIN": 10,            # 縮時快照間隔(分鐘)

    # --- 整合鉤子 ---
    "DND_HOOK": None,               # 例:'my-dnd.sh {state}';{state}∈present/meeting/away
    "ALLOW_NOPW_WAKE": False,       # macOS:你已自行關閉喚醒密碼?(僅影響 log 措辭)
    "PUSH_LED": False,              # 是否把狀態 LED 指令推回攝影機(韌體端未來功能)
}


# ======================================================================
# Logging —— 全部往 stdout,帶時間戳;絕不丟例外
# ======================================================================
_LOG_LOCK = threading.Lock()


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{level}] {msg}"
    with _LOG_LOCK:
        try:
            print(line, flush=True)
        except Exception:
            pass  # logging 本身絕不能讓 daemon 倒下


def base_dir() -> Path:
    return Path(os.path.expanduser(CONFIG["BASE_DIR"]))


def ensure_dir(p: Path) -> Path:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log(f"mkdir failed for {p}: {e}", "WARN")
    return p


def run_cmd(cmd, timeout=None, check=False):
    """執行外部指令並吞掉所有例外,回傳 (ok, CompletedProcess|None)。"""
    try:
        cp = subprocess.run(
            cmd, timeout=timeout, check=check,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return (cp.returncode == 0, cp)
    except FileNotFoundError:
        log(f"command not found: {cmd[0]}", "WARN")
    except subprocess.TimeoutExpired:
        log(f"command timeout: {' '.join(map(str, cmd))}", "WARN")
    except Exception as e:
        log(f"command error {cmd}: {e}", "WARN")
    return (False, None)


# ======================================================================
# OS abstraction —— MacController / LinuxController
#   lock() / unlock() / keep_awake_on() / keep_awake_off() / wake()
# ======================================================================
class OSController:
    """基底:預設全為 no-op,確保未知平台也不會崩潰。"""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def _dry(self, action: str) -> bool:
        if self.dry_run:
            log(f"[DRY-RUN] would {action}")
            return True
        return False

    def lock(self): pass
    def unlock(self): pass
    def keep_awake_on(self): pass
    def keep_awake_off(self): pass
    def wake(self): pass


class MacController(OSController):
    """macOS:pmset / osascript / caffeinate。"""

    def __init__(self, dry_run: bool = False):
        super().__init__(dry_run)
        self._caffeinate = None  # 在席時 hold 的 `caffeinate -d -i` 子程序

    def lock(self):
        if self._dry("lock screen (Ctrl+Cmd+Q + displaysleep)"):
            return
        # 1) 觸發鎖屏鍵 (Ctrl+Cmd+Q)
        run_cmd([
            "osascript", "-e",
            'tell application "System Events" to keystroke "q" '
            'using {control down, command down}',
        ], timeout=8)
        # 2) 立刻關螢幕
        run_cmd(["pmset", "displaysleepnow"], timeout=8)

    def unlock(self):
        # ⚠️ 第三方程式無法繞過 macOS 登入密碼。這裡只能喚醒螢幕。
        if self._dry("wake display (caffeinate -u -t 2)"):
            return
        run_cmd(["caffeinate", "-u", "-t", "2"], timeout=6)
        if CONFIG["ALLOW_NOPW_WAKE"]:
            log("display woken; ALLOW_NOPW_WAKE set → 假設你已關閉喚醒密碼,"
                "螢幕亮即可直接使用")
        else:
            log("display woken,但 macOS 仍會要求登入密碼。要真正『人到即用』,"
                "請到系統設定關閉『螢幕關閉後要求密碼』。", "WARN")

    def wake(self):
        if self._dry("wake display"):
            return
        run_cmd(["caffeinate", "-u", "-t", "2"], timeout=6)

    def keep_awake_on(self):
        if self._caffeinate and self._caffeinate.poll() is None:
            return  # 已在跑
        if self._dry("start caffeinate -d -i (keep awake while present)"):
            return
        try:
            self._caffeinate = subprocess.Popen(
                ["caffeinate", "-d", "-i"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            log("keep-awake on (caffeinate -d -i)")
        except FileNotFoundError:
            log("caffeinate not found; keep-awake unavailable", "WARN")
        except Exception as e:
            log(f"keep-awake start failed: {e}", "WARN")

    def keep_awake_off(self):
        if self.dry_run:
            log("[DRY-RUN] would stop caffeinate")
            return
        p = self._caffeinate
        self._caffeinate = None
        if p and p.poll() is None:
            try:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                log("keep-awake off")
            except Exception as e:
                log(f"keep-awake stop failed: {e}", "WARN")


class LinuxController(OSController):
    """Linux:systemd-logind(loginctl,退而求其次 DBus)。"""

    def __init__(self, dry_run: bool = False):
        super().__init__(dry_run)
        self._inhibit = None  # systemd-inhibit 子程序(keep-awake)

    def _session_id(self):
        """找出目前使用者的 graphical session id。"""
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        # 優先:目前 user 的 Display session
        ok, cp = run_cmd(["loginctl", "show-user", user, "-p", "Display"], timeout=5)
        if ok and cp:
            out = cp.stdout.decode("utf-8", "replace").strip()
            if "=" in out:
                sid = out.split("=", 1)[1].strip()
                if sid:
                    return sid
        # 退而求其次:掃 list-sessions 找 seat0 / 第一個
        ok, cp = run_cmd(["loginctl", "list-sessions", "--no-legend"], timeout=5)
        if ok and cp:
            for ln in cp.stdout.decode("utf-8", "replace").splitlines():
                parts = ln.split()
                if parts:
                    return parts[0]
        return None

    def _dbus_call(self, method: str):
        """loginctl 不可用時的 DBus 後援。"""
        sid = self._session_id() or ""
        ok, _ = run_cmd([
            "dbus-send", "--system", "--print-reply",
            "--dest=org.freedesktop.login1",
            "/org/freedesktop/login1", f"org.freedesktop.login1.Manager.{method}",
            f"string:{sid}",
        ], timeout=6)
        return ok

    def lock(self):
        if self._dry("loginctl lock-session"):
            return
        ok, _ = run_cmd(["loginctl", "lock-session"], timeout=6)
        if not ok:
            ok, _ = run_cmd(["loginctl", "lock-sessions"], timeout=6)
        if not ok and shutil.which("loginctl") is None:
            self._dbus_call("LockSession")

    def unlock(self):
        if self._dry("loginctl unlock-session"):
            return
        if shutil.which("loginctl"):
            sid = self._session_id()
            cmd = ["loginctl", "unlock-session"] + ([sid] if sid else [])
            ok, _ = run_cmd(cmd, timeout=6)
            if not ok:
                run_cmd(["loginctl", "unlock-sessions"], timeout=6)
        else:
            self._dbus_call("UnlockSession")

    def wake(self):
        # Linux 解鎖即喚醒;這裡盡力把螢幕點亮(DPMS),失敗無妨。
        if self._dry("wake display (xset dpms force on)"):
            return
        run_cmd(["xset", "dpms", "force", "on"], timeout=4)

    def keep_awake_on(self):
        if self._inhibit and self._inhibit.poll() is None:
            return
        if self._dry("systemd-inhibit (keep awake while present)"):
            return
        if shutil.which("systemd-inhibit") is None:
            return  # 沒有就算了,解鎖本身已足夠
        try:
            self._inhibit = subprocess.Popen(
                ["systemd-inhibit", "--what=idle:sleep",
                 "--who=amb82-office", "--why=owner present",
                 "sleep", "infinity"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            log("keep-awake on (systemd-inhibit)")
        except Exception as e:
            log(f"keep-awake start failed: {e}", "WARN")

    def keep_awake_off(self):
        if self.dry_run:
            log("[DRY-RUN] would stop systemd-inhibit")
            return
        p = self._inhibit
        self._inhibit = None
        if p and p.poll() is None:
            try:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                log("keep-awake off")
            except Exception as e:
                log(f"keep-awake stop failed: {e}", "WARN")


def make_controller(dry_run: bool) -> OSController:
    sysname = platform.system()
    if sysname == "Darwin":
        log("OS detected: macOS → MacController")
        return MacController(dry_run)
    if sysname == "Linux":
        log("OS detected: Linux → LinuxController")
        return LinuxController(dry_run)
    log(f"OS '{sysname}' unsupported → no-op controller (lock/unlock disabled)", "WARN")
    return OSController(dry_run)


# ======================================================================
# Notifications —— OS-aware,絕不崩潰
# ======================================================================
_NOTIFY_TITLE = "AMB82 Office"


def notify(msg: str) -> None:
    log(f"NOTIFY: {msg}")
    try:
        sysname = platform.system()
        if sysname == "Darwin":
            safe = msg.replace('"', "'")
            run_cmd([
                "osascript", "-e",
                f'display notification "{safe}" with title "{_NOTIFY_TITLE}"',
            ], timeout=6)
        elif sysname == "Linux":
            if shutil.which("notify-send"):
                run_cmd(["notify-send", _NOTIFY_TITLE, msg], timeout=6)
    except Exception as e:
        log(f"notify failed: {e}", "WARN")


# ======================================================================
# Snapshot —— 用 ffmpeg 抓一張 RTSP JPEG;ffmpeg 不存在就略過
# ======================================================================
def snapshot(camera_ip, reason: str, dry_run: bool, subdir: str = "snapshots"):
    if not camera_ip:
        log(f"snapshot({reason}) skipped: camera IP unknown", "WARN")
        return None
    outdir = ensure_dir(base_dir() / subdir)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outpath = outdir / f"{reason}-{ts}.jpg"
    if dry_run:
        log(f"[DRY-RUN] would snapshot ({reason}) → {outpath}")
        return outpath
    if shutil.which("ffmpeg") is None:
        log(f"snapshot({reason}) skipped: ffmpeg not installed", "WARN")
        return None
    url = f"rtsp://{camera_ip}:{CONFIG['RTSP_PORT']}"
    ok, _ = run_cmd([
        "ffmpeg", "-rtsp_transport", "tcp", "-i", url,
        "-frames:v", "1", "-q:v", "3", str(outpath), "-y",
    ], timeout=CONFIG["FFMPEG_TIMEOUT_SEC"])
    if ok and outpath.exists():
        log(f"snapshot saved: {outpath}")
        return outpath
    log(f"snapshot({reason}) failed", "WARN")
    return None


# ======================================================================
# UDP I/O —— receiver thread + command sender
# ======================================================================
class CameraLink:
    """綁定 UDP 0.0.0.0:LISTEN_PORT 接收廣播;送指令回 camera。"""

    def __init__(self):
        self.camera_ip = None
        self.last_packet_ts = 0.0
        self._lock = threading.Lock()
        self._sock = None
        self._tx = None

    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  # 非所有平台都有 SO_REUSEPORT
        s.bind(("0.0.0.0", CONFIG["LISTEN_PORT"]))
        s.settimeout(1.0)
        self._sock = s
        self._tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        log(f"listening UDP 0.0.0.0:{CONFIG['LISTEN_PORT']}")

    def recv_event(self):
        """阻塞至多 1 秒;回傳 parsed dict 或 None(逾時/壞封包)。"""
        try:
            data, addr = self._sock.recvfrom(8192)
        except socket.timeout:
            return None
        except OSError:
            return None
        try:
            evt = json.loads(data.decode("utf-8", "replace"))
            if not isinstance(evt, dict):
                return None
        except Exception:
            log(f"malformed datagram from {addr[0]} ({len(data)}B), ignored", "WARN")
            return None
        # dev 過濾
        if CONFIG["DEV_FILTER"] and evt.get("dev") != CONFIG["DEV_FILTER"]:
            return None
        with self._lock:
            if self.camera_ip != addr[0]:
                self.camera_ip = addr[0]
                log(f"camera IP learned: {self.camera_ip}")
            self.last_packet_ts = time.monotonic()
        # 正規化欄位,容忍缺漏 / 型別錯誤
        return {
            "faces": _as_int(evt.get("faces"), 0),
            "known": _as_str_list(evt.get("known")),
            "unknown": _as_int(evt.get("unknown"), 0),
            "ts": _as_int(evt.get("ts"), 0),
        }

    def get_ip(self):
        with self._lock:
            return self.camera_ip

    def seconds_since_packet(self):
        with self._lock:
            if not self.last_packet_ts:
                return float("inf")
            return time.monotonic() - self.last_packet_ts

    def send_cmd(self, payload: dict, dry_run: bool):
        ip = self.get_ip()
        if not ip:
            log(f"send_cmd skipped (no camera IP): {payload}", "WARN")
            return
        if dry_run:
            log(f"[DRY-RUN] would send cmd to {ip}:{CONFIG['CMD_PORT']}: {payload}")
            return
        try:
            raw = json.dumps(payload).encode("utf-8")
            self._tx.sendto(raw, (ip, CONFIG["CMD_PORT"]))
            log(f"cmd → {ip}:{CONFIG['CMD_PORT']}: {payload}")
        except Exception as e:
            log(f"send_cmd failed: {e}", "WARN")

    # 便利包裝:登錄 / LED
    def enroll(self, name: str, dry_run: bool):
        self.send_cmd({"cmd": "enroll", "name": name}, dry_run)

    def set_led(self, state: str, dry_run: bool):
        self.send_cmd({"cmd": "led", "state": state}, dry_run)

    def close(self):
        for s in (self._sock, self._tx):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_str_list(v):
    if isinstance(v, list):
        return [str(x) for x in v]
    return []


# ======================================================================
# MQTT I/O —— CameraLink 的 MQTT 替身(只在 --mqtt 時啟用)
#   公開介面與 CameraLink 完全相同,主迴圈無需改動。
#   paho-mqtt 為「選用」相依,僅 MQTT 模式才需要 → 延遲匯入(lazy import)。
# ======================================================================
import queue  # stdlib;MqttLink 內部事件佇列用


class MqttLink:
    """以 MQTT 取代 UDP:訂閱在席 topic 與通知 topic,publish 指令 topic。

    對外介面與 CameraLink 一致:open / recv_event / get_ip /
    seconds_since_packet / send_cmd / enroll / set_led / close。
    板子韌體不變;UDP→MQTT 由 udp_mqtt_bridge.py 在常開 Linux 機上做橋接。
    """

    def __init__(self, broker: str, port: int = 1883,
                 presence_topic: str = "amb82/office/presence",
                 notify_base: str = "amb82/notify",
                 cmd_topic: str = "amb82/office/cmd"):
        self.broker = broker
        self.port = port
        self.presence_topic = presence_topic
        self.notify_base = notify_base
        self.cmd_topic = cmd_topic

        self.camera_ip = None
        self.last_packet_ts = 0.0
        self._lock = threading.Lock()
        self._q = queue.Queue()         # 正規化後的在席事件佇列(thread-safe)
        self._client = None
        self._mqtt = None               # lazy-imported paho module

        # 本機平台 → 通知子 topic(mac / linux)
        sysname = platform.system()
        self._platform = "mac" if sysname == "Darwin" else (
            "linux" if sysname == "Linux" else sysname.lower())
        self._notify_topic_plat = f"{notify_base}/{self._platform}"

    def open(self):
        # 延遲匯入:UDP-only 使用者不需安裝 paho-mqtt
        try:
            import paho.mqtt.client as mqtt
        except ImportError as e:
            log("MQTT mode requires paho-mqtt. Install it with: "
                "pip install paho-mqtt", "ERROR")
            raise
        self._mqtt = mqtt
        # 相容 paho v1 與 v2 callback API
        try:
            c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
        except (AttributeError, TypeError):
            c = mqtt.Client()
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client = c
        try:
            c.connect(self.broker, self.port, keepalive=60)
        except Exception as e:
            log(f"MQTT initial connect failed ({e}); will retry in background", "WARN")
            try:
                c.connect_async(self.broker, self.port, keepalive=60)
            except Exception:
                pass
        c.loop_start()
        log(f"MQTT mode: broker {self.broker}:{self.port}, "
            f"presence='{self.presence_topic}', "
            f"notify='{self.notify_base}'(+/{self._platform})")

    def _on_connect(self, client, userdata, flags, rc, *args):
        if rc == 0:
            log(f"MQTT connected to {self.broker}:{self.port}")
            client.subscribe(self.presence_topic, qos=0)
            client.subscribe(self.notify_base, qos=0)
            client.subscribe(self._notify_topic_plat, qos=0)
            log(f"subscribed: {self.presence_topic}, {self.notify_base}, "
                f"{self._notify_topic_plat}")
        else:
            log(f"MQTT connect failed rc={rc}", "WARN")

    def _on_disconnect(self, client, userdata, rc, *args):
        if rc != 0:
            log(f"MQTT disconnected (rc={rc}); auto-reconnecting…", "WARN")

    def _on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            if topic == self.presence_topic:
                self._handle_presence(msg.payload)
            elif topic == self.notify_base or topic.startswith(self.notify_base + "/"):
                self._handle_notify(msg.payload)
        except Exception as e:
            log(f"on_message error (ignored): {e}", "WARN")

    def _handle_presence(self, payload: bytes):
        try:
            evt = json.loads(payload.decode("utf-8", "replace"))
            if not isinstance(evt, dict):
                return
        except Exception:
            log("malformed presence MQTT payload, ignored", "WARN")
            return
        # dev 過濾(與 CameraLink 一致)
        if CONFIG["DEV_FILTER"] and evt.get("dev") != CONFIG["DEV_FILTER"]:
            return
        ip = evt.get("ip")
        with self._lock:
            if ip and self.camera_ip != ip:
                self.camera_ip = ip
                log(f"camera IP learned (via MQTT): {ip}")
            self.last_packet_ts = time.monotonic()
        self._q.put({
            "faces": _as_int(evt.get("faces"), 0),
            "known": _as_str_list(evt.get("known")),
            "unknown": _as_int(evt.get("unknown"), 0),
            "ts": _as_int(evt.get("ts"), 0),
        })

    def _handle_notify(self, payload: bytes):
        text = payload.decode("utf-8", "replace")
        # 若是 JSON 且帶 "msg" 欄位 → 取其值;否則直接用原字串
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "msg" in obj:
                text = str(obj["msg"])
        except Exception:
            pass
        notify(text)

    def recv_event(self):
        """阻塞至多 ~1 秒;回傳正規化 dict 或 None(逾時)。"""
        try:
            return self._q.get(timeout=1.0)
        except queue.Empty:
            return None

    def get_ip(self):
        with self._lock:
            return self.camera_ip

    def seconds_since_packet(self):
        with self._lock:
            if not self.last_packet_ts:
                return float("inf")
            return time.monotonic() - self.last_packet_ts

    def send_cmd(self, payload: dict, dry_run: bool):
        if dry_run:
            log(f"[DRY-RUN] would publish cmd to {self.cmd_topic}: {payload}")
            return
        if not self._client:
            log(f"send_cmd skipped (MQTT not connected): {payload}", "WARN")
            return
        try:
            raw = json.dumps(payload)
            self._client.publish(self.cmd_topic, raw, qos=0, retain=False)
            log(f"cmd → MQTT {self.cmd_topic}: {payload}")
        except Exception as e:
            log(f"send_cmd failed: {e}", "WARN")

    # 便利包裝:登錄 / LED(與 CameraLink 相同)
    def enroll(self, name: str, dry_run: bool):
        self.send_cmd({"cmd": "enroll", "name": name}, dry_run)

    def set_led(self, state: str, dry_run: bool):
        self.send_cmd({"cmd": "led", "state": state}, dry_run)

    def close(self):
        try:
            if self._client:
                self._client.loop_stop()
                self._client.disconnect()
        except Exception:
            pass


# ======================================================================
# Attendance log —— CSV + 每日在席分鐘
# ======================================================================
class Attendance:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.csv_path = base_dir() / "attendance.csv"
        self.today = date.today()
        self.in_seat_seconds = 0.0      # 今日累計在席秒數
        self._present_since = None      # 本段在席起點(monotonic)

    def _row(self, event: str):
        owner = ",".join(CONFIG["OWNER_NAMES"])
        now = datetime.now()
        if self.dry_run:
            log(f"[DRY-RUN] would append attendance: {event} {owner}")
            return
        ensure_dir(self.csv_path.parent)
        try:
            with open(self.csv_path, "a", newline="") as f:
                csv.writer(f).writerow(
                    [now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), event, owner])
        except Exception as e:
            log(f"attendance write failed: {e}", "WARN")

    def _roll_day_if_needed(self):
        if date.today() != self.today:
            self._write_daily_summary()
            self.today = date.today()
            self.in_seat_seconds = 0.0

    def on_arrived(self):
        self._roll_day_if_needed()
        self._present_since = time.monotonic()
        self._row("enter")
        log(f"attendance: ENTER")

    def on_departed(self):
        self._roll_day_if_needed()
        if self._present_since is not None:
            self.in_seat_seconds += time.monotonic() - self._present_since
            self._present_since = None
        self._row("leave")
        log(f"attendance: LEAVE (today in-seat ~{self.in_seat_seconds/60:.1f} min)")

    def _write_daily_summary(self):
        secs = self.in_seat_seconds
        if self._present_since is not None:
            secs += time.monotonic() - self._present_since
        minutes = secs / 60.0
        owner = ",".join(CONFIG["OWNER_NAMES"])
        log(f"attendance daily summary {self.today}: {minutes:.1f} min in-seat")
        if self.dry_run:
            return
        ensure_dir(self.csv_path.parent)
        try:
            with open(self.csv_path, "a", newline="") as f:
                csv.writer(f).writerow(
                    [self.today.strftime("%Y-%m-%d"), "23:59:59",
                     "daily_total_min", owner, f"{minutes:.1f}"])
        except Exception as e:
            log(f"attendance summary write failed: {e}", "WARN")

    def tick(self):
        """主迴圈定期呼叫:跨日時補寫前一天總結。"""
        self._roll_day_if_needed()


# ======================================================================
# Presence engine —— hysteresis + 所有功能
# ======================================================================
class PresenceEngine:
    def __init__(self, link: CameraLink, ctrl: OSController, dry_run: bool, armed: bool = False):
        self.link = link
        self.ctrl = ctrl
        self.dry_run = dry_run
        self.armed = armed                  # 未 --arm 時:只記錄不真的鎖/解鎖(防鎖死)
        self.att = Attendance(dry_run)

        # 在席狀態機
        self.owner_present = False          # 已確認(去抖後)的在席狀態
        self._owner_seen_since = None       # 連續看到主人的起點
        self._owner_gone_since = None       # 連續沒看到主人的起點

        # 螢幕鎖狀態(daemon 自己追蹤,避免重複鎖造成「打不進密碼」的鎖死迴圈)
        self._screen_locked = False
        self._last_unlock = 0.0

        # 各功能狀態
        self._prev_faces = 0
        self._shoulder_since = None
        self._last_shoulder_notify = 0.0
        self._last_visitor_notify = 0.0
        self._focus_start = None            # 本段專注起點(monotonic)
        self._focus_gap_since = None        # 專注中暫時離席的起點
        self._last_pomodoro_notify = 0.0
        self._last_timelapse = 0.0
        self._last_dnd_state = None
        self._lost = False                  # 攝影機失聯旗標

    # ---- 對外:餵入一筆事件(已正規化)或 None ----
    def feed(self, evt):
        now = time.monotonic()

        # 攝影機失聯處理:當作「沒有主人、沒有臉」,但不誤觸發陌生人鎖
        if evt is None:
            if self.link.seconds_since_packet() > CONFIG["PACKET_TIMEOUT_SEC"]:
                if not self._lost:
                    self._lost = True
                    log("camera LOST (no packets) → treating as unknown", "WARN")
                # 失聯時主人視為不在 → 走正常離席去抖流程,但 faces 視為 0
                self._update_presence(owner_in_frame=False, now=now)
            return

        if self._lost:
            self._lost = False
            log("camera back online")

        faces = evt["faces"]
        known = evt["known"]
        unknown = evt["unknown"]
        owner_in_frame = any(o in known for o in CONFIG["OWNER_NAMES"])

        # 1) 在席去抖
        self._update_presence(owner_in_frame, now)
        # 番茄鐘:在席中「短暫離席」< FOCUS_BREAK_GAP_SEC 不重置計時,
        # 超過才重置(避免起身倒杯水就歸零;真正離席由 _on_departed 處理)。
        self._track_focus_gap(owner_in_frame, now)

        # 2) 其餘功能(都在「已知 faces」前提下)
        if CONFIG["FEAT_FOREIGN_FACE_LOCK"]:
            self._foreign_face_lock(unknown, now)
        if CONFIG["FEAT_VISITOR_DOORBELL"]:
            self._visitor_doorbell(faces, now)
        if CONFIG["FEAT_SHOULDER_SURFER"]:
            self._shoulder_surfer(faces, now)
        if CONFIG["FEAT_POMODORO"]:
            self._pomodoro(now)
        if CONFIG["FEAT_DND_STATUS"]:
            self._dnd_status(faces)
        if CONFIG["FEAT_TIMELAPSE"]:
            self._timelapse(now)

        self._prev_faces = faces

    # ---- 在席狀態機(hysteresis)----
    def _update_presence(self, owner_in_frame: bool, now: float):
        if owner_in_frame:
            self._owner_gone_since = None
            if self._owner_seen_since is None:
                self._owner_seen_since = now
            if (not self.owner_present and
                    now - self._owner_seen_since >= CONFIG["PRESENCE_DEBOUNCE_SEC"]):
                self._on_arrived(now)
        else:
            self._owner_seen_since = None
            if self._owner_gone_since is None:
                self._owner_gone_since = now
            if (self.owner_present and
                    now - self._owner_gone_since >= CONFIG["ABSENCE_LOCK_SEC"]):
                self._on_departed(now)

    # ---- 鎖/解鎖封裝:冪等 + grace + 武裝開關 + DISARM 失效保險 ----
    def _disarmed_by_file(self) -> bool:
        try:
            return os.path.exists(os.path.join(base_dir(), "DISARM"))
        except Exception:
            return False

    def _do_lock(self, reason: str):
        if self._screen_locked:
            return                                   # 已鎖 → 不重複鎖(關鍵:消除鎖死迴圈)
        if time.monotonic() - self._last_unlock < CONFIG["RELOCK_GRACE_SEC"]:
            log(f"[grace 期內,不鎖] {reason}")
            return
        if self._disarmed_by_file():
            log(f"[DISARM 檔存在 → 不鎖] {reason}", "WARN")
            return
        self._screen_locked = True
        if self.armed:
            log(f"LOCK ({reason})")
            self.ctrl.lock()
        else:
            log(f"[未 --arm → 只記錄] would LOCK ({reason})")

    def _do_unlock(self):
        self._last_unlock = time.monotonic()
        was_locked = self._screen_locked
        self._screen_locked = False
        if self.armed:
            if was_locked:
                log("UNLOCK")
            self.ctrl.unlock()
        else:
            log("[未 --arm → 只記錄] would UNLOCK")

    def _on_arrived(self, now: float):
        self.owner_present = True
        log("OWNER ARRIVED → unlock / wake / keep-awake")
        if CONFIG["FEAT_PRESENCE_LOCK"]:
            self.ctrl.keep_awake_on()
            self._do_unlock()
            self.ctrl.wake()
        if CONFIG["FEAT_ATTENDANCE_LOG"]:
            self.att.on_arrived()
        if CONFIG["FEAT_AUDIT_PHOTOS"]:
            snapshot(self.link.get_ip(), "unlock", self.dry_run, subdir="audit")
        if CONFIG["PUSH_LED"]:
            self.link.set_led("green", self.dry_run)
        # 開始 / 重置專注計時
        self._focus_start = now
        self._focus_gap_since = None
        self._last_pomodoro_notify = 0.0
        self._last_timelapse = now  # 避免一到就立刻拍

    def _on_departed(self, now: float):
        self.owner_present = False
        log("OWNER DEPARTED → lock")
        if CONFIG["FEAT_PRESENCE_LOCK"]:
            self._do_lock("owner departed")
            self.ctrl.keep_awake_off()
        if CONFIG["FEAT_ATTENDANCE_LOG"]:
            self.att.on_departed()
        if CONFIG["FEAT_AUDIT_PHOTOS"]:
            snapshot(self.link.get_ip(), "lock", self.dry_run, subdir="audit")
        if CONFIG["PUSH_LED"]:
            self.link.set_led("off", self.dry_run)
        # 離席重置專注計時
        self._focus_start = None
        self._focus_gap_since = None

    # ---- 2. 陌生人鎖 ----
    def _foreign_face_lock(self, unknown: int, now: float):
        # 只在「目前未鎖、主人不在、且有陌生人」時動作一次。
        # _screen_locked 守門 → 不會每幀(~3Hz)重複鎖,這正是先前把人鎖死的元兇。
        if self._screen_locked or self.owner_present or unknown < 1:
            return
        if not CONFIG["FEAT_PRESENCE_LOCK"]:
            return
        log("FOREIGN FACE while owner away → lock + snapshot + notify", "WARN")
        self._do_lock("foreign face")            # 冪等;鎖一次後 _screen_locked 擋住後續
        snapshot(self.link.get_ip(), "intruder", self.dry_run, subdir="audit")
        notify("Unknown person at your desk")
        if CONFIG["PUSH_LED"]:
            self.link.set_led("red", self.dry_run)

    # ---- 3. 偷看肩膀 ----
    def _shoulder_surfer(self, faces: int, now: float):
        if self.owner_present and faces >= 2:
            if self._shoulder_since is None:
                self._shoulder_since = now
            if now - self._shoulder_since >= CONFIG["SHOULDER_SEC"]:
                if now - self._last_shoulder_notify >= CONFIG["SHOULDER_COOLDOWN_SEC"]:
                    self._last_shoulder_notify = now
                    notify("Someone may be looking over your shoulder")
        else:
            self._shoulder_since = None

    # ---- 4. 訪客門鈴 ----
    def _visitor_doorbell(self, faces: int, now: float):
        # 0 → >=1 且主人在席
        if self._prev_faces == 0 and faces >= 1 and self.owner_present:
            if now - self._last_visitor_notify >= CONFIG["VISITOR_COOLDOWN_SEC"]:
                self._last_visitor_notify = now
                notify("Someone approached your desk")
                snapshot(self.link.get_ip(), "visitor", self.dry_run)

    # ---- 6. 番茄鐘 ----
    def _pomodoro(self, now: float):
        if not self.owner_present or self._focus_start is None:
            return
        elapsed = now - self._focus_start
        if elapsed >= CONFIG["FOCUS_MIN"] * 60:
            # 每段專注只提醒一次(直到重置)
            if self._last_pomodoro_notify < self._focus_start:
                self._last_pomodoro_notify = now
                notify("Time for a break")

    def _track_focus_gap(self, owner_in_frame: bool, now: float):
        """在『已確認在席』期間,逐幀追蹤主人是否暫時離開畫面。
        短暫離開(< FOCUS_BREAK_GAP_SEC)視為仍在專注;超過則重置番茄鐘計時。"""
        if not self.owner_present or self._focus_start is None:
            return
        if owner_in_frame:
            self._focus_gap_since = None
        else:
            if self._focus_gap_since is None:
                self._focus_gap_since = now
            elif now - self._focus_gap_since >= CONFIG["FOCUS_BREAK_GAP_SEC"]:
                # 中斷夠久 → 重置專注計時,回來重新累積
                self._focus_start = now
                self._focus_gap_since = None
                self._last_pomodoro_notify = 0.0
                log("focus reset (absence gap exceeded FOCUS_BREAK_GAP_SEC)")

    # ---- 7. DnD 狀態鉤子 ----
    def _dnd_status(self, faces: int):
        if self.owner_present and faces >= 2:
            state = "meeting"
        elif self.owner_present:
            state = "present"
        else:
            state = "away"
        if state == self._last_dnd_state:
            return
        self._last_dnd_state = state
        log(f"dnd state → {state}")
        hook = CONFIG["DND_HOOK"]
        if not hook:
            return
        cmd = hook.format(state=state)
        if self.dry_run:
            log(f"[DRY-RUN] would run dnd_hook: {cmd}")
            return
        try:
            subprocess.Popen(cmd, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log(f"dnd_hook failed: {e}", "WARN")

    # ---- 8. 縮時 ----
    def _timelapse(self, now: float):
        if not self.owner_present:
            return
        if now - self._last_timelapse >= CONFIG["TIMELAPSE_MIN"] * 60:
            self._last_timelapse = now
            snapshot(self.link.get_ip(), "timelapse", self.dry_run, subdir="timelapse")
            # 組裝縮時影片(離線):
            #   ffmpeg -framerate 12 -pattern_type glob -i '~/amb82-office/timelapse/*.jpg' \
            #          -c:v libx264 -pix_fmt yuv420p timelapse.mp4

    def tick(self):
        """主迴圈每輪呼叫:處理跨日總結與番茄鐘的純時間推進。"""
        if CONFIG["FEAT_ATTENDANCE_LOG"]:
            self.att.tick()
        if CONFIG["FEAT_POMODORO"]:
            self._pomodoro(time.monotonic())
        if CONFIG["FEAT_TIMELAPSE"]:
            self._timelapse(time.monotonic())


# ======================================================================
# Main
# ======================================================================
_STOP = threading.Event()


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
        description="AMB82-mini office presence daemon")
    ap.add_argument("--dry-run", action="store_true",
                    help="只記錄打算做的動作,不真的鎖/解鎖/快照")
    ap.add_argument("--arm", action="store_true",
                    help="真的執行鎖/解鎖(預設關閉以防鎖死;先不加跑看 log 判斷對了再加)")
    ap.add_argument("--owner", action="append", default=None,
                    help="覆寫 OWNER_NAMES(可多次)")
    ap.add_argument("--port", type=int, default=None,
                    help="覆寫 LISTEN_PORT")
    ap.add_argument("--unlock-debounce", type=float, default=None,
                    help="解鎖去抖秒數(越小越快解鎖,預設 0.6)")
    # --- MQTT 模式(additive;不加 --mqtt 仍是預設的 UDP 模式)---
    ap.add_argument("--mqtt", default=None, metavar="HOST",
                    help="啟用 MQTT 模式並指定 broker host(需 pip install paho-mqtt);"
                         "不加則維持預設 UDP 廣播模式")
    ap.add_argument("--mqtt-port", type=int, default=1883,
                    help="MQTT broker port(預設 1883)")
    ap.add_argument("--mqtt-topic", default="amb82/office/presence",
                    help="在席事件 topic(預設 amb82/office/presence)")
    ap.add_argument("--notify-topic", default="amb82/notify",
                    help="桌面通知 topic 前綴(預設 amb82/notify;另訂閱 /mac 或 /linux)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.owner:
        CONFIG["OWNER_NAMES"] = args.owner
    if args.port:
        CONFIG["LISTEN_PORT"] = args.port
    if args.unlock_debounce is not None:
        CONFIG["PRESENCE_DEBOUNCE_SEC"] = args.unlock_debounce

    dry = args.dry_run
    armed = args.arm and not dry
    ensure_dir(base_dir())
    log("=" * 60)
    log(f"AMB82 Office Daemon starting (dry_run={dry}, armed={armed})")
    if not armed:
        log("** 未武裝:只記錄、不會真的鎖/解鎖。確認 log 判斷正確後加 --arm 才會真的鎖 **", "WARN")
    log(f"失效保險:在 {os.path.join(base_dir(), 'DISARM')} 建一個檔即可即時停止鎖定")
    log(f"owners={CONFIG['OWNER_NAMES']} "
        f"absence_lock={CONFIG['ABSENCE_LOCK_SEC']}s "
        f"presence_debounce={CONFIG['PRESENCE_DEBOUNCE_SEC']}s "
        f"relock_grace={CONFIG['RELOCK_GRACE_SEC']}s")
    enabled = [k for k, v in CONFIG.items() if k.startswith("FEAT_") and v]
    log(f"features: {', '.join(enabled)}")
    log("=" * 60)

    _install_signal_handlers()

    ctrl = make_controller(dry)
    if args.mqtt:
        log(f"link mode: MQTT (broker {args.mqtt}:{args.mqtt_port})")
        link = MqttLink(args.mqtt, args.mqtt_port, args.mqtt_topic, args.notify_topic)
        try:
            link.open()
        except Exception as e:
            log(f"FATAL: cannot start MQTT link: {e}", "ERROR")
            return 1
    else:
        log("link mode: UDP broadcast (default)")
        link = CameraLink()
        try:
            link.open()
        except Exception as e:
            log(f"FATAL: cannot bind UDP {CONFIG['LISTEN_PORT']}: {e}", "ERROR")
            return 1

    engine = PresenceEngine(link, ctrl, dry, armed=armed)

    last_tick = 0.0
    try:
        while not _STOP.is_set():
            try:
                evt = link.recv_event()   # 阻塞至多 1 秒
                engine.feed(evt)
                # 每約 1 秒做一次時間推進(番茄鐘/縮時/跨日)
                now = time.monotonic()
                if now - last_tick >= 1.0:
                    last_tick = now
                    engine.tick()
            except Exception as e:
                # 單筆事件的任何錯誤都不能弄垮 daemon
                log(f"loop error (continuing): {e}", "ERROR")
                time.sleep(0.2)
    finally:
        log("cleaning up …")
        try:
            ctrl.keep_awake_off()
        except Exception:
            pass
        link.close()
        log("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
