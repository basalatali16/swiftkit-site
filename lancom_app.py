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

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.graphics import Color, Ellipse, RoundedRectangle
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import StringProperty, BooleanProperty
from kivy.storage.jsonstore import JsonStore
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen, SlideTransition
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import platform, escape_markup

import android_notify
import crypto_util
from store import Store

BROADCAST_PORT = 55555
MESSAGE_PORT = 55556
CALL_SIGNAL_PORT = 55557
AUDIO_PORT = 55558
FILE_PORT = 55559

BROADCAST_INTERVAL = 2.0
PEER_TIMEOUT = 7.0
FILE_CHUNK = 65536
FILE_TAG_LEN = 16  # ChaCha20-Poly1305 auth tag length appended to each chunk

# Set once by LancomApp.build() before any networking starts. Every
# device's id is derived from this identity's public key (see
# crypto_util.peer_id_for_pubkey) - it can't be spoofed without also
# having the matching private key.
IDENTITY = None

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
COLOR_BUBBLE_MINE = (0.27, 0.22, 0.12, 1)  # own messages - warm gold-dark
COLOR_CHIP = (0.10, 0.115, 0.17, 1)  # date separator chips

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


def format_time(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def format_date_chip(ts):
    day = time.localtime(ts)
    today = time.localtime()
    if (day.tm_year, day.tm_yday) == (today.tm_year, today.tm_yday):
        return "Today"
    yesterday = time.localtime(time.time() - 86400)
    if (day.tm_year, day.tm_yday) == (yesterday.tm_year, yesterday.tm_yday):
        return "Yesterday"
    return time.strftime("%d %b %Y", day)


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


def resolve_android_content_uri(uri_str, dest_dir):
    """Android's Storage Access Framework file picker (used by
    plyer.filechooser) commonly returns a content:// URI rather than a
    plain filesystem path - open(path, "rb") can't read that directly.
    Copy it via ContentResolver to a real local file first and send that
    instead. Returns the local path."""
    from jnius import autoclass

    Uri = autoclass("android.net.Uri")
    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    OpenableColumns = autoclass("android.provider.OpenableColumns")

    activity = PythonActivity.mActivity
    resolver = activity.getContentResolver()
    uri = Uri.parse(uri_str)

    display_name = "file"
    cursor = resolver.query(uri, None, None, None, None)
    if cursor is not None:
        try:
            if cursor.moveToFirst():
                idx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if idx >= 0:
                    name = cursor.getString(idx)
                    if name:
                        display_name = name
        finally:
            cursor.close()

    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, os.path.basename(display_name))

    input_stream = resolver.openInputStream(uri)
    if input_stream is None:
        raise OSError(f"ContentResolver couldn't open {uri_str}")
    try:
        buf = bytearray(65536)
        with open(dest_path, "wb") as out:
            while True:
                n = input_stream.read(buf)
                if n == -1:
                    break
                out.write(bytes(buf[:n]))
    finally:
        input_stream.close()

    return dest_path


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


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


MAX_MESSAGE_BYTES = 64 * 1024
PEER_ID_LEN = 16  # crypto_util.peer_id_for_pubkey() hex length


def send_encrypted_blob(sock, key, obj):
    """Encrypt+send one JSON message on an already-open, already-identified
    connection - no peer-id preamble, since the caller already knows which
    key to use (either it dialed this peer, or already read the preamble
    itself). Shared by the signaling/messaging transport and the file
    transfer's resume-offset handshake."""
    blob = crypto_util.encrypt(key, json.dumps(obj).encode("utf-8"))
    sock.sendall(struct.pack("!I", len(blob)) + blob)


