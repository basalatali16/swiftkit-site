"""
LANCOM - LAN-only voice calling and text messaging over WiFi.

No internet, no server. Devices discover each other via UDP broadcast and
talk directly, peer-to-peer, over the local WiFi network. All contacts,
messages, call history, and file-transfer records persist locally on the
device (SQLite) so history survives app restarts and is available even for
peers that are currently offline.

Ports:
  55555/UDP - peer discovery (broadcast)
  55556/TCP - text messaging
  55557/TCP - call signaling (invite/accept/reject/hangup)
  55558/UDP - call audio (raw PCM16 frames, best-effort)
  55559/TCP - file transfer
"""

import json
import os
import socket
import struct
import threading
import time
import uuid

from kivy.app import App
from kivy.clock import Clock
from kivy.graphics import Color, RoundedRectangle
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import StringProperty, BooleanProperty
from kivy.storage.jsonstore import JsonStore
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.screenmanager import Screen, SlideTransition
from kivy.utils import platform, escape_markup

from store import Store

BROADCAST_PORT = 55555
MESSAGE_PORT = 55556
CALL_SIGNAL_PORT = 55557
AUDIO_PORT = 55558
FILE_PORT = 55559

BROADCAST_INTERVAL = 2.0
PEER_TIMEOUT = 7.0
FILE_CHUNK = 65536

DEVICE_ID = uuid.uuid4().hex[:12]

# "Luxury" palette - deep navy background, warm gold accent.
COLOR_BG = (0.035, 0.043, 0.078, 1)
COLOR_CARD = (0.075, 0.086, 0.13, 1)
COLOR_GOLD = (0.83, 0.69, 0.42, 1)
COLOR_GOLD_DIM = (0.6, 0.5, 0.32, 1)
COLOR_TEXT = (0.94, 0.93, 0.90, 1)
COLOR_TEXT_DIM = (0.58, 0.58, 0.64, 1)
COLOR_ONLINE = (0.36, 0.82, 0.55, 1)
COLOR_OFFLINE = (0.46, 0.46, 0.52, 1)
COLOR_DANGER = (0.80, 0.28, 0.28, 1)

AVATAR_COLORS = [
    (0.72, 0.45, 0.20), (0.30, 0.48, 0.75), (0.52, 0.36, 0.75),
    (0.75, 0.32, 0.48), (0.30, 0.62, 0.48), (0.83, 0.69, 0.42),
]


def _color_for_name(name):
    h = sum(ord(c) for c in name) if name else 0
    return AVATAR_COLORS[h % len(AVATAR_COLORS)]


def format_last_seen(ts):
    if not ts:
        return "never seen"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def format_size(n):
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _get_local_ip_via_wifi_manager():
    """Ask Android's WifiManager for the WiFi interface's IP directly.
    Avoids relying on the OS routing table, which can pick the wrong
    interface (or fail outright) on a WiFi network with no internet
    gateway - exactly the case this app is built for."""
    from jnius import autoclass
    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Context = autoclass("android.content.Context")
    activity = PythonActivity.mActivity
    wifi_manager = activity.getSystemService(Context.WIFI_SERVICE)
    ip_int = wifi_manager.getConnectionInfo().getIpAddress()
    if not ip_int:
        return None
    return socket.inet_ntoa(struct.pack("<I", ip_int))


def get_local_ip():
    """Best-effort LAN IP without needing internet access."""
    if platform == "android":
        try:
            ip = _get_local_ip_via_wifi_manager()
            if ip and ip != "0.0.0.0":
                return ip
        except Exception:
            pass

    # Connecting a UDP socket doesn't send any packets - it just asks the
    # kernel to resolve a route, which is enough to read back the local
    # interface IP via getsockname(). Must be a plain unicast address:
    # connecting to a broadcast address requires SO_BROADCAST first and
    # raises PermissionError otherwise.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def get_broadcast_ip(local_ip):
    parts = local_ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:3] + ["255"])
    return "255.255.255.255"


def send_json_tcp(ip, port, obj, timeout=4.0):
    """Send one length-prefixed JSON message over a fresh TCP connection."""
    data = json.dumps(obj).encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((ip, port))
        sock.sendall(struct.pack("!I", len(data)) + data)


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


MAX_MESSAGE_BYTES = 64 * 1024


def recv_json_tcp(conn):
    header = recv_exact(conn, 4)
    if header is None:
        return None
    (length,) = struct.unpack("!I", header)
    if length > MAX_MESSAGE_BYTES:
        return None
    payload = recv_exact(conn, length)
    if payload is None:
        return None
    return json.loads(payload.decode("utf-8"))


# ---------------------------------------------------------------------------
# Audio I/O
# ---------------------------------------------------------------------------

class AudioIO:
    """Abstract mic capture / speaker playback for call audio."""

    SAMPLE_RATE = 16000

    def start_capture(self, callback):
        raise NotImplementedError

    def stop_capture(self):
        raise NotImplementedError

    def start_playback(self):
        raise NotImplementedError

    def write_playback(self, data):
        raise NotImplementedError

    def stop_playback(self):
        raise NotImplementedError


