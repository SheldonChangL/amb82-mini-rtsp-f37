#!/usr/bin/env python3
"""
AMB82-mini · BLE Wi-Fi 配網測試台 (Python / PySide6 / bleak)

真正操作 BLE:自己掃描、列裝置、連線、讀寫 characteristic、收 notify ——
沒有瀏覽器原生選單。內嵌 RTSP 預覽(QMediaPlayer),並可用 ffplay 開外部視窗。

需求:
    python3 -m pip install PySide6 qasync bleak
    # RTSP 內嵌播放需 Qt FFmpeg 後端(PySide6 6.5+ 內建);外部播放需系統有 ffplay

執行:
    python3 ble_wifi_tester.py

macOS 注意:第一次掃描會要求藍牙權限 —— 到「系統設定 → 隱私權與安全性 →
藍牙」把「終端機」(或你的 Python)打勾,否則掃不到任何裝置。

協定(對應韌體 wifi_prov_service.c,16-bit UUID):
    Service 0xA100 · SSID(W) 0xA101 · PASS(W) 0xA102 · CTRL(W) 0xA103
    STATUS(R/N) 0xA104 · IP(R/N) 0xA105
    寫 SSID → PASS → CTRL=0x01;STATUS 0x00 idle/0x01 connecting/0x02 connected/
    0x03 fail/0x04 wrong-password。
"""
import os
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")  # enable RTSP in QMediaPlayer

import sys
import asyncio
import subprocess
import shutil
import json
import socket
import threading
import queue

try:
    from PySide6.QtCore import Qt, QUrl, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (
        QApplication, QWidget, QLabel, QLineEdit, QPushButton, QListWidget,
        QListWidgetItem, QVBoxLayout, QHBoxLayout, QGridLayout, QTextEdit,
        QCheckBox, QFrame, QSizePolicy,
    )
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
except ImportError:
    sys.exit("缺少 PySide6 —— 請先執行:python3 -m pip install PySide6 qasync bleak")

try:
    import qasync
    from qasync import asyncSlot
except ImportError:
    sys.exit("缺少 qasync —— 請先執行:python3 -m pip install qasync")

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    sys.exit("缺少 bleak —— 請先執行:python3 -m pip install bleak")

# ---- protocol UUIDs (16-bit, canonical 128-bit form) ---------------------
def u16(x):  # 0xA100 -> "0000a100-0000-1000-8000-00805f9b34fb"
    return f"0000{x:04x}-0000-1000-8000-00805f9b34fb"

UUID_SVC, UUID_SSID, UUID_PASS = u16(0xA100), u16(0xA101), u16(0xA102)
UUID_CTRL, UUID_STATUS, UUID_IP = u16(0xA103), u16(0xA104), u16(0xA105)
NAME_PREFIX = "Ameba_"

STATUS = {
    0: ("待命",     "#6f827c", "IDLE"),
    1: ("連線中",   "#f5b73d", "CONNECTING"),
    2: ("已連上",   "#39e0a0", "CONNECTED"),
    3: ("連線失敗", "#ff5a52", "FAIL"),
    4: ("密碼錯誤", "#ff8c3b", "WRONG_PASSWORD"),
}