def recv_encrypted_blob(sock, key):
    header = recv_exact(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack("!I", header)
    if length > MAX_MESSAGE_BYTES:
        return None
    blob = recv_exact(sock, length)
    if blob is None:
        return None
    try:
        plaintext = crypto_util.decrypt(key, blob)
        return json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None


def send_encrypted_tcp(ip, port, obj, peer_pubkey_bytes, timeout=4.0):
    """Send one authenticated-encrypted JSON message over a fresh TCP
    connection. Wire format: our 16-byte peer id (plaintext - it's public,
    just tells the receiver which pubkey to use), then the sealed payload."""
    key = IDENTITY.shared_key_with(peer_pubkey_bytes)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((ip, port))
        sock.sendall(IDENTITY.peer_id.encode("ascii"))
        send_encrypted_blob(sock, key, obj)


def recv_encrypted_tcp(conn, pubkey_lookup_fn):
    """Reads the peer-id preamble, looks up that peer's stored public key,
    derives the shared key, and decrypts. Returns None on any failure
    (unknown peer, tampered/wrong-key ciphertext, truncated read) - there
    is no plaintext fallback. The returned dict's "_sender_id" is
    cryptographically authenticated: forging it would derive the wrong
    key and decryption would fail."""
    sender_id_bytes = recv_exact(conn, PEER_ID_LEN)
    if sender_id_bytes is None:
        return None
    sender_id = sender_id_bytes.decode("ascii", errors="replace")
    pubkey_b64 = pubkey_lookup_fn(sender_id)
    if not pubkey_b64:
        return None
    try:
        peer_pubkey = base64.b64decode(pubkey_b64)
    except Exception:
        return None

    key = IDENTITY.shared_key_with(peer_pubkey)
    obj = recv_encrypted_blob(conn, key)
    if obj is None:
        return None
    obj["_sender_id"] = sender_id
    return obj


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
        self._listen_sock = None

    def start(self):
        self._running = True
        # The listen socket doubles as the source socket for unicast probes
        # and probe replies: packets sent from it carry our discovery port
        # as their source port, so the other side can answer straight back
        # to the packet's source address and hit our listener - no extra
        # port negotiation on the wire.
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            sock.bind(("", BROADCAST_PORT))
            sock.settimeout(1.0)
            self._listen_sock = sock
        except OSError:
            self._listen_sock = None
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        threading.Thread(target=self._listen_loop, daemon=True).start()
        threading.Thread(target=self._reap_loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _hello_data(self, probe=False):
        payload = {
            "type": "LANCOM_HELLO",
            "id": IDENTITY.peer_id,
            "name": self.display_name,
            "pubkey": base64.b64encode(IDENTITY.public_bytes).decode("ascii"),
        }
        if probe:
            payload["probe"] = True
        return json.dumps(payload).encode("utf-8")

    def probe_ip(self, ip, port=None):
        """Announce ourselves directly (unicast) to one specific address and
        ask it to announce back. This is the discovery path for networks
        where the router filters UDP broadcast - both sides learn each
        other from a single probe, no broadcast required."""
        sock = self._listen_sock
        if sock is None:
            return False
        try:
            sock.sendto(self._hello_data(probe=True),
                        (ip, port if port is not None else BROADCAST_PORT))
            return True
        except OSError:
            return False

    def _reprobe_stale_peers(self):
        """Unicast HELLOs to known contacts we haven't heard from recently.
        On broadcast-filtering networks this is what keeps contacts online
        (and re-finds stored ones after an app restart): their replies
        refresh last_seen before the reaper would flag them offline."""
        now = time.time()
        with self._lock:
            fresh = {pid for pid, p in self.peers.items()
                     if now - p["last_seen"] <= PEER_TIMEOUT / 2}
        try:
            known = self.store.get_known_peers()
        except Exception:
            return
        for p in known:
            if p["id"] not in fresh and p.get("ip"):
                self.probe_ip(p["ip"])

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        while self._running:
            try:
                sock.sendto(self._hello_data(), (self.broadcast_ip, BROADCAST_PORT))
            except OSError:
                pass
            self._reprobe_stale_peers()
            time.sleep(BROADCAST_INTERVAL)
        sock.close()

    def _listen_loop(self):
        sock = self._listen_sock
        if sock is None:
            return
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
            if msg.get("id") == IDENTITY.peer_id:
                continue
            pubkey_b64 = msg.get("pubkey")
            try:
                pubkey_bytes = base64.b64decode(pubkey_b64) if pubkey_b64 else b""
            except Exception:
                continue
            # The id must actually be derived from the claimed pubkey -
            # otherwise anyone could broadcast a HELLO claiming to be an
            # id they don't hold the private key for.
            if not pubkey_bytes or crypto_util.peer_id_for_pubkey(pubkey_bytes) != msg.get("id"):
                continue
            peer_id = msg["id"]
            name = msg.get("name", "Unknown")
            with self._lock:
                self.peers[peer_id] = {
                    "id": peer_id,
                    "name": name,
                    "ip": addr[0],
                    "pubkey": pubkey_b64,
                    "last_seen": time.time(),
                }
            self.store.upsert_peer(peer_id, name, addr[0], pubkey_b64)
            if msg.get("probe"):
                # A directed probe means our broadcasts likely never reach
                # this device - answer straight back to the packet's source
                # (their listen socket) so it learns us too.
                try:
                    sock.sendto(self._hello_data(), addr)
                except OSError:
                    pass
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
                                "pubkey": live_info["pubkey"],
                                "online": True, "last_seen": live_info["last_seen"]})
            else:
                result.append({"id": p["id"], "name": p["name"], "ip": p["ip"],
                                "pubkey": p["pubkey"],
                                "online": False, "last_seen": p["last_seen"]})
        for pid, info in live.items():
            if pid not in seen_ids:
                result.append({"id": pid, "name": info["name"], "ip": info["ip"],
                                "pubkey": info["pubkey"],
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
                msg = recv_encrypted_tcp(conn, self.store.get_peer_pubkey)
            except OSError:
                return
            if not msg:
                return
            msg["ip"] = addr[0]
            # _sender_id is authenticated by successful decryption - trust
            # it over any from_id the JSON body itself might claim.
            sender_id = msg["_sender_id"]
            self.store.add_message(sender_id, msg.get("from_name", "Unknown"),
                                    "in", msg.get("text", ""))
            msg["from_id"] = sender_id
            self.on_message(msg)

    def send_message(self, peer_id, peer_name, peer_ip, text):
        pubkey_b64 = self.store.get_peer_pubkey(peer_id)
        if not pubkey_b64:
            raise OSError(f"no known public key for peer {peer_id}")
        self.store.add_message(peer_id, peer_name, "out", text)
        payload = {
            "type": "MSG",
            "from_name": self.display_name,
            "text": text,
            "timestamp": time.time(),
        }
        send_encrypted_tcp(peer_ip, MESSAGE_PORT, payload, base64.b64decode(pubkey_b64))


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
        self._call_salt = None  # random per-call salt -> distinct audio key per call
        self._call_key = None
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
        self._call_salt = None
        self._call_key = None

    def _log_call(self, peer_id, peer_name, direction, outcome):
        duration = (time.time() - self._call_start) if self._call_start else 0.0
        self.store.add_call_log(peer_id or "unknown", peer_name or "Unknown",
                                 direction or "out", outcome, duration)

    def _handle_signal(self, conn, addr):
        with conn:
            try:
                msg = recv_encrypted_tcp(conn, self.store.get_peer_pubkey)
            except OSError:
                return
            if not msg:
                return
            msg_type = msg.get("type")
            sender_id = msg["_sender_id"]

            if msg_type == "INVITE":
                with self._lock:
                    busy = self.state != "idle"
                    if not busy:
                        self.peer_id = sender_id
                        self.peer_ip = addr[0]
                        self.peer_name = msg.get("from_name", "Unknown")
                        self.state = "ringing"
                        self._direction = "in"
                        self._call_start = None
                        try:
                            self._call_salt = base64.b64decode(msg.get("call_salt", ""))
                        except Exception:
                            self._call_salt = None
                if busy:
                    pubkey_b64 = self.store.get_peer_pubkey(sender_id)
                    if pubkey_b64:
                        try:
                            send_encrypted_tcp(addr[0], CALL_SIGNAL_PORT, {"type": "REJECT", "reason": "busy"},
                                                base64.b64decode(pubkey_b64))
                        except OSError:
                            pass
                    return
                if not self._call_salt:
                    with self._lock:
                        self._reset_to_idle()
                    return
                self.on_event({"event": "incoming_call", "peer_name": self.peer_name, "peer_ip": self.peer_ip})
                return

            # ACCEPT/REJECT/HANGUP only apply to messages from the peer we're
            # actually talking to. sender_id is cryptographically
            # authenticated (forging it would derive the wrong decryption
            # key), unlike the IP-based check this used to rely on, which a
            # device on the same LAN could spoof.
            with self._lock:
                from_current_peer = self.peer_id is not None and sender_id == self.peer_id
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
    # block the Kivy UI thread on a slow/dead peer (send_encrypted_tcp has
    # up to a 4s connect timeout).

    def call(self, peer_id, peer_ip, peer_name):
        pubkey_b64 = self.store.get_peer_pubkey(peer_id)
        if not pubkey_b64:
            return False
        with self._lock:
            if self.state != "idle":
                return False
            self.peer_id = peer_id
            self.peer_ip = peer_ip
            self.peer_name = peer_name
            self.state = "calling"
            self._direction = "out"
            self._call_start = None
            self._call_salt = os.urandom(16)
            call_salt = self._call_salt

        def send_invite():
            try:
                send_encrypted_tcp(peer_ip, CALL_SIGNAL_PORT, {
                    "type": "INVITE", "from_name": self.display_name,
                    "call_salt": base64.b64encode(call_salt).decode("ascii"),
                }, base64.b64decode(pubkey_b64))
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
        pubkey_b64 = self.store.get_peer_pubkey(peer_id)
        if not pubkey_b64:
            with self._lock:
                self._reset_to_idle()
            self.on_event({"event": "call_failed", "peer_name": peer_name})
            return

        def send_accept():
            try:
                send_encrypted_tcp(peer_ip, CALL_SIGNAL_PORT, {"type": "ACCEPT"},
                                    base64.b64decode(pubkey_b64))
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
        pubkey_b64 = self.store.get_peer_pubkey(peer_id)

        def send_reject():
            if not pubkey_b64:
                return
            try:
                send_encrypted_tcp(peer_ip, CALL_SIGNAL_PORT, {"type": "REJECT"},
                                    base64.b64decode(pubkey_b64))
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
            pubkey_b64 = self.store.get_peer_pubkey(peer_id)

            def send_hangup():
                if not pubkey_b64:
                    return
                try:
                    send_encrypted_tcp(peer_ip, CALL_SIGNAL_PORT, {"type": "HANGUP"},
                                        base64.b64decode(pubkey_b64))
                except OSError:
                    pass

            threading.Thread(target=send_hangup, daemon=True).start()

    # -- audio streaming ------------------------------------------------
    # Each call gets its own symmetric key (derived from the pair's shared
    # secret plus a random per-call salt exchanged in INVITE), so keys
    # aren't reused across calls even though the underlying identity keys
    # are static. Frames use random nonces since UDP can reorder/drop
    # packets, so a counter can't be safely relied on for uniqueness here.

    def _start_audio(self):
        pubkey_b64 = self.store.get_peer_pubkey(self.peer_id)
        if not pubkey_b64 or not self._call_salt:
            return
        peer_pubkey = base64.b64decode(pubkey_b64)
        self._call_key = IDENTITY.shared_key_with(peer_pubkey, context=b"lancom-call:" + self._call_salt)

        self.audio = make_audio_io()
        self.audio.start_playback()

        self._audio_send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        def on_frame(data):
            if self._audio_send_sock and self.peer_ip and self._call_key:
                try:
                    encrypted = crypto_util.encrypt(self._call_key, data)
                    self._audio_send_sock.sendto(encrypted, (self.peer_ip, AUDIO_PORT))
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
            if self.audio and self._call_key and addr[0] == self.peer_ip:
                try:
                    decrypted = crypto_util.decrypt(self._call_key, data)
                except Exception:
                    continue
                self.audio.write_playback(decrypted)
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
    """Sends/receives arbitrary files over a dedicated TCP port, resumable
    from a connection drop - built for GB-scale transfers where restarting
    from zero after a WiFi hiccup isn't acceptable.

    Protocol per attempt:
      1. Sender connects, sends an encrypted FILE_META header (filename,
         size, a stable transfer_id, and a fresh per-attempt salt).
      2. Receiver looks up transfer_id in its local `transfers` table. If a
         resumable partial file exists with a byte count that actually
         matches what's on disk, it acks that offset; otherwise it acks 0
         and (re)starts the transfer record. The receiver's on-disk state
         is the single source of truth for where to resume, not whatever
         the sender remembers locally.
      3. Sender seeks to that offset and streams the remaining bytes as
         chunks, each independently encrypted (ChaCha20-Poly1305, counter
         nonce restarting at 0 for *this attempt's* chunks - safe because
         each attempt gets its own fresh salt/key, so key reuse across
         attempts never happens).
      4. Progress is checkpointed to the DB periodically so a resume is
         possible even if the app itself restarts mid-transfer, not just on
         a transient network drop.
    """

    PROGRESS_CHECKPOINT_CHUNKS = 16  # ~1MB at the 64KB chunk size

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

    @staticmethod
    def transfer_id_for(peer_id, filename, size):
        return hashlib.sha256(f"{peer_id}:{filename}:{size}".encode("utf-8")).hexdigest()[:16]

    def _unique_path(self, filename):
        path = os.path.join(self.files_dir, filename)
        if not os.path.exists(path) and not os.path.exists(path + ".partial"):
            return path
        base, ext = os.path.splitext(filename)
        n = 1
        while True:
            candidate = os.path.join(self.files_dir, f"{base} ({n}){ext}")
            if not os.path.exists(candidate) and not os.path.exists(candidate + ".partial"):
                return candidate
            n += 1

    def _handle_incoming(self, conn, addr):
        with conn:
            try:
                header = recv_encrypted_tcp(conn, self.store.get_peer_pubkey)
            except OSError:
                return
            if not header or header.get("type") != "FILE_META":
                return
            peer_id = header["_sender_id"]
            peer_name = header.get("from_name", "Unknown")
            filename = os.path.basename(header.get("filename") or "file")
            size = int(header.get("size") or 0)
            transfer_id = header.get("transfer_id") or self.transfer_id_for(peer_id, filename, size)

            pubkey_b64 = self.store.get_peer_pubkey(peer_id)
            try:
                transfer_salt = base64.b64decode(header.get("salt", ""))
            except Exception:
                transfer_salt = b""
            if not pubkey_b64 or not transfer_salt or size <= 0:
                return
            peer_pubkey = base64.b64decode(pubkey_b64)
            file_key = IDENTITY.shared_key_with(peer_pubkey, context=b"lancom-file:" + transfer_salt)
            header_key = IDENTITY.shared_key_with(peer_pubkey)

            os.makedirs(self.files_dir, exist_ok=True)

            existing = self.store.get_transfer(transfer_id)
            dest_path = None
            offset = 0
            if (existing and existing["status"] == "in_progress" and existing["size"] == size
                    and existing["direction"] == "in"):
                candidate = existing["path"]
                if os.path.exists(candidate) and os.path.getsize(candidate) == existing["bytes_done"]:
                    dest_path = candidate
                    offset = existing["bytes_done"]

            if dest_path is None:
                dest_path = self._unique_path(filename) + ".partial"
                offset = 0
                self.store.start_transfer(transfer_id, peer_id, "in", filename, dest_path,
                                           size, header.get("salt", ""))

            try:
                send_encrypted_blob(conn, header_key, {"type": "FILE_RESUME_ACK", "offset": offset})
            except OSError:
                return

            if offset >= size:
                # Already had the whole thing (e.g. a duplicate resume
                # attempt racing a completion) - nothing left to receive.
                self._finish_incoming(transfer_id, peer_id, peer_name, filename, size, dest_path, "completed")
                return

            received = offset
            chunk_index = 0
            status = "failed"
            try:
                conn.settimeout(30.0)
                with open(dest_path, "r+b" if offset else "wb") as f:
                    f.seek(offset)
                    while received < size:
                        plain_len = min(FILE_CHUNK, size - received)
                        blob = recv_exact(conn, plain_len + FILE_TAG_LEN)
                        if blob is None:
                            break
                        nonce = chunk_index.to_bytes(12, "big")
                        plaintext = crypto_util.decrypt_with_nonce(file_key, nonce, blob)
                        f.write(plaintext)
                        received += len(plaintext)
                        chunk_index += 1
                        if chunk_index % self.PROGRESS_CHECKPOINT_CHUNKS == 0:
                            self.store.update_transfer_progress(transfer_id, received)
                if received == size:
                    status = "completed"
            except OSError:
                # Connection dropped - keep the partial file and DB progress
                # as-is so a later attempt with the same transfer_id resumes
                # from here instead of restarting.
                status = "failed"
            except Exception:
                # Tampered/undecryptable chunk - this attempt is unsafe to
                # resume from (we don't know how much of what's on disk is
                # trustworthy), so drop the whole transfer rather than risk
                # silently keeping corrupted bytes.
                status = "corrupt"

            if status == "corrupt":
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
                self.store.finish_transfer(transfer_id, "failed")
                self.on_event({"event": "file_received", "peer_id": peer_id, "peer_name": peer_name,
                                "filename": filename, "size": size, "status": "failed"})
                return

            self.store.update_transfer_progress(transfer_id, received)
            self._finish_incoming(transfer_id, peer_id, peer_name, filename, size, dest_path, status)

    def _finish_incoming(self, transfer_id, peer_id, peer_name, filename, size, dest_path, status):
        if status == "completed":
            final_path = dest_path[:-len(".partial")] if dest_path.endswith(".partial") else dest_path
            try:
                os.replace(dest_path, final_path)
            except OSError:
                final_path = dest_path
            self.store.finish_transfer(transfer_id, "completed")
            self.store.add_file_record(peer_id, peer_name, "in", filename, size, final_path, "completed")
        self.on_event({"event": "file_received", "peer_id": peer_id, "peer_name": peer_name,
                        "filename": filename, "size": size, "status": status})

    def send_file(self, peer_id, peer_name, peer_ip, file_path):
        """Blocking - call from a background thread. Safe to call again
        with the same (peer, filename, size) after a failure: it'll ask the
        receiver where to resume from rather than starting over."""
        filename = os.path.basename(file_path)
        pubkey_b64 = self.store.get_peer_pubkey(peer_id)
        try:
            size = os.path.getsize(file_path)
        except OSError:
            size = 0
        if not pubkey_b64 or not size:
            self.store.add_file_record(peer_id, peer_name, "out", filename, size, file_path, "failed")
            self.on_event({"event": "file_send_failed", "peer_id": peer_id, "peer_name": peer_name,
                            "filename": filename, "size": size, "status": "failed"})
            return

        transfer_id = self.transfer_id_for(peer_id, filename, size)
        peer_pubkey = base64.b64decode(pubkey_b64)
        transfer_salt = os.urandom(16)
        file_key = IDENTITY.shared_key_with(peer_pubkey, context=b"lancom-file:" + transfer_salt)
        header_key = IDENTITY.shared_key_with(peer_pubkey)
        self.store.start_transfer(transfer_id, peer_id, "out", filename, file_path,
                                   size, base64.b64encode(transfer_salt).decode("ascii"))

        status = "failed"
        sent_total = 0
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(10.0)
                sock.connect((peer_ip, FILE_PORT))
                sock.sendall(IDENTITY.peer_id.encode("ascii"))
                send_encrypted_blob(sock, header_key, {
                    "type": "FILE_META", "filename": filename, "size": size,
                    "from_name": self.display_name, "transfer_id": transfer_id,
                    "salt": base64.b64encode(transfer_salt).decode("ascii"),
                })

                sock.settimeout(15.0)
                ack = recv_encrypted_blob(sock, header_key)
                offset = min(int(ack.get("offset", 0)), size) if ack else 0
                sent_total = offset

                sock.settimeout(60.0)
                chunk_index = 0
                with open(file_path, "rb") as f:
                    f.seek(offset)
                    while True:
                        chunk = f.read(FILE_CHUNK)
                        if not chunk:
                            break
                        nonce = chunk_index.to_bytes(12, "big")
                        encrypted = crypto_util.encrypt_with_nonce(file_key, nonce, chunk)
                        sock.sendall(encrypted)
                        chunk_index += 1
                        sent_total += len(chunk)
                        if chunk_index % self.PROGRESS_CHECKPOINT_CHUNKS == 0:
                            self.store.update_transfer_progress(transfer_id, sent_total)
            status = "completed"
        except OSError:
            status = "failed"

        self.store.update_transfer_progress(transfer_id, sent_total)
        if status == "completed":
            self.store.finish_transfer(transfer_id, "completed")
            self.store.add_file_record(peer_id, peer_name, "out", filename, size, file_path, "completed")

        self.on_event({"event": "file_sent" if status == "completed" else "file_send_failed",
                        "peer_id": peer_id, "peer_name": peer_name,
                        "filename": filename, "size": size, "status": status,
                        "transfer_id": transfer_id, "path": file_path})


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
    background_color: 0, 0, 0, 0
    color: 0.05, 0.06, 0.1, 1
    bold: True
    canvas.before:
        Color:
            rgba: (0.64, 0.52, 0.30, 1) if self.state == "down" else (0.83, 0.69, 0.42, 1)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(12)]

<GhostButton@Button>:
    background_normal: ""
    background_down: ""
    background_color: 0, 0, 0, 0
    color: 0.83, 0.69, 0.42, 1
    bold: True
    canvas.before:
        Color:
            rgba: (0.18, 0.21, 0.30, 1) if self.state == "down" else (0.12, 0.14, 0.21, 1)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(12)]