if platform == "android":
    from jnius import autoclass

    AndroidAudioRecord = autoclass("android.media.AudioRecord")
    AndroidAudioTrack = autoclass("android.media.AudioTrack")
    AudioFormat = autoclass("android.media.AudioFormat")
    AudioManager = autoclass("android.media.AudioManager")
    AudioSource = autoclass("android.media.MediaRecorder$AudioSource")

    class NativeAudioIO(AudioIO):
        """Raw PCM capture/playback via Android's native AudioRecord/AudioTrack,
        reached through pyjnius. Replaces the dead pyaudio call path -
        pyaudio has no python-for-android recipe."""

        def __init__(self):
            self._channel_in = AudioFormat.CHANNEL_IN_MONO
            self._channel_out = AudioFormat.CHANNEL_OUT_MONO
            self._encoding = AudioFormat.ENCODING_PCM_16BIT
            self._min_buf_in = AndroidAudioRecord.getMinBufferSize(
                self.SAMPLE_RATE, self._channel_in, self._encoding
            )
            self._min_buf_out = AndroidAudioTrack.getMinBufferSize(
                self.SAMPLE_RATE, self._channel_out, self._encoding
            )
            self._record = None
            self._track = None
            self._capturing = False
            self._capture_thread = None

        def start_capture(self, callback):
            buf_size = max(self._min_buf_in, 1) * 2
            self._record = AndroidAudioRecord(
                AudioSource.VOICE_COMMUNICATION,
                self.SAMPLE_RATE,
                self._channel_in,
                self._encoding,
                buf_size,
            )
            self._record.startRecording()
            self._capturing = True

            def loop():
                chunk = 1024
                buf = bytearray(chunk)
                while self._capturing:
                    try:
                        n = self._record.read(buf, 0, chunk)
                    except Exception:
                        break
                    if n and n > 0:
                        callback(bytes(buf[:n]))

            self._capture_thread = threading.Thread(target=loop, daemon=True)
            self._capture_thread.start()

        def stop_capture(self):
            self._capturing = False
            if self._capture_thread:
                self._capture_thread.join(timeout=1.0)
                self._capture_thread = None
            if self._record:
                try:
                    self._record.stop()
                except Exception:
                    pass
                self._record.release()
                self._record = None

        def start_playback(self):
            buf_size = max(self._min_buf_out, 4096) * 2
            self._track = AndroidAudioTrack(
                AudioManager.STREAM_VOICE_CALL,
                self.SAMPLE_RATE,
                self._channel_out,
                self._encoding,
                buf_size,
                AndroidAudioTrack.MODE_STREAM,
            )
            self._track.play()

        def write_playback(self, data):
            if self._track:
                self._track.write(data, 0, len(data))

        def stop_playback(self):
            if self._track:
                try:
                    self._track.stop()
                except Exception:
                    pass
                self._track.release()
                self._track = None

    def make_audio_io():
        return NativeAudioIO()

else:
    class DesktopAudioIO(AudioIO):
        """pyaudio-backed fallback for running/testing on a desktop.
        Not used on Android - pyaudio isn't part of the APK build."""

        def __init__(self):
            self._pa = None
            self._in_stream = None
            self._out_stream = None
            self._capturing = False
            try:
                import pyaudio
                self._pa = pyaudio.PyAudio()
            except Exception:
                self._pa = None

        def start_capture(self, callback):
            if not self._pa:
                return
            import pyaudio
            self._in_stream = self._pa.open(
                format=pyaudio.paInt16, channels=1, rate=self.SAMPLE_RATE,
                input=True, frames_per_buffer=1024,
            )
            self._capturing = True

            def loop():
                while self._capturing:
                    try:
                        data = self._in_stream.read(1024, exception_on_overflow=False)
                    except Exception:
                        break
                    callback(data)

            threading.Thread(target=loop, daemon=True).start()

        def stop_capture(self):
            self._capturing = False
            if self._in_stream:
                self._in_stream.stop_stream()
                self._in_stream.close()
                self._in_stream = None

        def start_playback(self):
            if not self._pa:
                return
            import pyaudio
            self._out_stream = self._pa.open(
                format=pyaudio.paInt16, channels=1, rate=self.SAMPLE_RATE, output=True
            )

        def write_playback(self, data):
            if self._out_stream:
                try:
                    self._out_stream.write(data)
                except Exception:
                    pass

        def stop_playback(self):
            if self._out_stream:
                self._out_stream.stop_stream()
                self._out_stream.close()
                self._out_stream = None

    def make_audio_io():
        return DesktopAudioIO()


# ---------------------------------------------------------------------------
# Peer discovery
# ---------------------------------------------------------------------------