QSS = """
* { font-family: 'IBM Plex Mono','SF Mono',Menlo,monospace; color:#c9d6d1; }
QWidget#root { background:#0b1110; }
QLabel#kicker { color:#39e0a0; font-weight:600; letter-spacing:3px; }
QLabel#title  { color:#e8efec; font-size:22px; font-weight:700; }
QLabel.h2 { color:#6f827c; font-weight:600; letter-spacing:3px; }
QFrame.card { background:#0e1715; border:1px solid #1c2a27; border-radius:6px; }
QLineEdit, QListWidget, QTextEdit {
    background:#060c0b; border:1px solid #1c2a27; border-radius:4px; padding:7px; color:#c9d6d1;
    selection-background-color:#1c8f66;
}
QLineEdit:focus { border:1px solid #1c8f66; }
QListWidget::item:selected { background:#13322a; color:#39e0a0; }
QPushButton {
    background:#11201b; border:1px solid #1c8f66; border-radius:4px; color:#39e0a0;
    padding:9px 14px; font-weight:600; letter-spacing:1px;
}
QPushButton:hover { background:#14271f; }
QPushButton:disabled { color:#46544f; border-color:#1c2a27; background:#0a1211; }
QPushButton#alt { color:#9fb0aa; border-color:#1c2a27; background:#0a1211; }
QPushButton#alt:hover { color:#e8efec; }
QCheckBox { color:#6f827c; }
QLabel#ip { color:#39e0a0; }
QLabel#code { color:#6f827c; letter-spacing:2px; }
QLabel#stext { font-size:20px; font-weight:700; color:#e8efec; }
"""


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("AMB82 · BLE Wi-Fi 配網測試台")
        self.resize(1040, 720)
        self.client = None
        self.devices = []
        self.ip = ""
        self._build()
        self.setStyleSheet(QSS)
        self._set_status(0)
        self._set_connected(False)
        # office presence: listen for the firmware's UDP broadcast on :48555
        self._office_q = queue.Queue()
        self._start_office_listener()
        self._otimer = QTimer(self)
        self._otimer.timeout.connect(self._drain_office)
        self._otimer.start(200)

    # ---------- UI ----------
    def _card(self, title):
        f = QFrame(); f.setProperty("class", "card")
        f.setFrameShape(QFrame.NoFrame)
        v = QVBoxLayout(f); v.setContentsMargins(16, 14, 16, 16); v.setSpacing(10)
        h = QLabel(title); h.setProperty("class", "h2")
        v.addWidget(h)
        return f, v

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(20, 18, 20, 18); root.setSpacing(14)

        # header
        head = QVBoxLayout(); head.setSpacing(3)
        k = QLabel("JET-OPTO · AMB82-MINI"); k.setObjectName("kicker")
        t = QLabel("BLE Wi-Fi 配網測試台"); t.setObjectName("title")
        head.addWidget(k); head.addWidget(t)
        root.addLayout(head)

        cols = QHBoxLayout(); cols.setSpacing(14); root.addLayout(cols, 1)
        left = QVBoxLayout(); left.setSpacing(14); cols.addLayout(left, 5)
        right = QVBoxLayout(); right.setSpacing(14); cols.addLayout(right, 7)

        # --- device card ---
        dc, dv = self._card("裝置  ·  DEVICE")
        self.lst = QListWidget(); self.lst.setMinimumHeight(120)
        self.lst.itemDoubleClicked.connect(lambda *_: self.on_connect())
        dv.addWidget(self.lst)
        row = QHBoxLayout()
        self.btn_scan = QPushButton("掃描"); self.btn_scan.clicked.connect(self.on_scan)
        self.btn_conn = QPushButton("連線"); self.btn_conn.clicked.connect(self.on_connect)
        self.btn_disc = QPushButton("斷線"); self.btn_disc.setObjectName("alt"); self.btn_disc.clicked.connect(self.on_disconnect)
        row.addWidget(self.btn_scan); row.addWidget(self.btn_conn); row.addWidget(self.btn_disc)
        dv.addLayout(row)
        left.addWidget(dc)

        # --- creds card ---
        cc, cv = self._card("Wi-Fi 憑證  ·  CREDENTIALS")
        cv.addWidget(QLabel("SSID"))
        self.ed_ssid = QLineEdit(); self.ed_ssid.setMaxLength(32); self.ed_ssid.setPlaceholderText("Wi-Fi 名稱")
        cv.addWidget(self.ed_ssid)
        cv.addWidget(QLabel("密碼"))
        pr = QHBoxLayout()
        self.ed_pass = QLineEdit(); self.ed_pass.setMaxLength(64)
        self.ed_pass.setEchoMode(QLineEdit.Password); self.ed_pass.setPlaceholderText("留空 = 開放網路")
        self.cb_show = QCheckBox("顯示"); self.cb_show.toggled.connect(
            lambda c: self.ed_pass.setEchoMode(QLineEdit.Normal if c else QLineEdit.Password))
        pr.addWidget(self.ed_pass, 1); pr.addWidget(self.cb_show)
        cv.addLayout(pr)
        self.btn_send = QPushButton("送出並連線  ▸"); self.btn_send.clicked.connect(self.on_send)
        cv.addWidget(self.btn_send)
        left.addWidget(cc)

        # --- status card ---
        sc, sv = self._card("狀態  ·  STATUS")
        srow = QHBoxLayout(); srow.setSpacing(16)
        self.led = QLabel(); self.led.setFixedSize(46, 46)
        st = QVBoxLayout(); st.setSpacing(3)
        self.lbl_st = QLabel("待命"); self.lbl_st.setObjectName("stext")
        self.lbl_code = QLabel("STATUS — 0x00"); self.lbl_code.setObjectName("code")
        self.lbl_ip = QLabel(""); self.lbl_ip.setObjectName("ip")
        st.addWidget(self.lbl_st); st.addWidget(self.lbl_code); st.addWidget(self.lbl_ip)
        srow.addWidget(self.led); srow.addLayout(st, 1)
        sv.addLayout(srow)
        left.addWidget(sc)

        # --- office presence card (UDP 48555 from office firmware) ---
        oc, ov = self._card("Office 在場事件  ·  PRESENCE")
        self.lbl_ostate = QLabel("等待 UDP 事件…(板子需跑 office 韌體)")
        self.lbl_ostate.setObjectName("stext")
        ov.addWidget(self.lbl_ostate)
        og = QGridLayout(); og.setSpacing(8)
        self.lbl_faces = QLabel("—")
        self.lbl_known = QLabel("—"); self.lbl_known.setObjectName("ip")
        self.lbl_unknown = QLabel("—")
        for r, (cap, w) in enumerate([("臉數 faces", self.lbl_faces),
                                      ("已辨識 known", self.lbl_known),
                                      ("陌生 unknown", self.lbl_unknown)]):
            c = QLabel(cap); c.setObjectName("code")
            og.addWidget(c, r, 0); og.addWidget(w, r, 1)
        ov.addLayout(og)
        left.addWidget(oc)
        left.addStretch(1)

        # --- RTSP card (right) ---
        rc, rv = self._card("RTSP 串流  ·  PREVIEW")
        self.video = QVideoWidget()
        self.video.setMinimumHeight(330)
        self.video.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video.setStyleSheet("background:#000;border:1px solid #1c2a27;border-radius:4px;")
        rv.addWidget(self.video, 1)
        self.player = QMediaPlayer()
        self.audio = QAudioOutput(); self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.errorOccurred.connect(
            lambda e, s: self.log(f"播放器錯誤:{s}(改用 ffplay)", "err") if e else None)

        urow = QHBoxLayout()
        self.lbl_url = QLabel("rtsp://—:554"); self.lbl_url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        urow.addWidget(self.lbl_url, 1)
        self.btn_play = QPushButton("▶ 內嵌播放"); self.btn_play.clicked.connect(self.on_play)
        self.btn_ffplay = QPushButton("ffplay"); self.btn_ffplay.setObjectName("alt"); self.btn_ffplay.clicked.connect(self.on_ffplay)
        urow.addWidget(self.btn_play); urow.addWidget(self.btn_ffplay)
        rv.addLayout(urow)
        right.addWidget(rc, 3)

        # --- log ---
        lc, lv = self._card("LOG")
        self.txt = QTextEdit(); self.txt.setReadOnly(True); self.txt.setMinimumHeight(150)
        lv.addWidget(self.txt)
        right.addWidget(lc, 2)

        self.log("就緒 — 按「掃描」找 Ameba_ 裝置", "ok")

    # ---------- helpers ----------
    def log(self, msg, kind="i"):
        color = {"i": "#9fb0aa", "ok": "#39e0a0", "err": "#ff5a52",
                 "tx": "#7cc7ff", "rx": "#39e0a0", "warn": "#f5b73d"}.get(kind, "#9fb0aa")
        self.txt.append(f'<span style="color:#46544f">{self._ts()}</span>  '
                        f'<span style="color:{color}">{msg}</span>')

    def _ts(self):
        import time
        return time.strftime("%H:%M:%S")

    def _set_status(self, code):
        name, color, en = STATUS.get(code, ("未知", "#ff5a52", "?"))
        self.lbl_st.setText(name)
        self.lbl_code.setText(f"STATUS — 0x{code:02x}  {en}")
        self.led.setStyleSheet(
            f"background:{color}; border-radius:23px; border:1px solid rgba(255,255,255,.1);")

    def _set_connected(self, on):
        self.btn_conn.setEnabled(not on)
        self.btn_disc.setEnabled(on)
        self.btn_send.setEnabled(on)
        self.btn_scan.setEnabled(not on)

    def _selected_device(self):
        i = self.lst.currentRow()
        if 0 <= i < len(self.devices):
            return self.devices[i]
        return None

    # ---------- BLE (async) ----------
    @asyncSlot()
    async def on_scan(self):
        self.btn_scan.setEnabled(False)
        self.lst.clear(); self.devices = []
        self.log("掃描中(6 秒)…", "i")
        try:
            found = await BleakScanner.discover(timeout=6.0)
        except Exception as e:
            self.log(f"掃描失敗:{e}", "err")
            if sys.platform == "darwin":
                self.log("macOS:系統設定 → 隱私權與安全性 → 藍牙,允許終端機/Python", "warn")
            elif sys.platform.startswith("linux"):
                self.log("Linux:bluetoothctl list 看有無 controller;"
                         "sudo systemctl enable --now bluetooth;"
                         "sudo rfkill unblock bluetooth;bluetoothctl power on", "warn")
            else:
                self.log("確認本機有可用的藍牙介面", "warn")
            self.btn_scan.setEnabled(True)
            return
        for d in found:
            name = d.name or ""
            if name.startswith(NAME_PREFIX):
                self.devices.append(d)
                self.lst.addItem(QListWidgetItem(f"{name}    [{d.address}]"))
        if self.devices:
            self.lst.setCurrentRow(0)
            self.log(f"找到 {len(self.devices)} 個 {NAME_PREFIX}* 裝置", "ok")
        else:
            self.log(f"沒找到 {NAME_PREFIX}* 裝置(裝置需在配網模式 / 先 QRCLR)", "warn")
        self.btn_scan.setEnabled(True)

    @asyncSlot()
    async def on_connect(self):
        dev = self._selected_device()
        if not dev:
            self.log("請先選一個裝置", "warn"); return
        self.btn_conn.setEnabled(False)
        self.log(f"連線 {dev.name} …", "i")
        try:
            self.client = BleakClient(dev, disconnected_callback=self._on_disc)
            await self.client.connect()
            await self.client.start_notify(UUID_STATUS, self._on_status)
            await self.client.start_notify(UUID_IP, self._on_ip)
            self.log("已連線 + 訂閱 STATUS/IP notify", "ok")
            try:
                self._handle_status((await self.client.read_gatt_char(UUID_STATUS))[0])
            except Exception:
                pass
            self._set_connected(True)
        except Exception as e:
            self.log(f"連線失敗:{e}", "err")
            self.client = None
            self.btn_conn.setEnabled(True)

    @asyncSlot()
    async def on_disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    def _on_disc(self, _client):
        self.log("裝置已斷線", "warn")
        self._set_connected(True if False else False)
        self._set_status(0); self.lbl_ip.setText("")
        self.client = None

    @asyncSlot()
    async def on_send(self):
        if not (self.client and self.client.is_connected):
            self.log("尚未連線", "warn"); return
        ssid = self.ed_ssid.text(); pw = self.ed_pass.text()
        if not ssid:
            self.log("SSID 不可為空", "err"); return
        if len(ssid.encode()) > 32 or len(pw.encode()) > 64:
            self.log("SSID/密碼過長", "err"); return
        try:
            self.btn_send.setEnabled(False)
            self.log("── 開始配網 ──", "i")
            await self.client.write_gatt_char(UUID_SSID, ssid.encode(), response=True)
            self.log(f'▶ SSID = "{ssid}"', "tx")
            await self.client.write_gatt_char(UUID_PASS, pw.encode(), response=True)
            self.log(f'▶ PASS = {"•"*len(pw) if pw else "(空/開放)"}', "tx")
            await self.client.write_gatt_char(UUID_CTRL, bytes([0x01]), response=True)
            self.log("▶ CTRL = 0x01 (connect)，等待 STATUS…", "tx")
        except Exception as e:
            self.log(f"寫入失敗:{e}", "err")
        finally:
            self.btn_send.setEnabled(bool(self.client and self.client.is_connected))

    def _on_status(self, _sender, data: bytearray):
        if data:
            self._handle_status(data[0])

    def _handle_status(self, code):
        self._set_status(code)
        en = STATUS.get(code, ("", "", "?"))[2]
        self.log(f"◀ STATUS 0x{code:02x}  {en}", "rx")
        if code == 2:
            self.lbl_ip.setText("✓ 已連上 — 裝置即將存檔重開,RTSP 上線")

    def _on_ip(self, _sender, data: bytearray):
        ip = bytes(data).decode(errors="ignore").strip("\x00").strip()
        if not ip:
            return
        self.ip = ip
        self.lbl_ip.setText(f"✓ {ip}  —  rtsp://{ip}:554")
        self.lbl_url.setText(f"rtsp://{ip}:554")
        self.log(f"◀ IP {ip}", "rx")

    # ---------- office presence (UDP 48555) ----------
    def _start_office_listener(self, port=48555):
        def run():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except Exception:
                    pass
                s.bind(("", port))
            except Exception as e:
                self._office_q.put(("err", str(e)))
                return
            while True:
                try:
                    data, addr = s.recvfrom(2048)
                    self._office_q.put(("ev", addr[0], data))
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True).start()

    def _drain_office(self):
        last = None
        while True:
            try:
                item = self._office_q.get_nowait()
            except queue.Empty:
                break
            if item[0] == "err":
                self.lbl_ostate.setText(f"UDP 監聽失敗:{item[1]}（48555 是否被佔用？）")
                continue
            last = item
        if not last:
            return
        _, ip, data = last
        try:
            ev = json.loads(data.decode("utf-8", "replace"))
        except Exception:
            return
        if ev.get("dev") != "amb82-office":
            return
        faces = ev.get("faces", 0)
        known = ev.get("known", []) or []
        unknown = ev.get("unknown", 0)
        self.lbl_faces.setText(str(faces))
        self.lbl_known.setText("、".join(known) if known else "—")
        self.lbl_unknown.setText(str(unknown))
        if known:
            self.lbl_ostate.setText(f"✓ 辨識到:{'、'.join(known)}")
        elif faces:
            self.lbl_ostate.setText("有臉但未辨識(unknown)")
        else:
            self.lbl_ostate.setText("無人")
        # learn camera IP from the broadcast -> auto-fill RTSP url (BLE IP wins if set)
        if ip and not self.ip:
            self.ip = ip
            self.lbl_url.setText(f"rtsp://{ip}:554")

    # ---------- RTSP ----------
    def _url(self):
        return self.lbl_url.text().strip()

    def on_play(self):
        url = self._url()
        if "—" in url:
            self.log("還沒有 IP(配網成功後會自動帶入,或手動改 rtsp URL)", "warn"); return
        self.log(f"內嵌播放 {url}", "i")
        self.player.setSource(QUrl(url))
        self.player.play()

    def on_ffplay(self):
        url = self._url()
        if "—" in url:
            self.log("還沒有 IP", "warn"); return
        exe = shutil.which("ffplay")
        if not exe:
            self.log("找不到 ffplay(brew install ffmpeg)", "err"); return
        subprocess.Popen([exe, "-rtsp_transport", "tcp", "-fflags", "nobuffer",
                          "-flags", "low_delay", url])
        self.log(f"已用 ffplay 開啟 {url}", "ok")


def main():
    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    w = MainWindow(); w.show()
    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