<DangerButton@Button>:
    background_normal: ""
    background_down: ""
    background_color: 0, 0, 0, 0
    color: 1, 1, 1, 1
    bold: True
    canvas.before:
        Color:
            rgba: (0.60, 0.20, 0.20, 1) if self.state == "down" else (0.80, 0.28, 0.28, 1)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(12)]

<PillGhostButton@Button>:
    background_normal: ""
    background_down: ""
    background_color: 0, 0, 0, 0
    color: 0.83, 0.69, 0.42, 1
    bold: True
    canvas.before:
        Color:
            rgba: (0.18, 0.21, 0.30, 1) if self.state == "down" else (0.12, 0.14, 0.21, 1)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [self.height / 2.0]

<PillLuxButton@Button>:
    background_normal: ""
    background_down: ""
    background_color: 0, 0, 0, 0
    color: 0.05, 0.06, 0.1, 1
    bold: True
    canvas.before:
        Color:
            rgba: (0.64, 0.52, 0.30, 1) if self.state == "down" else (0.83, 0.69, 0.42, 1)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [self.height / 2.0]

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
            spacing: dp(8)
            Label:
                text: "LANCOM"
                bold: True
                font_size: dp(20)
                color: 0.83, 0.69, 0.42, 1
                halign: "left"
                valign: "middle"
                text_size: self.size
            GhostButton:
                text: "+ IP"
                size_hint: None, None
                width: dp(68)
                height: dp(36)
                pos_hint: {"center_y": 0.5}
                on_release: root.show_add_ip_popup()

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
            height: dp(52)
            padding: dp(6), dp(6)
            spacing: dp(8)
            canvas.before:
                Color:
                    rgba: 0.075, 0.086, 0.13, 1
                Rectangle:
                    pos: self.pos
                    size: self.size
            GhostButton:
                text: "‹"
                font_size: dp(26)
                size_hint_x: None
                width: dp(44)
                on_release: root.on_back()
            BoxLayout:
                orientation: "vertical"
                Label:
                    text: root.peer_name
                    bold: True
                    font_size: dp(16)
                    color: 0.94, 0.93, 0.90, 1
                    halign: "left"
                    text_size: self.size
                    valign: "bottom"
                Label:
                    text: root.peer_status
                    font_size: dp(10)
                    color: (0.36, 0.82, 0.55, 1) if root.peer_online else (0.46, 0.46, 0.52, 1)
                    halign: "left"
                    text_size: self.size
                    valign: "top"
            GhostButton:
                text: "ID"
                size_hint_x: None
                width: dp(44)
                on_release: root.show_fingerprint()
            LuxButton:
                text: "Call"
                size_hint_x: None
                width: dp(60)
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
                spacing: dp(5)

        BoxLayout:
            size_hint_y: None
            height: dp(60)
            padding: dp(8), dp(8)
            spacing: dp(8)
            PillGhostButton:
                text: "+"
                font_size: dp(26)
                size_hint_x: None
                width: dp(44)
                on_release: root.on_send_file()
            BoxLayout:
                padding: dp(14), dp(4)
                canvas.before:
                    Color:
                        rgba: 0.075, 0.086, 0.13, 1
                    RoundedRectangle:
                        pos: self.pos
                        size: self.size
                        radius: [self.height / 2.0]
                TextInput:
                    id: chat_input
                    multiline: False
                    hint_text: "Message"
                    background_normal: ""
                    background_active: ""
                    background_color: 0, 0, 0, 0
                    foreground_color: 0.94, 0.93, 0.90, 1
                    hint_text_color: 0.5, 0.5, 0.55, 1
                    cursor_color: 0.83, 0.69, 0.42, 1
                    padding: dp(4), max(0, (self.height - self.line_height) / 2)
                    on_text_validate: root.on_send(chat_input.text)
            PillLuxButton:
                text: "Send"
                size_hint_x: None
                width: dp(64)
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
    """Colored initial tile. Pass online=True/False to draw a status dot on
    the bottom-right corner - a drawn circle, not a text glyph, because
    Kivy's bundled Roboto has no dot/emoji characters (they render as
    hollow boxes)."""

    DOT = 13

    def __init__(self, name, online=None, **kwargs):
        color = _color_for_name(name or "?")
        super().__init__(text=(name[:1] or "?").upper(), bold=True, color=(1, 1, 1, 1),
                          size_hint=(None, None), size=(dp(44), dp(44)), **kwargs)
        self._dot = self._dot_ring = None
        with self.canvas.before:
            Color(*color)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(10)])
        if online is not None:
            with self.canvas.after:
                # Dark ring behind the dot so it reads against any avatar color.
                Color(*COLOR_BG)
                self._dot_ring = Ellipse(pos=self.pos, size=(dp(self.DOT + 4),) * 2)
                Color(*(COLOR_ONLINE if online else COLOR_OFFLINE))
                self._dot = Ellipse(pos=self.pos, size=(dp(self.DOT),) * 2)
        self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *_args):
        self._rect.pos = self.pos
        self._rect.size = self.size
        if self._dot is not None:
            self._dot_ring.pos = (self.right - dp(self.DOT + 3), self.y - dp(2))
            self._dot.pos = (self.right - dp(self.DOT + 1), self.y)