class PeerDiscovery:
    def __init__(self, display_name, store):
        self.display_name = display_name
        self.store = store
        self.local_ip = get_local_ip()
        self.broadcast_ip = get_broadcast_ip(self.local_ip)
        self.peers = {}  # device_id -> {"name", "ip", "last_seen"} (currently live only)
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        threading.Thread(target=self._listen_loop, daemon=True).start()
        threading.Thread(target=self._reap_loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        payload = {
            "type": "LANCOM_HELLO",
            "id": DEVICE_ID,
            "name": self.display_name,
        }
        data = json.dumps(payload).encode("utf-8")
        while self._running:
            try:
                sock.sendto(data, (self.broadcast_ip, BROADCAST_PORT))
            except OSError:
                pass
            time.sleep(BROADCAST_INTERVAL)
        sock.close()

    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        sock.bind(("", BROADCAST_PORT))
        sock.settimeout(1.0)
        while self._running:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if msg.get("type") != "LANCOM_HELLO":
                continue
            if msg.get("id") == DEVICE_ID:
                continue
            peer_id = msg["id"]
            name = msg.get("name", "Unknown")
            with self._lock:
                self.peers[peer_id] = {
                    "id": peer_id,
                    "name": name,
                    "ip": addr[0],
                    "last_seen": time.time(),
                }
            self.store.upsert_peer(peer_id, name, addr[0])
        sock.close()

    def _reap_loop(self):
        while self._running:
            now = time.time()
            with self._lock:
                stale = [pid for pid, p in self.peers.items()
                         if now - p["last_seen"] > PEER_TIMEOUT]
                for pid in stale:
                    del self.peers[pid]
            time.sleep(1.0)

    def get_known_peers(self):
        """All contacts ever seen, online ones first, each flagged online/offline."""
        with self._lock:
            live = dict(self.peers)
        result = []
        seen_ids = set()
        for p in self.store.get_known_peers():
            seen_ids.add(p["id"])
            live_info = live.get(p["id"])
            if live_info:
                result.append({"id": p["id"], "name": live_info["name"], "ip": live_info["ip"],
                                "online": True, "last_seen": live_info["last_seen"]})
            else:
                result.append({"id": p["id"], "name": p["name"], "ip": p["ip"],
                                "online": False, "last_seen": p["last_seen"]})
        for pid, info in live.items():
            if pid not in seen_ids:
                result.append({"id": pid, "name": info["name"], "ip": info["ip"],
                                "online": True, "last_seen": info["last_seen"]})
        result.sort(key=lambda p: (not p["online"], p["name"].lower()))
        return result


# ---------------------------------------------------------------------------
# Text messaging
# ---------------------------------------------------------------------------

class MessageServer:
    def __init__(self, display_name, store, on_message):
        self.display_name = display_name
        self.store = store
        self.on_message = on_message
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", MESSAGE_PORT))
        sock.listen(5)
        sock.settimeout(1.0)
        while self._running:
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()
        sock.close()

    def _handle_conn(self, conn, addr):
        with conn:
            try:
                msg = recv_json_tcp(conn)
            except OSError:
                return
            if not msg:
                return
            msg["ip"] = addr[0]
            self.store.add_message(msg.get("from_id", "unknown"),
                                    msg.get("from_name", "Unknown"),
                                    "in", msg.get("text", ""))
            self.on_message(msg)

    def send_message(self, peer_id, peer_name, peer_ip, text):
        self.store.add_message(peer_id, peer_name, "out", text)
        payload = {
            "type": "MSG",
            "from_id": DEVICE_ID,
            "from_name": self.display_name,
            "text": text,
            "timestamp": time.time(),
        }
        send_json_tcp(peer_ip, MESSAGE_PORT, payload)


# ---------------------------------------------------------------------------
# Call manager
# ---------------------------------------------------------------------------

class CallManager:
    """Handles call signaling (TCP) and audio streaming (UDP)."""

    def __init__(self, display_name, store, on_event):
        self.display_name = display_name
        self.store = store
        self.on_event = on_event  # callback(event_dict) - marshalled to main thread by caller
        self._running = False
        self._lock = threading.Lock()
        self.peer_id = None
        self.peer_ip = None
        self.peer_name = None
        self.state = "idle"  # idle | calling | ringing | active
        self._direction = None  # "in" | "out"
        self._call_start = None
        self.audio = None
        self._audio_send_sock = None
        self._audio_recv_thread = None
        self._audio_recv_running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._signal_listen_loop, daemon=True).start()

    def stop(self):
        self._running = False
        self.hang_up()

    # -- signaling server -------------------------------------------------

    def _signal_listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", CALL_SIGNAL_PORT))
        sock.listen(5)
        sock.settimeout(1.0)
        while self._running:
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_signal, args=(conn, addr), daemon=True).start()
        sock.close()

    def _reset_to_idle(self):
        self.state = "idle"
        self.peer_id = None
        self.peer_ip = None
        self.peer_name = None
        self._direction = None
        self._call_start = None

    def _log_call(self, peer_id, peer_name, direction, outcome):
        duration = (time.time() - self._call_start) if self._call_start else 0.0
        self.store.add_call_log(peer_id or "unknown", peer_name or "Unknown",
                                 direction or "out", outcome, duration)

    def _handle_signal(self, conn, addr):
        with conn:
            try:
                msg = recv_json_tcp(conn)
            except OSError:
                return
            if not msg:
                return
            msg_type = msg.get("type")

            if msg_type == "INVITE":
                with self._lock:
                    busy = self.state != "idle"
                    if not busy:
                        self.peer_id = msg.get("from_id")
                        self.peer_ip = addr[0]
                        self.peer_name = msg.get("from_name", "Unknown")
                        self.state = "ringing"
                        self._direction = "in"
                        self._call_start = None
                if busy:
                    try:
                        send_json_tcp(addr[0], CALL_SIGNAL_PORT,
                                      {"type": "REJECT", "from_id": DEVICE_ID, "reason": "busy"})
                    except OSError:
                        pass
                    return
                self.on_event({"event": "incoming_call", "peer_name": self.peer_name, "peer_ip": self.peer_ip})
                return

            # ACCEPT/REJECT/HANGUP only apply to messages from the peer we're
            # actually talking to - otherwise any other device on the LAN
            # could hijack or end a call in progress.
            with self._lock:
                from_current_peer = self.peer_ip is not None and addr[0] == self.peer_ip
                peer_id, peer_name, direction = self.peer_id, self.peer_name, self._direction
                if msg_type == "ACCEPT" and from_current_peer and self.state == "calling":
                    self.state = "active"
                    self._call_start = time.time()
                    action = "activate"
                elif msg_type == "REJECT" and from_current_peer and self.state == "calling":
                    self._log_call(peer_id, peer_name, direction, "rejected")
                    self._reset_to_idle()
                    action = "rejected"
                elif msg_type == "HANGUP" and from_current_peer and self.state in ("active", "ringing", "calling"):
                    outcome = "completed" if self._call_start else (
                        "missed" if direction == "in" else "cancelled")
                    self._log_call(peer_id, peer_name, direction, outcome)
                    self._reset_to_idle()
                    action = "ended"
                else:
                    action = None

            if action == "activate":
                self._start_audio()
                self.on_event({"event": "call_active", "peer_name": peer_name})
            elif action == "rejected":
                self.on_event({"event": "call_rejected", "peer_name": peer_name})
            elif action == "ended":
                self._stop_audio()
                self.on_event({"event": "call_ended", "peer_name": peer_name})

    # -- outgoing actions ---------------------------------------------------
    # Signaling sends run on a background thread so button presses never
    # block the Kivy UI thread on a slow/dead peer (send_json_tcp has up to
    # a 4s connect timeout).

    def call(self, peer_id, peer_ip, peer_name):
        with self._lock:
            if self.state != "idle":
                return False
            self.peer_id = peer_id
            self.peer_ip = peer_ip
            self.peer_name = peer_name
            self.state = "calling"
            self._direction = "out"
            self._call_start = None

        def send_invite():
            try:
                send_json_tcp(peer_ip, CALL_SIGNAL_PORT,
                              {"type": "INVITE", "from_id": DEVICE_ID, "from_name": self.display_name})
            except OSError:
                with self._lock:
                    if self.peer_ip == peer_ip:
                        self._log_call(peer_id, peer_name, "out", "failed")
                        self._reset_to_idle()
                self.on_event({"event": "call_failed", "peer_name": peer_name})

        threading.Thread(target=send_invite, daemon=True).start()
        return True

    def accept(self):
        with self._lock:
            if self.state != "ringing":
                return
            peer_id, peer_ip, peer_name = self.peer_id, self.peer_ip, self.peer_name

        def send_accept():
            try:
                send_json_tcp(peer_ip, CALL_SIGNAL_PORT, {"type": "ACCEPT", "from_id": DEVICE_ID})
            except OSError:
                with self._lock:
                    if self.peer_ip == peer_ip:
                        self._log_call(peer_id, peer_name, "in", "failed")
                        self._reset_to_idle()
                self.on_event({"event": "call_failed", "peer_name": peer_name})
                return
            with self._lock:
                if self.peer_ip == peer_ip:
                    self.state = "active"
                    self._call_start = time.time()
            self._start_audio()
            self.on_event({"event": "call_active", "peer_name": peer_name})

        threading.Thread(target=send_accept, daemon=True).start()

    def reject(self):
        with self._lock:
            if self.state != "ringing":
                return
            peer_id, peer_ip, peer_name, direction = self.peer_id, self.peer_ip, self.peer_name, self._direction
            self._log_call(peer_id, peer_name, direction, "rejected")
            self._reset_to_idle()

        def send_reject():
            try:
                send_json_tcp(peer_ip, CALL_SIGNAL_PORT, {"type": "REJECT", "from_id": DEVICE_ID})
            except OSError:
                pass

        threading.Thread(target=send_reject, daemon=True).start()

    def hang_up(self):
        with self._lock:
            should_notify = self.state in ("active", "calling", "ringing") and self.peer_ip
            peer_id, peer_ip, peer_name, direction = self.peer_id, self.peer_ip, self.peer_name, self._direction
            if should_notify:
                outcome = "completed" if self._call_start else (
                    "missed" if direction == "in" else "cancelled")
                self._log_call(peer_id, peer_name, direction, outcome)
            self._reset_to_idle()
        self._stop_audio()

        if should_notify:
            def send_hangup():
                try:
                    send_json_tcp(peer_ip, CALL_SIGNAL_PORT, {"type": "HANGUP", "from_id": DEVICE_ID})
                except OSError:
                    pass

            threading.Thread(target=send_hangup, daemon=True).start()

    # -- audio streaming ------------------------------------------------

    def _start_audio(self):
        self.audio = make_audio_io()
        self.audio.start_playback()

        self._audio_send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        def on_frame(data):
            if self._audio_send_sock and self.peer_ip:
                try:
                    self._audio_send_sock.sendto(data, (self.peer_ip, AUDIO_PORT))
                except OSError:
                    pass

        self.audio.start_capture(on_frame)

        self._audio_recv_running = True
        self._audio_recv_thread = threading.Thread(target=self._audio_recv_loop, daemon=True)
        self._audio_recv_thread.start()

    def _audio_recv_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", AUDIO_PORT))
        sock.settimeout(1.0)
        while self._audio_recv_running:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if self.audio and addr[0] == self.peer_ip:
                self.audio.write_playback(data)
        sock.close()

    def _stop_audio(self):
        self._audio_recv_running = False
        if self.audio:
            self.audio.stop_capture()
            self.audio.stop_playback()
            self.audio = None
        if self._audio_send_sock:
            self._audio_send_sock.close()
            self._audio_send_sock = None


