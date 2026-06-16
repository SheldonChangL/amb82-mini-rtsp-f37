#!/usr/bin/env python3
"""
AMB82 Office · 臉部在場鎖定桌面應用 (Python / PySide6 / bleak)
==============================================================

把 AMB82-mini AI 攝影機變成「辦公桌在席感測器」的完整桌面產品:
  - BLE Wi-Fi 配網(掃描 / 連線 / 寫 SSID·PASS·CTRL / 收 STATUS·IP notify)
  - 內嵌 RTSP 預覽(QMediaPlayer,FFmpeg 後端)+ 外部 ffplay
  - Office 在場事件監看(韌體 UDP 廣播 :48555)
  - 臉部鎖定控制台:GUI 直接驅動 office_daemon 的 PresenceEngine(離開上鎖、
    主人回來解鎖、陌生人鎖、偷看警告、訪客門鈴、出勤、番茄鐘、縮時、稽核、DnD)

鎖定/解鎖的全部邏輯都「重用」office_daemon.py(PresenceEngine),本檔只負責
GUI 與事件串接,不重寫任何鎖機邏輯。

需求:
    python3 -m pip install PySide6 qasync bleak
    # RTSP 內嵌播放需 Qt FFmpeg 後端(PySide6 6.5+ 內建);外部播放需系統有 ffplay
    # 快照(稽核/縮時)選用系統 ffmpeg,沒有就自動略過

執行:
    python3 amb82_office.py

⚠️ 安全:主開關預設「關」=僅監看不鎖。打開才會真的鎖/解鎖。隨時可按
「立即停用 (DISARM)」按鈕,會在 ~/amb82-office/DISARM 建檔,即使程式失控也會
被引擎內建的失效保險擋住(刪掉該檔才會重新啟用)。

協定(對應韌體 wifi_prov_service.c,16-bit UUID):
    Service 0xA100 · SSID(W) 0xA101 · PASS(W) 0xA102 · CTRL(W) 0xA103
    STATUS(R/N) 0xA104 · IP(R/N) 0xA105
"""
import os
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")  # enable RTSP in QMediaPlayer

import sys
import time
import asyncio
import subprocess
import shutil
import json
import socket
import threading
import queue