class RoundButton(Button):
    """Flat rounded-rectangle button with a visibly darker pressed state -
    the Python-side counterpart of the KV Lux/Ghost/Danger button rules,
    for widgets that are built in code rather than in the KV string."""

    def __init__(self, bg, radius=None, **kwargs):
        super().__init__(background_normal="", background_down="",
                          background_color=(0, 0, 0, 0), **kwargs)
        self._bg = bg
        self._bg_down = tuple(c * 0.75 for c in bg[:3]) + (bg[3],)
        with self.canvas.before:
            self._color = Color(*bg)
            self._rect = RoundedRectangle(pos=self.pos, size=self.size,
                                           radius=radius or [dp(12)])
        self.bind(pos=self._sync, size=self._sync, state=self._on_state)

    def _sync(self, *_args):
        self._rect.pos = self.pos
        self._rect.size = self.size

    def _on_state(self, _inst, state):
        self._color.rgba = self._bg_down if state == "down" else self._bg


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

        self.add_widget(Avatar(peer["name"], online=online,
                                pos_hint={"center_y": 0.5}))

        info = BoxLayout(orientation="vertical", spacing=dp(2))
        # shorten=True keeps each line single-height with a trailing
        # ellipsis instead of letter-wrapping when a name outgrows the
        # space between the avatar and the buttons.
        name_label = Label(text=escape_markup(peer["name"]), bold=True, font_size=dp(15),
                            color=(0.94, 0.93, 0.90, 1), halign="left", valign="bottom",
                            shorten=True, shorten_from="right", size_hint_y=0.55)
        name_label.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        status_text = "Online" if online else f"Last seen {format_last_seen(peer.get('last_seen'))}"
        status_color = (0.36, 0.82, 0.55, 1) if online else (0.46, 0.46, 0.52, 1)
        status_label = Label(text=status_text, font_size=dp(12), color=status_color,
                              halign="left", valign="top", shorten=True,
                              shorten_from="right", size_hint_y=0.45)
        status_label.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        info.add_widget(name_label)
        info.add_widget(status_label)
        self.add_widget(info)

        chat_btn = RoundButton(bg=(0.12, 0.14, 0.21, 1), text="Chat",
                                size_hint=(None, None), size=(dp(56), dp(38)),
                                pos_hint={"center_y": 0.5}, font_size=dp(13),
                                color=(0.83, 0.69, 0.42, 1), bold=True)
        chat_btn.bind(on_release=lambda *_: on_open_chat(peer))
        self.add_widget(chat_btn)

        call_btn = RoundButton(bg=(0.83, 0.69, 0.42, 1), text="Call",
                                size_hint=(None, None), size=(dp(56), dp(38)),
                                pos_hint={"center_y": 0.5}, disabled=not online,
                                opacity=1 if online else 0.35, font_size=dp(13),
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

    def show_add_ip_popup(self):
        """Manual fallback for networks where the router filters UDP
        broadcast, so devices never see each other automatically: type the
        other device's IP (shown in its own debug line on this screen) and
        we probe it directly."""
        app = App.get_running_app()
        content = BoxLayout(orientation="vertical", spacing=dp(10),
                             padding=(dp(8), dp(8)))
        hint = Label(
            text=("If devices on this network can't find each other "
                  "automatically, type the other device's IP address. "
                  "It's shown at the top of their contacts screen."),
            font_size=dp(12), color=COLOR_TEXT_DIM, halign="center",
            valign="middle", size_hint_y=None, height=dp(64))
        hint.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        ip_input = TextInput(
            hint_text="e.g. 192.168.1.23", multiline=False,
            size_hint_y=None, height=dp(44), padding=(dp(12), dp(12)),
            background_color=COLOR_CARD, foreground_color=COLOR_TEXT,
            hint_text_color=(0.5, 0.5, 0.55, 1), cursor_color=COLOR_GOLD,
            input_filter=lambda s, _undo: "".join(c for c in s if c in "0123456789."),
        )
        error = Label(text="", font_size=dp(12), color=COLOR_DANGER,
                       size_hint_y=None, height=dp(18))
        add_btn = RoundButton(bg=COLOR_GOLD, text="Add device", bold=True,
                               color=(0.05, 0.06, 0.1, 1),
                               size_hint_y=None, height=dp(44))
        content.add_widget(hint)
        content.add_widget(ip_input)
        content.add_widget(error)
        content.add_widget(add_btn)
        popup = Popup(title="Add device by IP", content=content,
                       size_hint=(0.92, None), height=dp(300))

        def do_add(*_args):
            ip = ip_input.text.strip()
            parts = ip.split(".")
            valid = (len(parts) == 4
                     and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts))
            if not valid:
                error.text = "That doesn't look like a valid IP address"
                return
            if app.discovery:
                app.discovery.probe_ip(ip)
            popup.dismiss()

        add_btn.bind(on_release=do_add)
        ip_input.bind(on_text_validate=do_add)
        popup.open()

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
    """WhatsApp-style rounded message bubble. Own messages sit right in a
    warm gold-dark bubble, the peer's sit left in a card-colored one, and
    the timestamp renders small and dim inside the bubble after the text.
    No sender name - chats here are always 1:1 and the header says who."""

    MAX_FRAC = 0.75  # bubble never grows past this fraction of the row
    PAD_X = 12
    PAD_Y = 8

    def __init__(self, text, mine, timestamp=None, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None,
                          padding=(dp(2), dp(1)), **kwargs)
        body = escape_markup(text)
        if timestamp:
            body += (f" [size={int(dp(10))}][color=#9a93a4]"
                     f"{format_time(timestamp)}[/color][/size]")

        self._label = Label(text=body, markup=True, color=COLOR_TEXT,
                             halign="left", size_hint=(None, None))
        self._bubble = BoxLayout(size_hint=(None, None),
                                  padding=(dp(self.PAD_X), dp(self.PAD_Y)))
        with self._bubble.canvas.before:
            Color(*(COLOR_BUBBLE_MINE if mine else COLOR_CARD))
            # Tighter corner on the side the bubble "points" from, like a
            # speech-bubble tail. Radius order: TL, TR, BR, BL.
            radius = ([dp(14), dp(14), dp(4), dp(14)] if mine
                      else [dp(14), dp(14), dp(14), dp(4)])
            self._rect = RoundedRectangle(pos=self._bubble.pos,
                                           size=self._bubble.size, radius=radius)
        self._bubble.bind(pos=self._sync, size=self._sync)
        self._bubble.add_widget(self._label)

        if mine:
            self.add_widget(Widget())
            self.add_widget(self._bubble)
        else:
            self.add_widget(self._bubble)
            self.add_widget(Widget())

        self.bind(width=self._relayout)
        self._relayout()

    def _sync(self, *_args):
        self._rect.pos = self._bubble.pos
        self._rect.size = self._bubble.size

    def _relayout(self, *_args):
        """Size the bubble to its text: unwrap, measure the natural width,
        and only wrap when it exceeds the max fraction of the row."""
        max_w = max(self.width * self.MAX_FRAC - dp(2 * self.PAD_X), dp(60))
        if self._label.text_size[0] is not None:
            self._label.text_size = (None, None)
        self._label.texture_update()
        if self._label.texture_size[0] > max_w:
            self._label.text_size = (max_w, None)
            self._label.texture_update()
        tw, th = self._label.texture_size
        self._label.size = (tw, th)
        self._bubble.size = (tw + dp(2 * self.PAD_X), th + dp(2 * self.PAD_Y))
        self.height = self._bubble.height + dp(2)