# ---------------------------------------------------------------------------
# File transfer
# ---------------------------------------------------------------------------

class FileTransferManager:
    """Sends/receives arbitrary files over a dedicated TCP port. A small
    JSON header (length-prefixed, like signaling) announces the filename
    and size, followed by the raw file bytes streamed directly - no
    base64/JSON overhead for the payload itself, so large files are fine."""

    def __init__(self, display_name, store, files_dir, on_event):
        self.display_name = display_name
        self.store = store
        self.files_dir = files_dir
        self.on_event = on_event  # callback(event_dict) - caller marshals to main thread
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", FILE_PORT))
        sock.listen(5)
        sock.settimeout(1.0)
        while self._running:
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_incoming, args=(conn, addr), daemon=True).start()
        sock.close()

    def _unique_path(self, filename):
        path = os.path.join(self.files_dir, filename)
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(filename)
        n = 1
        while True:
            candidate = os.path.join(self.files_dir, f"{base} ({n}){ext}")
            if not os.path.exists(candidate):
                return candidate
            n += 1

    def _handle_incoming(self, conn, addr):
        with conn:
            try:
                header = recv_json_tcp(conn)
            except OSError:
                return
            if not header or header.get("type") != "FILE_META":
                return
            filename = os.path.basename(header.get("filename") or "file")
            size = int(header.get("size") or 0)
            peer_id = header.get("from_id", "unknown")
            peer_name = header.get("from_name", "Unknown")

            os.makedirs(self.files_dir, exist_ok=True)
            dest_path = self._unique_path(filename)

            received = 0
            status = "failed"
            try:
                conn.settimeout(30.0)
                with open(dest_path, "wb") as f:
                    while received < size:
                        chunk = conn.recv(min(FILE_CHUNK, size - received))
                        if not chunk:
                            break
                        f.write(chunk)
                        received += len(chunk)
                if received == size:
                    status = "completed"
            except OSError:
                status = "failed"

            self.store.add_file_record(peer_id, peer_name, "in", filename, size, dest_path, status)
            self.on_event({"event": "file_received", "peer_id": peer_id, "peer_name": peer_name,
                            "filename": filename, "size": size, "status": status})

    def send_file(self, peer_id, peer_name, peer_ip, file_path):
        """Blocking - call from a background thread."""
        filename = os.path.basename(file_path)
        try:
            size = os.path.getsize(file_path)
        except OSError:
            self.store.add_file_record(peer_id, peer_name, "out", filename, 0, file_path, "failed")
            self.on_event({"event": "file_send_failed", "peer_id": peer_id, "peer_name": peer_name,
                            "filename": filename, "size": 0, "status": "failed"})
            return

        header = {"type": "FILE_META", "filename": filename, "size": size,
                  "from_id": DEVICE_ID, "from_name": self.display_name}
        status = "failed"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(10.0)
                sock.connect((peer_ip, FILE_PORT))
                data = json.dumps(header).encode("utf-8")
                sock.sendall(struct.pack("!I", len(data)) + data)
                sock.settimeout(30.0)
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(FILE_CHUNK)
                        if not chunk:
                            break
                        sock.sendall(chunk)
            status = "completed"
        except OSError:
            status = "failed"

        self.store.add_file_record(peer_id, peer_name, "out", filename, size, file_path, status)
        self.on_event({"event": "file_sent" if status == "completed" else "file_send_failed",
                        "peer_id": peer_id, "peer_name": peer_name,
                        "filename": filename, "size": size, "status": status})


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

