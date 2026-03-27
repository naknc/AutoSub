from __future__ import annotations

import json
import os
import re
import shutil
import struct
import sys
import traceback
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
	os.environ["QT_QPA_PLATFORM"] = "xcb"

from dotenv import load_dotenv
from PyQt6 import QtCore, QtGui, QtNetwork, QtWidgets
import requests
import vlc
import whisper

load_dotenv()
OS_API = "https://api.opensubtitles.com/api/v1"
OS_KEY = os.environ["OPENSUBTITLES_API_KEY"]
OS_USER = os.environ["OPENSUBTITLES_USERNAME"]
OS_PASS = os.environ["OPENSUBTITLES_PASSWORD"]
LANG_MAP = {
	"eng": "en",
	"tur": "tr",
	"fra": "fr",
	"deu": "de",
	"spa": "es",
	"ita": "it",
	"por": "pt",
	"jpn": "ja",
	"kor": "ko",
	"rus": "ru",
}

def is_linux():
	return sys.platform.startswith("linux")


def base_font_family():
	if sys.platform == "darwin":
		return "'SF Pro Text','Inter','Avenir Next','Helvetica Neue',sans-serif"
	if is_linux():
		return "'Noto Sans','Inter','Ubuntu','Cantarell','DejaVu Sans',sans-serif"
	return "'Segoe UI','Inter','Helvetica Neue',sans-serif"


def build_style():
	radius = 14 if is_linux() else 18
	video_radius = 16 if is_linux() else 22
	control_padding = "8px 12px" if is_linux() else "9px 14px"
	item_padding = "8px 10px" if is_linux() else "10px 12px"
	input_padding = "7px 9px" if is_linux() else "8px 10px"
	return (
		f"QWidget{{font-family:{base_font_family()};font-size:13px;color:#E5E7EB;"
		"selection-background-color:#2563EB;selection-color:#F8FAFC}"
		"QMainWindow{background:#0F172A}"
		f"QFrame#playlistFrame,QFrame#syncFrame,QFrame#controlCard,QFrame#progressCard{{"
		f"background:#111827;border:1px solid #1F2937;border-radius:{radius}px}}"
		f"QFrame#videoFrame{{background:#020617;border:1px solid #1E293B;border-radius:{video_radius}px}}"
		f"QPushButton{{background:#2563EB;color:#F8FAFC;border:none;border-radius:10px;padding:{control_padding};font-weight:600}}"
		"QPushButton:hover{background:#1D4ED8}"
		"QPushButton:pressed{background:#1E40AF}"
		"QPushButton:disabled{background:#334155;color:#94A3B8}"
		"QPushButton#secondaryButton{background:#1F2937;color:#E5E7EB;border:1px solid #334155}"
		"QPushButton#secondaryButton:hover{background:#273449}"
		"QPushButton#dangerButton{background:#7F1D1D;color:#FEE2E2;border:1px solid #991B1B}"
		"QPushButton#dangerButton:hover{background:#991B1B}"
		"QLabel{font-size:12px;color:#CBD5E1}"
		"QLabel#titleLabel{font-size:16px;font-weight:700;color:#F8FAFC}"
		"QLabel#sectionLabel{font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#94A3B8}"
		"QLabel#mutedLabel{color:#94A3B8}"
		"QLabel#statusLabel{color:#E2E8F0;font-weight:600}"
		"QLabel#badgeLabel{background:#172554;color:#BFDBFE;border:1px solid #1D4ED8;border-radius:999px;padding:4px 10px;font-weight:700}"
		"QSlider::groove:horizontal{height:8px;background:#1E293B;border-radius:4px}"
		"QSlider::handle:horizontal{background:#E2E8F0;width:16px;height:16px;margin:-5px 0;border-radius:8px}"
		"QSlider::sub-page:horizontal{background:#2563EB;border-radius:4px}"
		"QSlider::add-page:horizontal{background:#334155;border-radius:4px}"
		f"QListWidget{{background:#0B1220;border:1px solid #1F2937;border-radius:{max(radius - 4, 10)}px;outline:none;padding:6px}}"
		f"QListWidget::item{{padding:{item_padding};border-radius:10px;color:#E5E7EB}}"
		"QListWidget::item:selected{background:#1D4ED8;color:#F8FAFC}"
		"QListWidget::item:hover{background:#172033}"
		f"QLineEdit,QComboBox{{background:#0B1220;border:1px solid #334155;border-radius:10px;padding:{input_padding};color:#F8FAFC}}"
		"QLineEdit:focus,QComboBox:focus{border:1px solid #2563EB}"
	)


def build_video_style():
	video_radius = 16 if is_linux() else 22
	return (
		f"QFrame{{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #020617,stop:0.6 #0B1220,stop:1 #111827);"
		f"border:1px solid #1E293B;border-radius:{video_radius}px}}"
	)


STYLE = build_style()
VIDEO_STYLE = build_video_style()


def clean_title(stem):
	"""Extract movie/show title from release filename."""
	name = re.split(
		r"[\[\(]|\b(19|20)\d{2}\b|\b(720|1080|2160|480)[pPiI]\b|\bWEB|\bBlu[Rr]ay|\bBRRip|\bHDRip|\bWEBRip|\bHDTS|\bx26[45]|\bHEVC|\bAAC|\bH\.?264|\bH\.?265",
		stem,
	)[0]
	return re.sub(r"[._]", " ", name).strip()