class DateChip(BoxLayout):
    """Centered rounded chip marking a day boundary in the chat history -
    'Today' / 'Yesterday' / '28 Jun 2026'."""

    def __init__(self, text, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None,
                          height=dp(32), padding=(0, dp(5)), **kwargs)
        self._label = Label(text=text, font_size=dp(11), bold=True,
                             color=(0.62, 0.62, 0.68, 1), size_hint=(None, None))
        self._label.bind(texture_size=lambda inst, val: setattr(
            inst, "size", (val[0] + dp(20), dp(22))))
        with self._label.canvas.before:
            Color(*COLOR_CHIP)
            self._rect = RoundedRectangle(pos=self._label.pos,
                                           size=self._label.size, radius=[dp(11)])
        self._label.bind(pos=self._sync, size=self._sync)
        self.add_widget(Widget())
        self.add_widget(self._label)
        self.add_widget(Widget())

    def _sync(self, *_args):
        self._rect.pos = self._label.pos
        self._rect.size = self._label.size


class ChatEvent(BoxLayout):
    """A muted, centered line for call/file history entries interleaved
    with messages - e.g. 'Missed call', 'Sent report.pdf (2.4 MB)'. Pass
    on_retry for a failed transfer to show a Retry button under it."""

    def __init__(self, text, on_retry=None, **kwargs):
        super().__init__(orientation="vertical", size_hint_y=None, spacing=dp(4), **kwargs)
        label = Label(text=escape_markup(text), font_size=dp(12),
                      color=(0.55, 0.55, 0.6, 1), size_hint_y=None, halign="center")
        label.bind(texture_size=lambda inst, val: setattr(label, "height", val[1] + 6))
        label.bind(width=lambda inst, val: setattr(label, "text_size", (val, None)))
        self.add_widget(label)
        if on_retry:
            retry_btn = RoundButton(bg=(0.83, 0.69, 0.42, 1), text="Retry",
                                     size_hint=(None, None), size=(dp(72), dp(28)),
                                     pos_hint={"center_x": 0.5}, radius=[dp(14)],
                                     color=(0.05, 0.06, 0.1, 1), font_size=dp(11), bold=True)
            retry_btn.bind(on_release=lambda *_: on_retry())
            self.add_widget(retry_btn)
        self.bind(minimum_height=self.setter("height"))