KV = """
#:import dp kivy.metrics.dp

ScreenManager:
    SetupScreen:
    UsersScreen:
    ChatScreen:
    CallScreen:

<LuxButton@Button>:
    background_normal: ""
    background_down: ""
    background_color: 0.83, 0.69, 0.42, 1
    color: 0.05, 0.06, 0.1, 1
    bold: True

<GhostButton@Button>:
    background_normal: ""
    background_down: ""
    background_color: 0.12, 0.14, 0.21, 1
    color: 0.83, 0.69, 0.42, 1
    bold: True

<DangerButton@Button>:
    background_normal: ""
    background_down: ""
    background_color: 0.80, 0.28, 0.28, 1
    color: 1, 1, 1, 1
    bold: True

<SetupScreen>:
    name: "setup"
    BoxLayout:
        orientation: "vertical"
        padding: dp(24)
        spacing: dp(16)
        canvas.before:
            Color:
                rgba: 0.035, 0.043, 0.078, 1
            Rectangle:
                pos: self.pos
                size: self.size

        Widget:
            size_hint_y: 0.3

        Label:
            text: "LANCOM"
            font_size: dp(40)
            bold: True
            color: 0.83, 0.69, 0.42, 1
            size_hint_y: None
            height: dp(52)

        Label:
            text: "LAN voice + messaging, no internet needed"
            color: 0.58, 0.58, 0.64, 1
            size_hint_y: None
            height: dp(24)

        Widget:
            size_hint_y: 0.15

        TextInput:
            id: name_input
            hint_text: "Enter your display name"
            multiline: False
            size_hint_y: None
            height: dp(48)
            padding: dp(12), dp(12)
            background_color: 0.075, 0.086, 0.13, 1
            foreground_color: 0.94, 0.93, 0.90, 1
            hint_text_color: 0.5, 0.5, 0.55, 1
            cursor_color: 0.83, 0.69, 0.42, 1

        Label:
            id: setup_error
            text: ""
            color: 0.80, 0.28, 0.28, 1
            size_hint_y: None
            height: dp(20)

        LuxButton:
            text: "Continue"
            size_hint_y: None
            height: dp(48)
            on_release: root.on_continue(name_input.text)

        Widget:

<UsersScreen>:
    name: "users"
    BoxLayout:
        orientation: "vertical"
        canvas.before:
            Color:
                rgba: 0.035, 0.043, 0.078, 1
            Rectangle:
                pos: self.pos
                size: self.size

        BoxLayout:
            size_hint_y: None
            height: dp(56)
            padding: dp(16), dp(8)
            Label:
                text: "LANCOM"
                bold: True
                font_size: dp(20)
                color: 0.83, 0.69, 0.42, 1
                halign: "left"
                text_size: self.size

        Label:
            id: debug_info
            text: ""
            font_size: dp(10)
            color: 0.4, 0.4, 0.46, 1
            size_hint_y: None
            height: dp(16)

        ScrollView:
            BoxLayout:
                id: peer_list
                orientation: "vertical"
                size_hint_y: None
                height: self.minimum_height
                padding: dp(10)
                spacing: dp(6)

<ChatScreen>:
    name: "chat"
    BoxLayout:
        orientation: "vertical"
        canvas.before:
            Color:
                rgba: 0.035, 0.043, 0.078, 1
            Rectangle:
                pos: self.pos
                size: self.size

        BoxLayout:
            size_hint_y: None
            height: dp(56)
            padding: dp(8)
            spacing: dp(8)
            canvas.before:
                Color:
                    rgba: 0.075, 0.086, 0.13, 1
                Rectangle:
                    pos: self.pos
                    size: self.size
            GhostButton:
                text: "< Back"
                size_hint_x: None
                width: dp(80)
                on_release: root.on_back()
            BoxLayout:
                orientation: "vertical"
                Label:
                    text: root.peer_name
                    bold: True
                    font_size: dp(17)
                    color: 0.94, 0.93, 0.90, 1
                    halign: "left"
                    text_size: self.size
                    valign: "bottom"
                Label:
                    text: root.peer_status
                    font_size: dp(11)
                    color: (0.36, 0.82, 0.55, 1) if root.peer_online else (0.46, 0.46, 0.52, 1)
                    halign: "left"
                    text_size: self.size
                    valign: "top"
            LuxButton:
                text: "Call"
                size_hint_x: None
                width: dp(64)
                disabled: not root.peer_online
                opacity: 1 if root.peer_online else 0.4
                on_release: root.on_call()

        ScrollView:
            id: chat_scroll
            BoxLayout:
                id: message_list
                orientation: "vertical"
                size_hint_y: None
                height: self.minimum_height
                padding: dp(10)
                spacing: dp(8)

        BoxLayout:
            size_hint_y: None
            height: dp(56)
            padding: dp(8)
            spacing: dp(8)
            GhostButton:
                text: "File"
                size_hint_x: None
                width: dp(56)
                on_release: root.on_send_file()
            TextInput:
                id: chat_input
                multiline: False
                hint_text: "Message"
                background_color: 0.075, 0.086, 0.13, 1
                foreground_color: 0.94, 0.93, 0.90, 1
                hint_text_color: 0.5, 0.5, 0.55, 1
                cursor_color: 0.83, 0.69, 0.42, 1
                on_text_validate: root.on_send(chat_input.text)
            LuxButton:
                text: "Send"
                size_hint_x: None
                width: dp(70)
                on_release: root.on_send(chat_input.text)

<CallScreen>:
    name: "call"
    BoxLayout:
        orientation: "vertical"
        padding: dp(24)
        spacing: dp(16)
        canvas.before:
            Color:
                rgba: 0.035, 0.043, 0.078, 1
            Rectangle:
                pos: self.pos
                size: self.size

        Widget:
            size_hint_y: 0.3

        Label:
            text: root.peer_name
            font_size: dp(28)
            bold: True
            color: 0.94, 0.93, 0.90, 1
            size_hint_y: None
            height: dp(40)

        Label:
            text: root.status_text
            color: 0.83, 0.69, 0.42, 1
            size_hint_y: None
            height: dp(28)

        Widget:

        BoxLayout:
            size_hint_y: None
            height: dp(64)
            spacing: dp(16)
            LuxButton:
                text: "Accept"
                opacity: 1 if root.show_accept else 0
                disabled: not root.show_accept
                on_release: root.on_accept()
            DangerButton:
                text: "Reject" if root.show_accept else "Hang Up"
                on_release: root.on_reject_or_hangup()

        Widget:
            size_hint_y: 0.2
"""