try:
    from PySide6.QtCore import Qt, QUrl, QTimer
    from PySide6.QtWidgets import (
        QApplication, QWidget, QLabel, QLineEdit, QPushButton, QListWidget,
        QListWidgetItem, QVBoxLayout, QHBoxLayout, QGridLayout, QTextEdit,
        QCheckBox, QFrame, QSizePolicy, QSpinBox, QDoubleSpinBox, QScrollArea,
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

# ---- 重用 daemon 引擎:鎖定/解鎖邏輯一律不重寫 -------------------------
import office_daemon
from office_daemon import (
    PresenceEngine, make_controller, CONFIG, base_dir, ensure_dir, notify,
)

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

# 臉部鎖定控制台要綁的功能開關(CONFIG key, 繁中標籤)
FEATURES = [
    ("FEAT_PRESENCE_LOCK",     "在席鎖屏(核心)"),
    ("FEAT_FOREIGN_FACE_LOCK", "陌生人鎖(主人不在卻有陌生臉)"),
    ("FEAT_SHOULDER_SURFER",   "偷看肩膀警告"),
    ("FEAT_VISITOR_DOORBELL",  "訪客門鈴通知"),
    ("FEAT_ATTENDANCE_LOG",    "出勤紀錄 CSV"),
    ("FEAT_POMODORO",          "番茄鐘(久坐提醒)"),
    ("FEAT_TIMELAPSE",         "縮時快照"),
    ("FEAT_AUDIT_PHOTOS",      "稽核存證快照"),
    ("FEAT_DND_STATUS",        "勿擾狀態鉤子(DnD)"),
]

QSS = """
* { font-family: 'IBM Plex Mono','SF Mono',Menlo,monospace; color:#c9d6d1; }
QWidget#root { background:#0b1110; }
QScrollArea, QScrollArea > QWidget > QWidget { background:transparent; }
QLabel#kicker { color:#39e0a0; font-weight:600; letter-spacing:3px; }
QLabel#title  { color:#e8efec; font-size:22px; font-weight:700; }
QLabel#sub    { color:#6f827c; letter-spacing:1px; }
QLabel.h2 { color:#6f827c; font-weight:600; letter-spacing:3px; }
QFrame.card { background:#0e1715; border:1px solid #1c2a27; border-radius:6px; }
QLineEdit, QListWidget, QTextEdit, QSpinBox, QDoubleSpinBox {
    background:#060c0b; border:1px solid #1c2a27; border-radius:4px; padding:7px; color:#c9d6d1;
    selection-background-color:#1c8f66;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border:1px solid #1c8f66; }
QListWidget::item:selected { background:#13322a; color:#39e0a0; }
QPushButton {
    background:#11201b; border:1px solid #1c8f66; border-radius:4px; color:#39e0a0;
    padding:9px 14px; font-weight:600; letter-spacing:1px;
}
QPushButton:hover { background:#14271f; }
QPushButton:disabled { color:#46544f; border-color:#1c2a27; background:#0a1211; }
QPushButton#alt { color:#9fb0aa; border-color:#1c2a27; background:#0a1211; }
QPushButton#alt:hover { color:#e8efec; }
QPushButton#danger { color:#ff5a52; border-color:#5a1f1c; background:#1a0e0d; }
QPushButton#danger:hover { background:#2a1311; color:#ff7d76; }
QCheckBox { color:#9fb0aa; }
QCheckBox#master { color:#e8efec; font-size:15px; font-weight:700; letter-spacing:1px; }
QLabel#ip { color:#39e0a0; }
QLabel#code { color:#6f827c; letter-spacing:2px; }
QLabel#stext { font-size:20px; font-weight:700; color:#e8efec; }
QLabel#armOn  { color:#39e0a0; font-weight:700; }
QLabel#armOff { color:#6f827c; font-weight:700; }
"""


class GuiCameraLink:
    """給 PresenceEngine 用的輕量 link 介面卡。

    GUI 已自行綁定 UDP 48555(office 監看),不能再用 daemon 的 CameraLink
    去重綁同一個 port。這裡只提供 PresenceEngine 會呼叫的三個方法:
      - get_ip():從 UDP 封包來源學到的攝影機 IP
      - set_led(state, dry):no-op(GUI 沒有送指令的 socket,直接記在 log)
      - seconds_since_packet():距離最後一筆 UDP 封包的秒數
    """

    def __init__(self):
        self._ip = None
        self._last_ts = 0.0  # time.monotonic() of last packet
        self._lock = threading.Lock()

    def note_packet(self, ip):
        """_drain_office 收到封包時呼叫:更新 IP 與存活時間。"""
        with self._lock:
            self._ip = ip
            self._last_ts = time.monotonic()

    def get_ip(self):
        with self._lock:
            return self._ip

    def seconds_since_packet(self):
        with self._lock:
            if not self._last_ts:
                return float("inf")
            return time.monotonic() - self._last_ts

    def set_led(self, state, dry):
        # 沒有回送 socket → 僅記錄(韌體端 LED 推送為未來功能)
        office_daemon.log(f"[GUI link] would set LED → {state}")


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("AMB82 Office · 臉部在場鎖定")
        self.resize(1180, 800)
        self.client = None
        self.devices = []
        self.ip = ""

        # 臉部鎖定引擎(重用 daemon)。armed 預設 False = 僅監看不鎖。
        self.link = GuiCameraLink()
        self.engine = PresenceEngine(
            self.link, make_controller(dry_run=False), dry_run=False, armed=False)
        self._last_engine_tick = 0.0          # engine.tick()/feed(None) 的 ~1s 節流
        self._prev_present = None             # 偵測在席狀態轉變以寫 GUI log
        self._prev_locked = None              # 偵測螢幕鎖狀態轉變以寫 GUI log

        self._build()
        self.setStyleSheet(QSS)
        self._set_status(0)
        self._set_connected(False)

        # 讓 daemon 的 log() 也回灌到 GUI log(ARRIVED/LOCK/UNLOCK… 即時可見)
        self._patch_daemon_log()

        # office presence: 監聽韌體的 UDP 廣播 :48555
        self._office_q = queue.Queue()
        self._start_office_listener()
        self._otimer = QTimer(self)
        self._otimer.timeout.connect(self._drain_office)
        self._otimer.start(200)

        self._refresh_lock_status()

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

        # header(產品化品牌,移除「測試台」字樣)
        head = QVBoxLayout(); head.setSpacing(3)
        k = QLabel("JET-OPTO · AMB82-MINI"); k.setObjectName("kicker")
        t = QLabel("AMB82 Office"); t.setObjectName("title")
        s = QLabel("臉部在場鎖定 · 桌面應用"); s.setObjectName("sub")
        head.addWidget(k); head.addWidget(t); head.addWidget(s)
        root.addLayout(head)

        cols = QHBoxLayout(); cols.setSpacing(14); root.addLayout(cols, 1)

        # 左欄放進可捲動區域(卡片較多,避免擠壓)
        left_inner = QWidget(); left = QVBoxLayout(left_inner)
        left.setContentsMargins(0, 0, 6, 0); left.setSpacing(14)
        left_scroll = QScrollArea(); left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setWidget(left_inner)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        cols.addWidget(left_scroll, 5)

        right = QVBoxLayout(); right.setSpacing(14); cols.addLayout(right, 7)

        # --- device card ---
        dc, dv = self._card("裝置  ·  DEVICE")
        self.lst = QListWidget(); self.lst.setMinimumHeight(96)
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
        sc, sv = self._card("配網狀態  ·  STATUS")
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

        # --- 臉部鎖定控制台(本應用的核心新功能)---
        self._build_facelock_card(left)

        # --- office presence card (UDP 48555 from office firmware) ---
        oc, ov = self._card("Office 在場事件  ·  PRESENCE")
        self.lbl_ostate = QLabel("等待 UDP 事件…(板子需跑 office 韌體)")
        self.lbl_ostate.setObjectName("stext")
        self.lbl_ostate.setWordWrap(True)
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

        self.log("就緒 — 鎖定預設關閉(僅監看)。按「掃描」找 Ameba_ 裝置", "ok")

    def _build_facelock_card(self, parent_layout):
        """臉部鎖定控制台:主開關 + 參數 + 功能開關 + 即時狀態 + DISARM。"""
        fc, fv = self._card("臉部鎖定  ·  FACE LOCK")

        # 1) 主開關 ── 預設關閉(僅監看,絕不上鎖)
        mrow = QHBoxLayout()
        self.cb_arm = QCheckBox("啟用鎖定"); self.cb_arm.setObjectName("master")
        self.cb_arm.setChecked(False)
        self.cb_arm.toggled.connect(self._on_arm_toggled)
        self.lbl_arm = QLabel("僅監看不鎖"); self.lbl_arm.setObjectName("armOff")
        mrow.addWidget(self.cb_arm); mrow.addStretch(1); mrow.addWidget(self.lbl_arm)
        fv.addLayout(mrow)

        # 2~4) 參數
        grid = QGridLayout(); grid.setSpacing(8)
        grid.addWidget(QLabel("主人名字(需與登錄臉相符)"), 0, 0)
        self.ed_owner = QLineEdit(CONFIG["OWNER_NAMES"][0] if CONFIG["OWNER_NAMES"] else "")
        self.ed_owner.setPlaceholderText("例如 sheldon")
        self.ed_owner.textChanged.connect(self._on_owner_changed)
        grid.addWidget(self.ed_owner, 0, 1)

        grid.addWidget(QLabel("離開幾秒上鎖"), 1, 0)
        self.sp_absence = QSpinBox(); self.sp_absence.setRange(5, 600)
        self.sp_absence.setValue(int(CONFIG["ABSENCE_LOCK_SEC"]))
        self.sp_absence.setSuffix(" 秒")
        self.sp_absence.valueChanged.connect(
            lambda v: CONFIG.__setitem__("ABSENCE_LOCK_SEC", int(v)))
        grid.addWidget(self.sp_absence, 1, 1)

        grid.addWidget(QLabel("解鎖去抖秒數"), 2, 0)
        self.sp_debounce = QDoubleSpinBox(); self.sp_debounce.setRange(0.1, 5.0)
        self.sp_debounce.setSingleStep(0.1); self.sp_debounce.setDecimals(1)
        self.sp_debounce.setValue(float(CONFIG["PRESENCE_DEBOUNCE_SEC"]))
        self.sp_debounce.setSuffix(" 秒")
        self.sp_debounce.valueChanged.connect(
            lambda v: CONFIG.__setitem__("PRESENCE_DEBOUNCE_SEC", float(v)))
        grid.addWidget(self.sp_debounce, 2, 1)
        fv.addLayout(grid)

        # 5) 功能開關
        feat_cap = QLabel("功能開關"); feat_cap.setObjectName("code")
        fv.addWidget(feat_cap)
        fgrid = QGridLayout(); fgrid.setSpacing(4)
        for i, (key, label) in enumerate(FEATURES):
            cb = QCheckBox(label)
            cb.setChecked(bool(CONFIG.get(key, False)))
            cb.toggled.connect(lambda c, k=key: CONFIG.__setitem__(k, bool(c)))
            fgrid.addWidget(cb, i // 2, i % 2)
        fv.addLayout(fgrid)

        # 6) 即時狀態列
        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#1c2a27;")
        fv.addWidget(sep)
        live = QHBoxLayout(); live.setSpacing(20)
        self.lbl_present = QLabel("在席 —"); self.lbl_present.setObjectName("stext")
        self.lbl_screen = QLabel("螢幕 —"); self.lbl_screen.setObjectName("stext")
        live.addWidget(self.lbl_present); live.addWidget(self.lbl_screen); live.addStretch(1)
        fv.addLayout(live)

        # 7) 立即停用 (DISARM)
        self.btn_disarm = QPushButton("立即停用 (DISARM)")
        self.btn_disarm.setObjectName("danger")
        self.btn_disarm.clicked.connect(self.on_disarm)
        fv.addWidget(self.btn_disarm)
        note = QLabel(f"DISARM 會在 {os.path.join(str(base_dir()), 'DISARM')} 建檔即時停止上鎖;"
                      "刪掉該檔才會重新啟用。")
        note.setObjectName("code"); note.setWordWrap(True)
        fv.addWidget(note)

        parent_layout.addWidget(fc)

    # ---------- 臉部鎖定控制 ----------
    def _on_arm_toggled(self, on):
        self.engine.armed = bool(on)
        if on:
            self.lbl_arm.setText("已啟用:會真的鎖/解鎖"); self.lbl_arm.setObjectName("armOn")
            self.log("臉部鎖定【已啟用】— 主人離開會真的鎖屏", "warn")
        else:
            self.lbl_arm.setText("僅監看不鎖"); self.lbl_arm.setObjectName("armOff")
            self.log("臉部鎖定【已停用】— 僅監看不會上鎖", "i")
        # 重新套用 objectName 對應的 QSS 樣式
        self.lbl_arm.setStyleSheet("")
        self.lbl_arm.style().unpolish(self.lbl_arm)
        self.lbl_arm.style().polish(self.lbl_arm)

    def _on_owner_changed(self, text):
        text = text.strip()
        CONFIG["OWNER_NAMES"] = [text] if text else []

    def on_disarm(self):
        """硬停用:取消 armed、收起主開關、並建立 DISARM 失效保險檔。"""
        self.engine.armed = False
        self.cb_arm.setChecked(False)   # 連帶觸發 _on_arm_toggled 更新標籤
        try:
            ensure_dir(base_dir())
            path = os.path.join(str(base_dir()), "DISARM")
            with open(path, "w") as f:
                f.write("disarmed by GUI\n")
            self.log(f"已寫入 DISARM 失效保險檔:{path}(刪掉才會重新啟用)", "err")
        except Exception as e:
            self.log(f"寫入 DISARM 檔失敗:{e}", "err")

    def _refresh_lock_status(self):
        """由 _drain_office 觸發:更新在席/螢幕鎖即時狀態 + log 轉變。"""
        present = self.engine.owner_present
        locked = self.engine._screen_locked
        self.lbl_present.setText("在席 ✓ present" if present else "在席 ✗ away")
        self.lbl_present.setStyleSheet("color:#39e0a0;" if present else "color:#6f827c;")
        self.lbl_screen.setText("螢幕 已鎖" if locked else "螢幕 未鎖")
        self.lbl_screen.setStyleSheet("color:#ff8c3b;" if locked else "color:#39e0a0;")
        # 轉變才寫 log(避免每 200ms 洗版)
        if self._prev_present is not None and present != self._prev_present:
            self.log("主人到達(present)" if present else "主人離開(away)",
                     "ok" if present else "warn")
        if self._prev_locked is not None and locked != self._prev_locked:
            self.log("螢幕已鎖" if locked else "螢幕已解鎖",
                     "warn" if locked else "ok")
        self._prev_present = present
        self._prev_locked = locked

    # ---------- daemon log → GUI log 橋接 ----------
    def _patch_daemon_log(self):
        """monkeypatch office_daemon.log,讓引擎動作(LOCK/UNLOCK/ARRIVED…)
        也出現在 GUI 的 LOG;同時保留原本的 stdout 輸出。"""
        orig = office_daemon.log

        def patched(msg, level="INFO"):
            orig(msg, level)
            kind = {"WARN": "warn", "ERROR": "err"}.get(level, "i")
            # 跨執行緒安全:用 single-shot timer 丟回 GUI thread
            try:
                QTimer.singleShot(0, lambda m=msg, k=kind: self.log(f"[engine] {m}", k))
            except Exception:
                pass
        office_daemon.log = patched

    # ---------- helpers ----------
    def log(self, msg, kind="i"):
        color = {"i": "#9fb0aa", "ok": "#39e0a0", "err": "#ff5a52",
                 "tx": "#7cc7ff", "rx": "#39e0a0", "warn": "#f5b73d"}.get(kind, "#9fb0aa")
        self.txt.append(f'<span style="color:#46544f">{self._ts()}</span>  '
                        f'<span style="color:{color}">{msg}</span>')

    def _ts(self):
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
        self._set_connected(False)
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
        """200ms 定時:消化 UDP 佇列、更新監看標籤、餵事件給 PresenceEngine。"""
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

        if last:
            _, ip, data = last
            try:
                ev = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                ev = None
            if isinstance(ev, dict) and ev.get("dev") == "amb82-office":
                faces = ev.get("faces", 0) or 0
                known = ev.get("known", []) or []
                unknown = ev.get("unknown", 0) or 0
                # 更新監看標籤
                self.lbl_faces.setText(str(faces))
                self.lbl_known.setText("、".join(known) if known else "—")
                self.lbl_unknown.setText(str(unknown))
                if known:
                    self.lbl_ostate.setText(f"✓ 辨識到:{'、'.join(known)}")
                elif faces:
                    self.lbl_ostate.setText("有臉但未辨識(unknown)")
                else:
                    self.lbl_ostate.setText("無人")
                # 學到攝影機 IP → 自動帶入 RTSP url(BLE IP 優先)
                if ip and not self.ip:
                    self.ip = ip
                    self.lbl_url.setText(f"rtsp://{ip}:554")
                # 更新 link 存活時間 + IP,並把事件餵給引擎
                self.link.note_packet(ip)
                self.engine.feed({
                    "faces": int(faces),
                    "known": [str(x) for x in known],
                    "unknown": int(unknown),
                    "ts": int(ev.get("ts", 0) or 0),
                })

        # 沒有事件時也要推進時間:約 1 秒一次 tick() + feed(None)(處理攝影機失聯)
        now = time.monotonic()
        if now - self._last_engine_tick >= 1.0:
            self._last_engine_tick = now
            try:
                if last is None:
                    self.engine.feed(None)
                self.engine.tick()
            except Exception as e:
                self.log(f"引擎 tick 錯誤:{e}", "err")

        self._refresh_lock_status()

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