def os_hash(path):
	"""OpenSubtitles hash: sum of first+last 64KB as uint64 words + filesize."""
	bs = 64 * 1024
	sz = path.stat().st_size
	if sz < bs * 2:
		return None
	h = sz
	with open(path, "rb") as f:
		for _ in range(bs // 8):
			h += struct.unpack("<Q", f.read(8))[0]
		f.seek(-bs, 2)
		for _ in range(bs // 8):
			h += struct.unpack("<Q", f.read(8))[0]
	return f"{h & 0xFFFFFFFFFFFFFFFF:016x}"


def fmt(ms):
	s = max(0, ms // 1000)
	return f"{s // 60}:{s % 60:02d}"


def srt_ts(sec):
	h, r = divmod(sec, 3600)
	m, s = divmod(r, 60)
	return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((sec % 1) * 1000):03d}"


def media_signature(path: Path):
	try:
		size = path.stat().st_size
	except OSError:
		size = 0
	title = clean_title(path.stem).lower()
	return f"{title}|{size}"


def media_identity(path: Path):
	return {
		"name": path.name,
		"stem": clean_title(path.stem).lower(),
		"signature": media_signature(path),
	}


class SubtitleWorker(QtCore.QObject):
	finished = QtCore.pyqtSignal(str, object)
	errored = QtCore.pyqtSignal(str)
	progress = QtCore.pyqtSignal(str)

	def __init__(self, path: Path, lang: str = "eng"):
		super().__init__()
		self.path, self.lang = path, lang

	def _log(self, msg):
		print(f"[Sub] {msg}", flush=True)
		self.progress.emit(msg)

	@QtCore.pyqtSlot()
	def run(self):
		try:
			self.progress.emit("Searching OpenSubtitles...")
			result = self._try_opensubtitles()
			if not result:
				self.progress.emit("Providers failed, trying Whisper...")
				result = self._whisper()
			self.finished.emit(f"Subtitles: {result.name}", result)
		except Exception as e:
			traceback.print_exc()
			self.errored.emit(str(e))

	def _try_opensubtitles(self):
		hdr = {"Api-Key": OS_KEY, "User-Agent": "AutoSub v0.1"}
		lang2 = LANG_MAP.get(self.lang, self.lang[:2])
		try:
			data = []
			fhash = os_hash(self.path)
			if fhash:
				self._log(f"Hash search: {fhash} [{lang2}]")
				r = requests.get(
					f"{OS_API}/subtitles",
					headers=hdr,
					params={"moviehash": fhash, "languages": lang2},
					timeout=15,
				)
				if r.status_code == 200:
					data = r.json().get("data", [])
				if data:
					self._log(f"Hash matched {len(data)} subtitle(s)")
			if not data:
				title = clean_title(self.path.stem)
				self._log(f"Text search: '{title}' [{lang2}]")
				r = requests.get(
					f"{OS_API}/subtitles",
					headers=hdr,
					params={"query": title, "languages": lang2},
					timeout=15,
				)
				if r.status_code != 200:
					self._log(f"Search error: {r.status_code}")
					return None
				data = r.json().get("data", [])
			if not data:
				self._log("No results")
				return None
			self._log(f"Found {len(data)} subtitle(s)")
			fid = next(
				(
					f["file_id"]
					for e in data
					for f in e.get("attributes", {}).get("files", [])
					if "file_id" in f
				),
				None,
			)
			if not fid:
				return None
			self._log(f"Logging in as {OS_USER}...")
			lr = requests.post(
				f"{OS_API}/login",
				headers={**hdr, "Content-Type": "application/json"},
				json={"username": OS_USER, "password": OS_PASS},
				timeout=15,
			)
			tok = lr.json().get("token") if lr.status_code == 200 else None
			dl_h = {**hdr, "Content-Type": "application/json"}
			if tok:
				dl_h["Authorization"] = f"Bearer {tok}"
			dr = requests.post(
				f"{OS_API}/download",
				headers=dl_h,
				json={"file_id": fid},
				timeout=15,
			)
			if dr.status_code != 200:
				self._log(f"Download error: {dr.status_code} {dr.text[:200]}")
				if dr.status_code == 406:
					self.progress.emit(
						f"Quota exhausted, resets in {dr.json().get('reset_time', '?')}"
					)
				return None
			link = dr.json().get("link")
			if not link:
				return None
			self.progress.emit("Downloading subtitle file...")
			fr = requests.get(link, timeout=30)
			if fr.status_code != 200:
				return None
			out = self.path.with_suffix(f".{self.lang}.srt")
			out.write_bytes(fr.content)
			self._log(f"Saved: {out.name}")
			return out
		except Exception as e:
			self._log(f"OpenSubtitles error: {e}")
			return None

	def _whisper(self):
		if not shutil.which("ffmpeg"):
			raise RuntimeError("ffmpeg required for Whisper")
		self._log("Loading Whisper model...")
		model = whisper.load_model("large-v3")
		self._log("Transcribing...")
		result = model.transcribe(str(self.path), fp16=False)
		lines = [
			f"{i}\n{srt_ts(s['start'])} --> {srt_ts(s['end'])}\n{s.get('text', '').strip()}"
			for i, s in enumerate(result.get("segments", []), 1)
		]
		out = self.path.with_suffix(".whisper.srt")
		out.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
		self._log(f"Whisper done: {out.name}")
		return out


class WatchSyncClient(QtCore.QObject):
	connectedChanged = QtCore.pyqtSignal(bool)
	statusChanged = QtCore.pyqtSignal(str)
	memberChanged = QtCore.pyqtSignal(list)
	eventReceived = QtCore.pyqtSignal(dict)

	def __init__(self, parent=None):
		super().__init__(parent)
		self.network = QtNetwork.QNetworkAccessManager(self)
		self.poll_timer = QtCore.QTimer(self)
		self.poll_timer.setInterval(900)
		self.poll_timer.timeout.connect(self.poll)
		self.server_url = ""
		self.room_code = ""
		self.name = ""
		self.client_id = uuid.uuid4().hex
		self.connected = False
		self.last_event_id = 0
		self._pending = set()

	def connect_room(self, server_url: str, room_code: str, name: str):
		self.server_url = server_url.rstrip("/")
		self.room_code = room_code.strip()
		self.name = name.strip() or "Guest"
		if not self.server_url or not self.room_code:
			self.statusChanged.emit("Server URL and room code are required")
			return
		self._post(
			f"/rooms/{quote(self.room_code)}/join",
			{"client_id": self.client_id, "name": self.name},
			self._on_join,
		)
		self.statusChanged.emit("Connecting to room...")

	def disconnect_room(self):
		self.poll_timer.stop()
		self.connected = False
		self.last_event_id = 0
		self._pending.clear()
		self.connectedChanged.emit(False)
		self.memberChanged.emit([])
		self.statusChanged.emit("Watch Together disconnected")

	def send_event(self, event_type: str, payload: dict):
		if not self.connected:
			return
		self._post(
			f"/rooms/{quote(self.room_code)}/events",
			{"client_id": self.client_id, "type": event_type, "payload": payload},
			self._on_event_sent,
		)

	def poll(self):
		if not self.connected:
			return
		req = QtNetwork.QNetworkRequest(
			QtCore.QUrl(
				f"{self.server_url}/rooms/{quote(self.room_code)}/events?since={self.last_event_id}&client_id={self.client_id}"
			)
		)
		reply = self.network.get(req)
		self._track(reply, self._on_poll)

	def _post(self, path: str, payload: dict, callback):
		req = QtNetwork.QNetworkRequest(QtCore.QUrl(f"{self.server_url}{path}"))
		req.setHeader(
			QtNetwork.QNetworkRequest.KnownHeaders.ContentTypeHeader,
			"application/json",
		)
		reply = self.network.post(req, json.dumps(payload).encode("utf-8"))
		self._track(reply, callback)

	def _track(self, reply, callback):
		self._pending.add(reply)

		def done():
			self._pending.discard(reply)
			callback(reply)
			reply.deleteLater()

		reply.finished.connect(done)

	def _decode(self, reply):
		if reply.error() != QtNetwork.QNetworkReply.NetworkError.NoError:
			self.statusChanged.emit(reply.errorString())
			return None
		try:
			return json.loads(bytes(reply.readAll()).decode("utf-8") or "{}")
		except json.JSONDecodeError:
			self.statusChanged.emit("Sync server returned invalid JSON")
			return None

	def _on_join(self, reply):
		data = self._decode(reply)
		if not data:
			self.connected = False
			self.connectedChanged.emit(False)
			return
		self.connected = True
		self.last_event_id = int(data.get("latest_event_id", 0))
		self.connectedChanged.emit(True)
		self.memberChanged.emit(data.get("members", []))
		self.statusChanged.emit(f"Connected to room {self.room_code}")
		self.poll_timer.start()
		state = data.get("state")
		if state:
			self.eventReceived.emit(state)

	def _on_event_sent(self, reply):
		self._decode(reply)

	def _on_poll(self, reply):
		data = self._decode(reply)
		if not data:
			return
		self.last_event_id = max(self.last_event_id, int(data.get("latest_event_id", self.last_event_id)))
		self.memberChanged.emit(data.get("members", []))
		for event in data.get("events", []):
			if event.get("client_id") == self.client_id:
				continue
			self.eventReceived.emit(event)


class VideoPlayer(QtWidgets.QMainWindow):
	def __init__(self):
		super().__init__()
		self.setWindowTitle("AutoSub")
		self.resize(1160 if is_linux() else 1180, 760 if is_linux() else 680)
		self.setAcceptDrops(True)
		self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
		self._apply_platform_tweaks()
		self.setStyleSheet(STYLE)
		self.instance = vlc.Instance()
		self.player = self.instance.media_player_new()
		self.media = None
		self.playlist: list[Path] = []
		self.current_index = None
		self.is_fullscreen = False
		self.saved_geometry = None
		self._threads: dict = {}
		self._workers: dict = {}
		self._active: dict = {}
		self._q_items: dict = {}
		self._jobs: list = []
		self._jid = 0
		self._sync_applying = False
		self._resume_after_seek = False
		self.sync = WatchSyncClient(self)
		self._build_ui()
		self.timer = QtCore.QTimer(self)
		self.timer.setInterval(200)
		self.timer.timeout.connect(self._tick)
		self.sync.statusChanged.connect(self._on_sync_status)
		self.sync.connectedChanged.connect(self._on_sync_connected)
		self.sync.memberChanged.connect(self._on_sync_members)
		self.sync.eventReceived.connect(self._apply_remote_event)

	def _apply_platform_tweaks(self):
		QtWidgets.QApplication.setStyle(QtWidgets.QStyleFactory.create("Fusion"))
		font = QtGui.QFont()
		font.setPointSize(10 if is_linux() else 11)
		QtWidgets.QApplication.setFont(font)
		if is_linux():
			self.setMinimumSize(980, 700)
		else:
			self.setMinimumSize(960, 620)

	def _build_ui(self):
		c = QtWidgets.QWidget()
		self._ml = QtWidgets.QHBoxLayout(c)
		self._default_outer_margins = 14 if is_linux() else 18
		self._default_outer_spacing = 12 if is_linux() else 14
		self._card_margins = 14 if is_linux() else 16
		self._card_spacing = 10 if is_linux() else 12
		self._compact_spacing = 10 if is_linux() else 12
		self._control_slider_width = 96 if is_linux() else 120
		self._queue_height = 84 if is_linux() else 100
		self._ml.setContentsMargins(
			self._default_outer_margins,
			self._default_outer_margins,
			self._default_outer_margins,
			self._default_outer_margins,
		)
		self._ml.setSpacing(self._default_outer_spacing)

		self._pf = QtWidgets.QFrame()
		self._pf.setObjectName("playlistFrame")
		self._pf.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel | QtWidgets.QFrame.Shadow.Raised)
		pl = QtWidgets.QVBoxLayout(self._pf)
		pl.setContentsMargins(self._card_margins, self._card_margins, self._card_margins, self._card_margins)
		pl.setSpacing(self._card_spacing)

		hdr = QtWidgets.QHBoxLayout()
		playlist_title = QtWidgets.QLabel("Library")
		playlist_title.setObjectName("titleLabel")
		hdr.addWidget(playlist_title)
		lang_layout = QtWidgets.QVBoxLayout()
		lang_label = QtWidgets.QLabel("Subtitles")
		lang_label.setObjectName("sectionLabel")
		lang_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
		lang_layout.addWidget(lang_label)
		self.lang_combo = QtWidgets.QComboBox()
		self.lang_combo.addItem("English", "eng")
		self.lang_combo.addItem("Turkish", "tur")
		self.lang_combo.addItem("Russian", "rus")
		self.lang_combo.currentIndexChanged.connect(self._on_lang_changed)
		lang_layout.addWidget(self.lang_combo)
		lang_widget = QtWidgets.QWidget()
		lang_widget.setLayout(lang_layout)
		hdr.addWidget(lang_widget)
		hdr.addStretch()
		self._activity = QtWidgets.QLabel("")
		self._activity.setObjectName("badgeLabel")
		self._activity.hide()
		hdr.addWidget(self._activity)
		self._sel_btn = QtWidgets.QPushButton("Select All")
		self._sel_btn.setObjectName("secondaryButton")
		self._sel_btn.clicked.connect(self._toggle_select)
		self._sel_btn.hide()
		hdr.addWidget(self._sel_btn)
		for lbl, fn in [("Download Subs", self._download_subs), ("Add Videos", self._add_videos)]:
			b = QtWidgets.QPushButton(lbl)
			if lbl == "Download Subs":
				b.setObjectName("secondaryButton")
			b.clicked.connect(fn)
			hdr.addWidget(b)
		pl.addLayout(hdr)

		self._plw = QtWidgets.QListWidget()
		self._plw.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
		self._plw.itemChanged.connect(self._refresh_selection_button)
		self._plw.itemDoubleClicked.connect(lambda it: self._play_index(self._plw.row(it)))
		pl.addWidget(self._plw, stretch=1)
		self._queue_label = QtWidgets.QLabel("Subtitle Queue")
		self._queue_label.setObjectName("sectionLabel")
		pl.addWidget(self._queue_label)
		self._qw = QtWidgets.QListWidget()
		self._qw.setMaximumHeight(self._queue_height)
		pl.addWidget(self._qw)

		self._sync_frame = QtWidgets.QFrame()
		self._sync_frame.setObjectName("syncFrame")
		sl = QtWidgets.QVBoxLayout(self._sync_frame)
		sl.setContentsMargins(self._card_margins, self._card_margins, self._card_margins, self._card_margins)
		sl.setSpacing(self._compact_spacing)
		sync_title = QtWidgets.QLabel("Watch Together")
		sync_title.setObjectName("titleLabel")
		sl.addWidget(sync_title)
		sync_body = QtWidgets.QLabel(
			"Sync playback across different networks. Both people still need the same local video file."
		)
		sync_body.setObjectName("mutedLabel")
		sync_body.setWordWrap(True)
		sl.addWidget(sync_body)
		self._server_in = QtWidgets.QLineEdit("http://127.0.0.1:8765")
		self._room_in = QtWidgets.QLineEdit("date-night")
		self._name_in = QtWidgets.QLineEdit(os.environ.get("USER", "You"))
		self._server_in.setPlaceholderText("Sync server URL")
		self._room_in.setPlaceholderText("Room code")
		self._name_in.setPlaceholderText("Your name")
		for label, widget in [
			("Server", self._server_in),
			("Room", self._room_in),
			("Name", self._name_in),
		]:
			lab = QtWidgets.QLabel(label)
			lab.setObjectName("sectionLabel")
			sl.addWidget(lab)
			sl.addWidget(widget)
		btns = QtWidgets.QHBoxLayout()
		self._connect_btn = QtWidgets.QPushButton("Connect")
		self._connect_btn.clicked.connect(self._connect_sync)
		btns.addWidget(self._connect_btn)
		self._disconnect_btn = QtWidgets.QPushButton("Leave")
		self._disconnect_btn.setObjectName("dangerButton")
		self._disconnect_btn.clicked.connect(self.sync.disconnect_room)
		self._disconnect_btn.setEnabled(False)
		btns.addWidget(self._disconnect_btn)
		sl.addLayout(btns)
		self._sync_status = QtWidgets.QLabel("Not connected")
		self._sync_status.setObjectName("statusLabel")
		self._sync_status.setWordWrap(True)
		self._sync_members = QtWidgets.QLabel("Members: -")
		self._sync_members.setObjectName("mutedLabel")
		self._sync_members.setWordWrap(True)
		sl.addWidget(self._sync_status)
		sl.addWidget(self._sync_members)
		pl.addWidget(self._sync_frame)

		self._rl = QtWidgets.QVBoxLayout()
		self._rl.setSpacing(self._default_outer_spacing)
		self._vf = QtWidgets.QFrame()
		self._vf.setObjectName("videoFrame")
		self._vf.setStyleSheet(VIDEO_STYLE)
		self._vf.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel | QtWidgets.QFrame.Shadow.Raised)
		vbox = QtWidgets.QVBoxLayout(self._vf)
		vbox.setContentsMargins(10, 10, 10, 10)
		vbox.setSpacing(0)
		self._video_surface = QtWidgets.QWidget()
		self._video_surface.setObjectName("videoSurface")
		self._video_surface.setStyleSheet("QWidget#videoSurface{background:black}")
		self._video_surface.setSizePolicy(
			QtWidgets.QSizePolicy.Policy.Expanding,
			QtWidgets.QSizePolicy.Policy.Expanding,
		)
		self._video_surface.setMinimumSize(320, 200)
		self._video_surface.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
		self._vf.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
		self._vf.installEventFilter(self)
		self._video_surface.installEventFilter(self)
		vbox.addWidget(self._video_surface, stretch=1)
		self._rl.addWidget(self._vf, stretch=1)

		self._prog_w = QtWidgets.QFrame()
		self._prog_w.setObjectName("progressCard")
		pr = QtWidgets.QHBoxLayout()
		pr.setContentsMargins(self._card_margins, 12 if is_linux() else 12, self._card_margins, 12)
		pr.setSpacing(self._compact_spacing)
		self._elapsed = QtWidgets.QLabel("0:00")
		self._slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
		self._slider.setRange(0, 1000)
		self._slider.sliderPressed.connect(self._begin_seek)
		self._slider.sliderReleased.connect(self._seek)
		self._total = QtWidgets.QLabel("0:00")
		pr.addWidget(self._elapsed)
		pr.addWidget(self._slider, stretch=1)
		pr.addWidget(self._total)
		self._prog_w.setLayout(pr)
		self._rl.addWidget(self._prog_w)

		self._ctrl_w = QtWidgets.QFrame()
		self._ctrl_w.setObjectName("controlCard")
		cl = QtWidgets.QHBoxLayout()
		cl.setContentsMargins(self._card_margins, 12, self._card_margins, 12)
		cl.setSpacing(self._compact_spacing)
		self._play_btn = QtWidgets.QPushButton("Play")
		self._play_btn.clicked.connect(self._toggle_play)
		cl.addWidget(self._play_btn)
		b = QtWidgets.QPushButton("Stop")
		b.setObjectName("secondaryButton")
		b.clicked.connect(self._stop)
		cl.addWidget(b)
		self._fs_btn = QtWidgets.QPushButton("Fullscreen")
		self._fs_btn.setObjectName("secondaryButton")
		self._fs_btn.clicked.connect(self._toggle_fs)
		cl.addWidget(self._fs_btn)
		vol_label = QtWidgets.QLabel("Volume")
		vol_label.setObjectName("sectionLabel")
		cl.addWidget(vol_label)
		vol = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
		vol.setRange(0, 100)
		vol.setValue(80)
		vol.setMaximumWidth(self._control_slider_width)
		vol.valueChanged.connect(self.player.audio_set_volume)
		cl.addWidget(vol)
		cl.addStretch()
		self._status = QtWidgets.QLabel("Drop a video to start")
		self._status.setObjectName("statusLabel")
		cl.addWidget(self._status)
		self._ctrl_w.setLayout(cl)
		self._rl.addWidget(self._ctrl_w)

		self._pf.setMinimumWidth(340 if is_linux() else 360)
		self._ml.addWidget(self._pf, stretch=0)
		self._ml.addLayout(self._rl, stretch=1)
		self.setCentralWidget(c)
		self._ctrl_w.hide()
		self._prog_w.hide()
		self._queue_label.hide()
		self._qw.hide()
		self._refresh_selection_button()

	def _connect_sync(self):
		self.sync.connect_room(
			self._server_in.text().strip(),
			self._room_in.text().strip(),
			self._name_in.text().strip(),
		)

	def _on_sync_status(self, message):
		self._sync_status.setText(message)

	def _on_sync_connected(self, connected):
		self._connect_btn.setEnabled(not connected)
		self._disconnect_btn.setEnabled(connected)
		for w in (self._server_in, self._room_in, self._name_in):
			w.setEnabled(not connected)
		if connected:
			self._sync_status.setText(f"Connected to {self._room_in.text().strip()}")

	def _on_sync_members(self, members):
		names = [m.get("name", "Guest") for m in members]
		self._sync_members.setText(f"Members: {', '.join(names) if names else '-'}")

	def _add_videos(self):
		paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
			self,
			"Add Videos",
			"",
			"Video (*.mp4 *.mkv *.avi *.mov *.wmv *.flv);;All (*)",
		)
		self._enqueue([Path(p) for p in paths])

	def _enqueue(self, paths):
		new = [p for p in paths if p.exists()]
		if not new:
			return
		for p in new:
			self.playlist.append(p)
			it = QtWidgets.QListWidgetItem(p.name)
			it.setData(QtCore.Qt.ItemDataRole.UserRole, str(p))
			it.setData(QtCore.Qt.ItemDataRole.UserRole + 2, media_signature(p))
			it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
			it.setCheckState(QtCore.Qt.CheckState.Unchecked)
			self._plw.addItem(it)
		self._sel_btn.setVisible(self._plw.count() > 0)
		self._ctrl_w.show()
		self._prog_w.show()
		self._refresh_selection_button()
		if self.current_index is None and self.playlist:
			self.current_index = 0
			self._plw.setCurrentRow(0)
		self._status.setText(f"Added {len(new)} video(s). Double-click or press Play to start.")
		self._refresh_queue_visibility()

	def _play_index(self, idx, remote=False):
		if 0 <= idx < len(self.playlist):
			self.current_index = idx
			self._plw.setCurrentRow(idx)
			self._load(self.playlist[idx], remote=remote)

	def _toggle_select(self):
		n = self._plw.count()
		if not n:
			return
		on = all(self._plw.item(i).checkState() == QtCore.Qt.CheckState.Checked for i in range(n))
		st = QtCore.Qt.CheckState.Unchecked if on else QtCore.Qt.CheckState.Checked
		for i in range(n):
			self._plw.item(i).setCheckState(st)
		self._refresh_selection_button()

	def _refresh_selection_button(self):
		n = self._plw.count()
		if not n:
			self._sel_btn.hide()
			return
		checked = sum(
			1 for i in range(n) if self._plw.item(i).checkState() == QtCore.Qt.CheckState.Checked
		)
		self._sel_btn.show()
		self._sel_btn.setText("Clear Selection" if checked == n and n > 0 else "Select All")

	def _refresh_queue_visibility(self):
		has_queue = self._qw.count() > 0
		self._queue_label.setVisible(has_queue)
		self._qw.setVisible(has_queue)

	def _download_subs(self):
		lang = self.lang_combo.currentData() or "eng"
		paths = [
			Path(self._plw.item(i).data(QtCore.Qt.ItemDataRole.UserRole))
			for i in range(self._plw.count())
			if self._plw.item(i).checkState() == QtCore.Qt.CheckState.Checked
			and self._plw.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
		]
		if not paths:
			self._status.setText("Check videos first")
			return
		existing = {(Path(j["path"]).resolve(), j["lang"]) for j in self._jobs}
		existing |= {(p.resolve(), l) for p, l in self._active.values()}
		for p in paths:
			if not p.exists() or (p.resolve(), lang) in existing:
				continue
			self._jid += 1
			self._jobs.append({"id": self._jid, "path": p, "lang": lang})
			it = QtWidgets.QListWidgetItem(f"Queued [{lang}]: {p.name}")
			it.setData(QtCore.Qt.ItemDataRole.UserRole, self._jid)
			it.setData(QtCore.Qt.ItemDataRole.UserRole + 1, p.name)
			self._qw.addItem(it)
			self._q_items[self._jid] = it
		self._refresh_queue_visibility()
		self._pump()

	def _pump(self):
		while len(self._threads) < 2 and self._jobs:
			job = self._jobs.pop(0)
			jid, path, lang = job["id"], job["path"], job["lang"]
			w = SubtitleWorker(path, lang)
			t = QtCore.QThread(self)
			w.moveToThread(t)
			_j, _p = jid, path
			w.finished.connect(lambda msg, sp, j=_j, vp=_p: self._sub_done(msg, sp, j, vp))
			w.errored.connect(lambda msg, j=_j, vp=_p: self._sub_err(msg, j, vp))
			w.progress.connect(lambda msg, j=_j: self._sub_prog(msg, j))
			w.finished.connect(t.quit)
			w.errored.connect(t.quit)
			t.finished.connect(t.deleteLater)
			t.started.connect(w.run)
			self._threads[jid] = t
			self._workers[jid] = w
			self._active[jid] = (path, lang)
			self._update_q(jid, f"Searching [{lang}]")
			t.start()
		n = len(self._jobs) + len(self._threads)
		if n:
			self._activity.setText(f"Downloading... ({n})")
			self._activity.show()
		else:
			self._activity.hide()
		self._refresh_queue_visibility()

	def _get_video_wid(self):
		self._video_surface.show()
		self._video_surface.raise_()
		QtWidgets.QApplication.processEvents()
		return int(self._video_surface.winId())

	def _load(self, path, remote=False):
		self.player.stop()
		self.media = self.instance.media_new(str(path))
		self.player.set_media(self.media)
		wid = self._get_video_wid()
		try:
			if sys.platform == "darwin":
				self.player.set_nsobject(wid)
			elif sys.platform.startswith("win"):
				self.player.set_hwnd(wid)
			else:
				self.player.set_xwindow(wid)
		except Exception as e:
			self._status.setText(f"Video output error: {e}")
			return
		self.player.play()
		self._status.setText(f"Loaded {path.name}")
		self._slider.setValue(0)
		self._elapsed.setText("0:00")
		self._total.setText("0:00")
		self._play_btn.setText("Pause")
		self.timer.start()
		if not remote:
			self._broadcast("load", {"media": self._media_payload(path), "position_ms": 0, "playing": True})

	def _toggle_play(self):
		if self.player.is_playing():
			self.player.pause()
			self._play_btn.setText("Play")
			self._broadcast("pause", {"position_ms": self.player.get_time()})
		elif self.media:
			self.player.play()
			self._play_btn.setText("Pause")
			self.timer.start()
			self._broadcast("play", {"position_ms": self.player.get_time()})
		elif self.playlist:
			self._play_index(self.current_index or 0)

	def _stop(self, remote=False):
		self.player.stop()
		self._play_btn.setText("Play")
		self.timer.stop()
		self._slider.setValue(0)
		self._elapsed.setText("0:00")
		self._total.setText("0:00")
		if not remote:
			self._broadcast("stop", {"position_ms": 0})
		if self.media:
			self._status.setText("Playback stopped")

	def _begin_seek(self):
		self._resume_after_seek = self.player.is_playing()
		if self._resume_after_seek:
			self.player.pause()

	def _seek(self):
		if not self.media:
			return
		pos = int(self._slider.value() / 1000.0 * max(1, self.player.get_length()))
		self.player.set_position(self._slider.value() / 1000.0)
		if self._resume_after_seek:
			self.player.play()
			self._play_btn.setText("Pause")
		else:
			self._play_btn.setText("Play")
		self._resume_after_seek = False
		self._broadcast("seek", {"position_ms": pos})

	def _on_lang_changed(self):
		if self.current_index is None or self.current_index >= len(self.playlist):
			return
		vid = self.playlist[self.current_index]
		lang = self.lang_combo.currentData() or "eng"
		srt = vid.with_suffix(f".{lang}.srt")
		if srt.exists():
			self.player.video_set_subtitle_file(str(srt))
			self._status.setText(f"Loaded {lang} subtitles")
		else:
			self._status.setText(f"No {lang} subtitle file found")

	def _tick(self):
		if not self.media:
			return
		ln = self.player.get_length()
		if ln > 0:
			self._slider.blockSignals(True)
			self._slider.setValue(int(self.player.get_time() / ln * 1000))
			self._slider.blockSignals(False)
			self._elapsed.setText(fmt(self.player.get_time()))
			self._total.setText(fmt(ln))
		if self.player.get_state() in (vlc.State.Ended, vlc.State.Stopped):
			if self.current_index is not None and self.current_index + 1 < len(self.playlist):
				self._play_index(self.current_index + 1)
			else:
				self._stop()

	def _sub_done(self, msg, sub_path, jid, video_path):
		lang = self._active.get(jid, ("", ""))[1]
		self._status.setText(f"{msg} [{lang}]")
		self._update_q(jid, f"Done [{lang}]")
		if self.media:
			mrl = self.media.get_mrl()
			if isinstance(mrl, str):
				p = urlparse(mrl)
				cur = Path(unquote(p.path)) if p.scheme == "file" else Path(mrl)
				if cur.resolve() == video_path.resolve() and sub_path:
					self.player.video_set_subtitle_file(str(sub_path))
		self._cleanup(jid)

	def _sub_err(self, msg, jid, _):
		self._status.setText(f"Error: {msg}")
		self._update_q(jid, "Failed")
		self._cleanup(jid)

	def _sub_prog(self, msg, jid):
		lang = self._active.get(jid, ("", ""))[1]
		self._status.setText(f"{msg} [{lang}]")
		self._update_q(jid, f"{msg} [{lang}]")

	def _cleanup(self, jid):
		t = self._threads.pop(jid, None)
		if t and t.isRunning():
			t.quit()
		self._workers.pop(jid, None)
		self._active.pop(jid, None)
		it = self._q_items.pop(jid, None)
		if it:
			self._qw.takeItem(self._qw.row(it))
		self._refresh_queue_visibility()
		self._pump()

	def _update_q(self, jid, status):
		it = self._q_items.get(jid)
		if it:
			it.setText(f"{status}: {it.data(QtCore.Qt.ItemDataRole.UserRole + 1) or ''}")

	def _broadcast(self, event_type, payload):
		if self._sync_applying:
			return
		if event_type != "load" and self.current_index is not None and self.current_index < len(self.playlist):
			payload.setdefault("media", self._media_payload(self.playlist[self.current_index]))
		self.sync.send_event(event_type, payload)

	def _media_payload(self, path: Path):
		return media_identity(path)

	def _find_playlist_index(self, media: dict):
		signature = media.get("signature")
		stem = (media.get("stem") or "").strip().lower()
		name = (media.get("name") or "").strip().lower()
		for i in range(self._plw.count()):
			item = self._plw.item(i)
			path = Path(item.data(QtCore.Qt.ItemDataRole.UserRole))
			if signature and item.data(QtCore.Qt.ItemDataRole.UserRole + 2) == signature:
				return i
			if stem and clean_title(path.stem).lower() == stem:
				return i
			if name and path.name.lower() == name:
				return i
		return None

	def _apply_remote_event(self, event):
		payload = event.get("payload", {}) if "payload" in event else event.get("payload", {})
		event_type = event.get("type")
		if event.get("client_id") == self.sync.client_id:
			return
		if event_type is None and event.get("payload") is None:
			return
		media = payload.get("media", {})
		idx = self._find_playlist_index(media) if media else None
		self._sync_applying = True
		try:
			if media and idx is None:
				self._status.setText(f"Partner opened {media.get('name', 'a video')}. Add the same file locally to sync.")
				return
			if event_type == "load" and idx is not None:
				self._play_index(idx, remote=True)
				self.player.set_time(int(payload.get("position_ms", 0)))
				if not payload.get("playing", True):
					self.player.pause()
					self._play_btn.setText("Play")
			elif event_type == "play" and idx is not None:
				if self.current_index != idx:
					self._play_index(idx, remote=True)
				self.player.set_time(int(payload.get("position_ms", 0)))
				self.player.play()
				self._play_btn.setText("Pause")
			elif event_type == "pause" and idx is not None:
				if self.current_index != idx:
					self._play_index(idx, remote=True)
				self.player.set_time(int(payload.get("position_ms", 0)))
				self.player.pause()
				self._play_btn.setText("Play")
			elif event_type == "seek" and idx is not None:
				if self.current_index != idx:
					self._play_index(idx, remote=True)
				self.player.set_time(int(payload.get("position_ms", 0)))
			elif event_type == "stop":
				self._stop(remote=True)
			if event_type:
				self._sync_status.setText(f"Synced remote {event_type}")
		finally:
			QtCore.QTimer.singleShot(250, self._clear_sync_guard)

	def _clear_sync_guard(self):
		self._sync_applying = False

	def dragEnterEvent(self, e):
		e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

	def dropEvent(self, e):
		self._enqueue([Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()])
		e.acceptProposedAction()

	def eventFilter(self, obj, ev):
		if obj in (self._vf, self._video_surface) and ev.type() == QtCore.QEvent.Type.MouseButtonPress:
			self._toggle_play()
			return True
		return super().eventFilter(obj, ev)

	def keyPressEvent(self, e):
		k = e.key()
		if k == QtCore.Qt.Key.Key_Space:
			e.accept()
			self._toggle_play()
		elif k == QtCore.Qt.Key.Key_Right:
			e.accept()
			self._seek_rel(5000)
		elif k == QtCore.Qt.Key.Key_Left:
			e.accept()
			self._seek_rel(-5000)
		elif k == QtCore.Qt.Key.Key_F:
			e.accept()
			self._toggle_fs()
		elif k == QtCore.Qt.Key.Key_Escape and self.is_fullscreen:
			e.accept()
			self._toggle_fs()
		else:
			super().keyPressEvent(e)

	def _seek_rel(self, ms):
		if self.media and self.player.get_length() > 0:
			target = max(0, min(self.player.get_length() - 500, self.player.get_time() + ms))
			self.player.set_time(target)
			self._broadcast("seek", {"position_ms": target})

	def _toggle_fs(self):
		if self.is_fullscreen:
			self.player.set_fullscreen(False)
			self._ml.setContentsMargins(
				self._default_outer_margins,
				self._default_outer_margins,
				self._default_outer_margins,
				self._default_outer_margins,
			)
			self._ml.setSpacing(self._default_outer_spacing)
			self._vf.setStyleSheet(VIDEO_STYLE)
			self._vf.setFrameStyle(QtWidgets.QFrame.Shape.StyledPanel | QtWidgets.QFrame.Shadow.Raised)
			for w in (self._pf, self._ctrl_w, self._prog_w):
				w.show()
			if self.saved_geometry:
				self.setGeometry(self.saved_geometry)
			self.showNormal()
			self._fs_btn.setText("Fullscreen")
		else:
			self.saved_geometry = self.geometry()
			self._ml.setContentsMargins(0, 0, 0, 0)
			self._ml.setSpacing(0)
			self._vf.setStyleSheet("QFrame{background:black;border:none}")
			self._vf.setFrameStyle(QtWidgets.QFrame.Shape.NoFrame)
			for w in (self._pf, self._ctrl_w, self._prog_w):
				w.hide()
			self.showFullScreen()
			if not is_linux():
				self.player.set_fullscreen(True)
			self._fs_btn.setText("Windowed")
		self.is_fullscreen = not self.is_fullscreen

	def closeEvent(self, e):
		self.sync.disconnect_room()
		for t in list(self._threads.values()):
			if t.isRunning():
				t.quit()
				t.wait(2000)
		e.accept()


if __name__ == "__main__":
	app = QtWidgets.QApplication(sys.argv)
	app.setApplicationName("AutoSub")
	w = VideoPlayer()
	w.show()
	sys.exit(app.exec())