class SetupScreen(Screen):
    def on_continue(self, name):
        name = name.strip()
        app = App.get_running_app()
        if not name:
            self.ids.setup_error.text = "Please enter a name"
            return
        app.set_display_name(name)
        self.manager.current = "users"


class Avatar(Label):
    def __init__(self, name, **kwargs):
        color = _color_for_name(name or "?")
        super().__init__(text=(name[:1] or "?").upper(), bold=True, color=(1, 1, 1, 1),
                          size_hint=(None, None), size=(dp(44), dp(44)), **kwargs)
        with self.canvas.before:
            Color(*color)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(10)])
        self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *_args):
        self._rect.pos = self.pos
        self._rect.size = self.size


class ContactRow(BoxLayout):
    def __init__(self, peer, on_open_chat, on_call, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None, height=dp(68),
                          spacing=dp(12), padding=(dp(6), dp(4)), **kwargs)
        self.peer = peer
        online = peer.get("online", False)

        with self.canvas.before:
            Color(0.075, 0.086, 0.13, 1)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(12)])
        self.bind(pos=self._sync, size=self._sync)

        self.add_widget(Avatar(peer["name"]))

        info = BoxLayout(orientation="vertical", spacing=dp(2))
        name_label = Label(text=escape_markup(peer["name"]), bold=True, font_size=dp(15),
                            color=(0.94, 0.93, 0.90, 1), halign="left", valign="bottom",
                            size_hint_y=0.55)
        name_label.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        status_text = "● Online" if online else f"○ Last seen {format_last_seen(peer.get('last_seen'))}"
        status_color = (0.36, 0.82, 0.55, 1) if online else (0.46, 0.46, 0.52, 1)
        status_label = Label(text=status_text, font_size=dp(12), color=status_color,
                              halign="left", valign="top", size_hint_y=0.45)
        status_label.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        info.add_widget(name_label)
        info.add_widget(status_label)
        self.add_widget(info)

        chat_btn = Button(text="Chat", size_hint_x=None, width=dp(64),
                           background_normal="", background_color=(0.12, 0.14, 0.21, 1),
                           color=(0.83, 0.69, 0.42, 1), bold=True)
        chat_btn.bind(on_release=lambda *_: on_open_chat(peer))
        self.add_widget(chat_btn)

        call_btn = Button(text="Call", size_hint_x=None, width=dp(64), disabled=not online,
                           opacity=1 if online else 0.35,
                           background_normal="", background_color=(0.83, 0.69, 0.42, 1),
                           color=(0.05, 0.06, 0.1, 1), bold=True)
        call_btn.bind(on_release=lambda *_: on_call(peer))
        self.add_widget(call_btn)

    def _sync(self, *_args):
        self._rect.pos = self.pos
        self._rect.size = self.size


class UsersScreen(Screen):
    def on_pre_enter(self):
        Clock.schedule_interval(self.refresh, 1.5)
        self.refresh(0)

    def on_leave(self):
        Clock.unschedule(self.refresh)

    def refresh(self, _dt):
        app = App.get_running_app()
        if not app.discovery:
            return
        self.ids.debug_info.text = (
            f"This device: {app.discovery.local_ip}  "
            f"|  broadcasting to: {app.discovery.broadcast_ip}"
        )
        peers = app.discovery.get_known_peers()
        container = self.ids.peer_list
        container.clear_widgets()
        if not peers:
            container.add_widget(Label(text="Searching for devices on this WiFi...",
                                        size_hint_y=None, height=40,
                                        color=(0.5, 0.5, 0.55, 1)))
        for peer in peers:
            container.add_widget(ContactRow(peer, self.open_chat, self.call_peer))

    def open_chat(self, peer):
        chat = self.manager.get_screen("chat")
        chat.set_peer(peer)
        self.manager.transition = SlideTransition(direction="left")
        self.manager.current = "chat"

    def call_peer(self, peer):
        if not peer.get("online"):
            return
        App.get_running_app().start_call(peer["id"], peer["ip"], peer["name"])