class ChatScreen(Screen):
    peer_name = StringProperty("")
    peer_status = StringProperty("")
    peer_online = BooleanProperty(False)
    peer = None
    _last_day = None

    def set_peer(self, peer):
        self.peer = peer
        self.peer_name = peer["name"]
        self.peer_online = bool(peer.get("online"))
        self.peer_status = "Online" if self.peer_online else f"Last seen {format_last_seen(peer.get('last_seen'))}"
        self._last_day = None
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
                self.append_message(item["text"], mine=mine, timestamp=item["timestamp"])
            elif kind == "call":
                self.append_event(self._call_log_text(item), timestamp=item["timestamp"])
            elif kind == "file":
                self.append_event(self._file_log_text(item), timestamp=item["timestamp"])

    def on_back(self):
        self.manager.transition = SlideTransition(direction="right")
        self.manager.current = "users"

    def on_call(self):
        if not self.peer_online:
            self.append_event("Can't call - this device is offline")
            return
        App.get_running_app().start_call(self.peer["id"], self.peer["ip"], self.peer["name"])

    def show_fingerprint(self):
        app = App.get_running_app()
        peer_pubkey_b64 = self.peer.get("pubkey")
        their_fp = (crypto_util.fingerprint(base64.b64decode(peer_pubkey_b64))
                    if peer_pubkey_b64 else "unavailable")
        my_fp = crypto_util.fingerprint(app.identity.public_bytes)
        content = Label(
            text=(f"All messages, calls, and files with {self.peer_name}\n"
                  f"are end-to-end encrypted.\n\n"
                  f"To confirm you're really talking to {self.peer_name}\n"
                  f"and not an impostor on the network, read these\n"
                  f"codes aloud to each other and check they match:\n\n"
                  f"Your code:\n{my_fp}\n\n"
                  f"{self.peer_name}'s code:\n{their_fp}"),
            halign="center",
        )
        content.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        Popup(title="Verify identity", content=content,
              size_hint=(0.9, 0.6)).open()

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
        self.append_message(text, mine=True)
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

        if path.startswith("content://"):
            try:
                local_dir = os.path.join(app.user_data_dir, "outgoing_tmp")
                path = resolve_android_content_uri(path, local_dir)
            except Exception:
                self.append_event("Couldn't read that file - try picking it again")
                return

        self.append_event(f"Sending {os.path.basename(path)}…")

        def do_send():
            app.file_manager.send_file(peer["id"], peer["name"], peer["ip"], path)

        threading.Thread(target=do_send, daemon=True).start()

    def _on_send_failed(self, peer_name):
        if self.peer_name == peer_name:
            self.append_event("Couldn't deliver - device unreachable")

    def _maybe_date_chip(self, ts):
        day = time.localtime(ts)[:3]
        if day != self._last_day:
            self._last_day = day
            self.ids.message_list.add_widget(DateChip(format_date_chip(ts)))

    def append_message(self, text, mine, timestamp=None):
        ts = timestamp or time.time()
        self._maybe_date_chip(ts)
        self.ids.message_list.add_widget(ChatBubble(text, mine, timestamp=ts))
        Clock.schedule_once(lambda dt: setattr(self.ids.chat_scroll, "scroll_y", 0), 0.05)

    def append_event(self, text, on_retry=None, timestamp=None):
        ts = timestamp or time.time()
        self._maybe_date_chip(ts)
        self.ids.message_list.add_widget(ChatEvent(text, on_retry=on_retry))
        Clock.schedule_once(lambda dt: setattr(self.ids.chat_scroll, "scroll_y", 0), 0.05)

    def receive_message(self, msg):
        if self.peer and msg.get("from_id") == self.peer.get("id"):
            self.append_message(msg.get("text", ""), mine=False)

    def receive_file_event(self, event):
        if not self.peer or self.peer.get("id") != event.get("peer_id"):
            return
        direction = "in" if event["event"] == "file_received" else "out"
        row = {"direction": direction, "filename": event["filename"],
               "size": event.get("size", 0), "status": event.get("status", "failed")}
        on_retry = None
        if event["event"] == "file_send_failed" and event.get("path"):
            on_retry = lambda: self._start_file_send(event["path"])
        self.append_event(self._file_log_text(row), on_retry=on_retry)

    # No emoji in these lines - Kivy's bundled Roboto has no emoji glyphs,
    # so anything like a paperclip or phone renders as a hollow box.
    @staticmethod
    def _call_log_text(c):
        if c["outcome"] == "missed":
            return "Missed call"
        if c["outcome"] == "rejected":
            return "Call declined"
        if c["outcome"] in ("failed", "cancelled"):
            return "Call not connected"
        mins, secs = divmod(int(c["duration"] or 0), 60)
        which = "Outgoing" if c["direction"] == "out" else "Incoming"
        return f"{which} call – {mins:02d}:{secs:02d}"

    @staticmethod
    def _file_log_text(f):
        which = "Sent" if f["direction"] == "out" else "Received"
        size_str = format_size(f["size"])
        if f["status"] != "completed":
            return f"{which} {f['filename']} ({size_str}) – failed"
        return f"{which} {f['filename']} ({size_str})"


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
        global IDENTITY
        self.discovery = None
        self.message_server = None
        self.call_manager = None
        self.file_manager = None
        self._is_foreground = True
        self.profile_store = JsonStore(self.user_data_dir + "/lancom.json")

        IDENTITY = crypto_util.Identity.load_or_create(
            os.path.join(self.user_data_dir, "identity.key"))
        self.identity = IDENTITY
        self.store = Store(os.path.join(self.user_data_dir, "lancom.db"), IDENTITY.storage_key())

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
                    Permission.POST_NOTIFICATIONS,
                ])
            except Exception:
                pass
            # Not a runtime permission - a separate system settings prompt,
            # so it's requested after the batch above rather than mixed in.
            Clock.schedule_once(lambda dt: android_notify.request_ignore_battery_optimizations(), 1.5)

        if self.profile_store.exists("profile"):
            name = self.profile_store.get("profile").get("name", "")
            if name:
                self.set_display_name(name)
                self.root.current = "users"
                return
        self.root.current = "setup"

    def on_pause(self):
        # Returning True tells Android this app may keep running in the
        # background instead of being torn down, so discovery/messaging/
        # calls keep working while LANCOM isn't the foreground app. Some
        # OEM battery managers ignore this and kill it anyway - the
        # ignore-battery-optimizations prompt in on_start is the mitigation
        # for that; a full foreground service would be the next step up if
        # that's still not enough on-device.
        self._is_foreground = False
        return True

    def on_resume(self):
        self._is_foreground = True

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
        viewing_this_chat = (self.root.current == "chat" and chat.peer
                              and chat.peer.get("id") == msg.get("from_id"))
        chat.receive_message(msg)
        if not viewing_this_chat or not self._is_foreground:
            android_notify.notify_message(msg.get("from_name", "Unknown"), msg.get("text", ""))

    def _on_call_event(self, event):
        Clock.schedule_once(lambda dt: self._dispatch_call_event(event), 0)

    def _dispatch_call_event(self, event):
        call_screen = self.root.get_screen("call")
        kind = event["event"]
        if kind == "incoming_call":
            call_screen.on_incoming(event["peer_name"])
            self.root.transition = SlideTransition(direction="up")
            self.root.current = "call"
            if not self._is_foreground:
                android_notify.notify_incoming_call(event["peer_name"])
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