class ChatBubble(BoxLayout):
    def __init__(self, text, sender, mine, **kwargs):
        super().__init__(orientation="vertical", size_hint_y=None, **kwargs)
        safe_sender = escape_markup(sender)
        safe_text = escape_markup(text)
        label = Label(text=f"[b]{safe_sender}[/b]\n{safe_text}", markup=True,
                      color=(0.94, 0.93, 0.90, 1),
                      halign="left" if not mine else "right",
                      size_hint_y=None)
        label.bind(texture_size=lambda inst, val: setattr(label, "height", val[1] + 10))
        label.bind(width=lambda inst, val: setattr(label, "text_size", (val, None)))
        self.add_widget(label)
        self.bind(minimum_height=self.setter("height"))


class ChatEvent(BoxLayout):
    """A muted, centered line for call/file history entries interleaved
    with messages - e.g. 'Missed call', 'Sent report.pdf (2.4 MB)'."""

    def __init__(self, text, **kwargs):
        super().__init__(orientation="vertical", size_hint_y=None, **kwargs)
        label = Label(text=escape_markup(text), font_size=dp(12),
                      color=(0.55, 0.55, 0.6, 1), size_hint_y=None, halign="center")
        label.bind(texture_size=lambda inst, val: setattr(label, "height", val[1] + 6))
        label.bind(width=lambda inst, val: setattr(label, "text_size", (val, None)))
        self.add_widget(label)
        self.bind(minimum_height=self.setter("height"))


class ChatScreen(Screen):
    peer_name = StringProperty("")
    peer_status = StringProperty("")
    peer_online = BooleanProperty(False)
    peer = None

    def set_peer(self, peer):
        self.peer = peer
        self.peer_name = peer["name"]
        self.peer_online = bool(peer.get("online"))
        self.peer_status = "Online" if self.peer_online else f"Last seen {format_last_seen(peer.get('last_seen'))}"
        self.ids.message_list.clear_widgets()
        self.load_history()

    def load_history(self):
        app = App.get_running_app()
        peer_id = self.peer["id"]
        items = []
        for m in app.store.get_messages(peer_id):
            items.append((m["timestamp"], "message", m))
        for c in app.store.get_call_log(peer_id):
            items.append((c["timestamp"], "call", c))
        for f in app.store.get_files(peer_id):
            items.append((f["timestamp"], "file", f))
        items.sort(key=lambda x: x[0])
        for _ts, kind, item in items:
            if kind == "message":
                mine = item["direction"] == "out"
                sender = app.display_name if mine else self.peer_name
                self.append_message(sender, item["text"], mine=mine)
            elif kind == "call":
                self.append_event(self._call_log_text(item))
            elif kind == "file":
                self.append_event(self._file_log_text(item))

    def on_back(self):
        self.manager.transition = SlideTransition(direction="right")
        self.manager.current = "users"

    def on_call(self):
        if not self.peer_online:
            self.append_event("Can't call - this device is offline")
            return
        App.get_running_app().start_call(self.peer["id"], self.peer["ip"], self.peer["name"])

    def on_send(self, text):
        text = text.strip()
        if not text:
            return
        app = App.get_running_app()
        peer_id = self.peer["id"]
        peer_ip = self.peer["ip"]
        peer_name = self.peer_name

        def do_send():
            try:
                app.message_server.send_message(peer_id, peer_name, peer_ip, text)
            except OSError:
                Clock.schedule_once(lambda dt: self._on_send_failed(peer_name), 0)

        threading.Thread(target=do_send, daemon=True).start()
        self.append_message(app.display_name, text, mine=True)
        self.ids.chat_input.text = ""

    def on_send_file(self):
        try:
            from plyer import filechooser
        except Exception:
            self.append_event("File picker unavailable on this device")
            return
        filechooser.open_file(on_selection=self._on_file_chosen)

    def _on_file_chosen(self, selection):
        if not selection:
            return
        Clock.schedule_once(lambda dt: self._start_file_send(selection[0]), 0)

    def _start_file_send(self, path):
        app = App.get_running_app()
        peer = self.peer
        self.append_event(f"Sending {os.path.basename(path)}…")

        def do_send():
            app.file_manager.send_file(peer["id"], peer["name"], peer["ip"], path)

        threading.Thread(target=do_send, daemon=True).start()

    def _on_send_failed(self, peer_name):
        if self.peer_name == peer_name:
            self.append_message("System", "Failed to send: peer unreachable", mine=False)

    def append_message(self, sender, text, mine):
        bubble = ChatBubble(text, sender, mine)
        self.ids.message_list.add_widget(bubble)
        Clock.schedule_once(lambda dt: setattr(self.ids.chat_scroll, "scroll_y", 0), 0.05)

    def append_event(self, text):
        self.ids.message_list.add_widget(ChatEvent(text))
        Clock.schedule_once(lambda dt: setattr(self.ids.chat_scroll, "scroll_y", 0), 0.05)

    def receive_message(self, msg):
        if self.peer and msg.get("from_id") == self.peer.get("id"):
            self.append_message(msg.get("from_name", "Unknown"), msg.get("text", ""), mine=False)

    def receive_file_event(self, event):
        if not self.peer or self.peer.get("id") != event.get("peer_id"):
            return
        direction = "in" if event["event"] == "file_received" else "out"
        row = {"direction": direction, "filename": event["filename"],
               "size": event.get("size", 0), "status": event.get("status", "failed")}
        self.append_event(self._file_log_text(row))

    @staticmethod
    def _call_log_text(c):
        icon = "\U0001F4DE"
        if c["outcome"] == "missed":
            return f"{icon} Missed call"
        if c["outcome"] == "rejected":
            return f"{icon} Call declined"
        if c["outcome"] in ("failed", "cancelled"):
            return f"{icon} Call not connected"
        mins, secs = divmod(int(c["duration"] or 0), 60)
        which = "Outgoing" if c["direction"] == "out" else "Incoming"
        return f"{icon} {which} call – {mins:02d}:{secs:02d}"

    @staticmethod
    def _file_log_text(f):
        icon = "\U0001F4CE"
        which = "Sent" if f["direction"] == "out" else "Received"
        size_str = format_size(f["size"])
        if f["status"] != "completed":
            return f"{icon} {which} {f['filename']} ({size_str}) – failed"
        return f"{icon} {which} {f['filename']} ({size_str})"


class CallScreen(Screen):
    peer_name = StringProperty("")
    status_text = StringProperty("")
    show_accept = BooleanProperty(False)
    _timer_ev = None
    _seconds = 0

    def on_incoming(self, peer_name):
        self.peer_name = peer_name
        self.status_text = "Incoming call..."
        self.show_accept = True

    def on_calling(self, peer_name):
        self.peer_name = peer_name
        self.status_text = "Calling..."
        self.show_accept = False

    def on_active(self, peer_name):
        self.peer_name = peer_name
        self.status_text = "Connected"
        self.show_accept = False
        self._seconds = 0
        self._timer_ev = Clock.schedule_interval(self._tick, 1.0)

    def on_ended(self, reason=""):
        if self._timer_ev:
            self._timer_ev.cancel()
            self._timer_ev = None
        self.status_text = reason or "Call ended"
        Clock.schedule_once(lambda dt: self._return_to_users(), 1.2)

    def _tick(self, _dt):
        self._seconds += 1
        m, s = divmod(self._seconds, 60)
        self.status_text = f"Connected - {m:02d}:{s:02d}"

    def _return_to_users(self):
        if self.manager.current == "call":
            self.manager.transition = SlideTransition(direction="down")
            self.manager.current = "users"

    def on_accept(self):
        App.get_running_app().call_manager.accept()
        self.status_text = "Connecting..."
        self.show_accept = False

    def on_reject_or_hangup(self):
        app = App.get_running_app()
        if self.show_accept:
            app.call_manager.reject()
            self._return_to_users()
        else:
            app.call_manager.hang_up()
            self.on_ended("Call ended")


class LancomApp(App):
    display_name = StringProperty("")

    def build(self):
        self.discovery = None
        self.message_server = None
        self.call_manager = None
        self.file_manager = None
        self.profile_store = JsonStore(self.user_data_dir + "/lancom.json")
        self.store = Store(os.path.join(self.user_data_dir, "lancom.db"))

        return Builder.load_string(KV)

    def on_start(self):
        if platform == "android":
            try:
                from android.permissions import request_permissions, Permission
                request_permissions([
                    Permission.RECORD_AUDIO,
                    Permission.ACCESS_WIFI_STATE,
                    Permission.ACCESS_NETWORK_STATE,
                    Permission.INTERNET,
                    Permission.READ_EXTERNAL_STORAGE,
                    Permission.WRITE_EXTERNAL_STORAGE,
                ])
            except Exception:
                pass

        if self.profile_store.exists("profile"):
            name = self.profile_store.get("profile").get("name", "")
            if name:
                self.set_display_name(name)
                self.root.current = "users"
                return
        self.root.current = "setup"

    def set_display_name(self, name):
        self.display_name = name
        self.profile_store.put("profile", name=name)

        self.discovery = PeerDiscovery(name, self.store)
        self.discovery.start()

        self.message_server = MessageServer(name, self.store, self._on_message_received)
        self.message_server.start()

        self.call_manager = CallManager(name, self.store, self._on_call_event)
        self.call_manager.start()

        files_dir = os.path.join(self.user_data_dir, "received_files")
        self.file_manager = FileTransferManager(name, self.store, files_dir, self._on_file_event)
        self.file_manager.start()

    def _on_message_received(self, msg):
        Clock.schedule_once(lambda dt: self._dispatch_message(msg), 0)

    def _dispatch_message(self, msg):
        chat = self.root.get_screen("chat")
        chat.receive_message(msg)

    def _on_call_event(self, event):
        Clock.schedule_once(lambda dt: self._dispatch_call_event(event), 0)

    def _dispatch_call_event(self, event):
        call_screen = self.root.get_screen("call")
        kind = event["event"]
        if kind == "incoming_call":
            call_screen.on_incoming(event["peer_name"])
            self.root.transition = SlideTransition(direction="up")
            self.root.current = "call"
        elif kind == "call_active":
            call_screen.on_active(event["peer_name"])
        elif kind == "call_rejected":
            call_screen.on_ended(f"{event['peer_name']} declined")
        elif kind == "call_failed":
            call_screen.on_ended("Call failed")
        elif kind == "call_ended":
            call_screen.on_ended("Call ended")

    def _on_file_event(self, event):
        Clock.schedule_once(lambda dt: self._dispatch_file_event(event), 0)

    def _dispatch_file_event(self, event):
        chat = self.root.get_screen("chat")
        chat.receive_file_event(event)

    def start_call(self, peer_id, ip, name):
        if self.call_manager.call(peer_id, ip, name):
            call_screen = self.root.get_screen("call")
            call_screen.on_calling(name)
            self.root.transition = SlideTransition(direction="up")
            self.root.current = "call"

    def on_stop(self):
        if self.discovery:
            self.discovery.stop()
        if self.message_server:
            self.message_server.stop()
        if self.call_manager:
            self.call_manager.stop()
        if self.file_manager:
            self.file_manager.stop()


if __name__ == "__main__":
    LancomApp().run()
