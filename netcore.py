"""
LANCOM networking core - discovery, messaging, calls, file transfer.

Deliberately Kivy-free: on Android this whole module runs inside the
foreground service process (service.py) so communication keeps working
when the app UI is closed, the phone is locked, or the task is swiped
away. The UI process talks to it over the localhost control socket
(lancom_ipc.py). On desktop the UI embeds it directly (DirectBackend).

Ports:
  55555/UDP - peer discovery (broadcast + unicast probes)
  55556/TCP - text messaging + delivery/read receipts
  55557/TCP - call signaling (invite/accept/reject/hangup)
  55558/UDP - call audio (raw PCM16 frames, best-effort)
  55559/TCP - file transfer
  55560/TCP - localhost-only UI<->service control channel
"""

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import uuid

import crypto_util
from store import Store

IS_ANDROID = "ANDROID_ARGUMENT" in os.environ

BROADCAST_PORT = 55555
MESSAGE_PORT = 55556
CALL_SIGNAL_PORT = 55557
AUDIO_PORT = 55558
FILE_PORT = 55559
CONTROL_PORT = 55560

BROADCAST_INTERVAL = 2.0
PEER_TIMEOUT = 7.0
FILE_CHUNK = 65536
FILE_TAG_LEN = 16  # ChaCha20-Poly1305 auth tag length appended to each chunk

MAX_MESSAGE_BYTES = 64 * 1024
PEER_ID_LEN = 16  # crypto_util.peer_id_for_pubkey() hex length

# Files with these extensions get an inline picture preview in the chat
# (and keep a private copy for it); everything else is handed straight
# to the public Downloads folder.
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# Set by NetworkCore before any networking starts. Every device's id is
# derived from this identity's public key (crypto_util.peer_id_for_pubkey)
# - it can't be spoofed without also holding the private key.
IDENTITY = None


def app_data_dir():
    """The shared per-app data directory. On Android this must be the
    SAME path in the UI and service processes (both get ANDROID_PRIVATE
    = the app's files dir, which is also what Kivy's user_data_dir
    resolves to), so identity/DB/received files are shared."""
    if IS_ANDROID:
        return os.environ.get("ANDROID_PRIVATE", ".")
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, "lancom")


def android_context():
    """Context for jnius calls that works in BOTH processes: the activity
    when running in the app, the service otherwise."""
    from jnius import autoclass
    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    if activity is not None:
        return activity
    return autoclass("org.kivy.android.PythonService").mService


def _get_local_ip_via_wifi_manager():
    """Ask Android's WifiManager for the WiFi interface's IP directly.
    Avoids relying on the OS routing table, which can pick the wrong
    interface (or fail outright) on a WiFi network with no internet
    gateway - exactly the case this app is built for."""
    from jnius import autoclass
    Context = autoclass("android.content.Context")
    ctx = android_context()
    wifi_manager = ctx.getSystemService(Context.WIFI_SERVICE)
    ip_int = wifi_manager.getConnectionInfo().getIpAddress()
    if not ip_int:
        return None
    return socket.inet_ntoa(struct.pack("<I", ip_int))


def get_local_ip():
    """Best-effort LAN IP without needing internet access."""
    if IS_ANDROID:
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


def export_to_downloads(src_path, filename):
    """Copy a received file into the phone's public Downloads/LANCOM so
    the user can actually reach it (Files app, Gallery) - the app's
    private dir where transfers land is invisible to them. Returns the
    human-readable destination, or "" if unavailable/failed.

    API 29+ goes through MediaStore (no permission needed for files the
    app creates); older Androids write directly to the public directory
    using the WRITE_EXTERNAL_STORAGE permission the app already holds."""
    if not IS_ANDROID:
        return ""
    try:
        from jnius import autoclass
        Build_VERSION = autoclass("android.os.Build$VERSION")
        ctx = android_context()

        if Build_VERSION.SDK_INT >= 29:
            ContentValues = autoclass("android.content.ContentValues")
            Downloads = autoclass("android.provider.MediaStore$Downloads")
            String = autoclass("java.lang.String")
            resolver = ctx.getContentResolver()
            values = ContentValues()
            values.put(String("_display_name"), String(filename))
            values.put(String("relative_path"), String("Download/LANCOM"))
            uri = resolver.insert(Downloads.EXTERNAL_CONTENT_URI, values)
            if uri is None:
                return ""
            out = resolver.openOutputStream(uri)
            try:
                with open(src_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
            finally:
                out.close()
        else:
            Environment = autoclass("android.os.Environment")
            base = Environment.getExternalStoragePublicDirectory(
                Environment.DIRECTORY_DOWNLOADS).getAbsolutePath()
            dest_dir = os.path.join(base, "LANCOM")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, filename)
            with open(src_path, "rb") as fin, open(dest, "wb") as fout:
                while True:
                    chunk = fin.read(65536)
                    if not chunk:
                        break
                    fout.write(chunk)
        return "Download/LANCOM"
    except Exception:
        return ""


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


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


if IS_ANDROID:
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
    def __init__(self, display_name, store, on_peer_online=None):
        self.display_name = display_name
        self.store = store
        # Called (from the listener thread) when a peer we didn't have
        # live appears - the core uses it to flush queued messages.
        self.on_peer_online = on_peer_online
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
                came_online = peer_id not in self.peers
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
            if came_online and self.on_peer_online:
                try:
                    self.on_peer_online(peer_id, addr[0])
                except Exception:
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
    """Messaging with store-and-forward. Outgoing messages always go
    through the outbox: queue_message() records them as "pending", and
    flush_outbox() tries to deliver everything queued for a peer, oldest
    first. Flushes run on send, and whenever discovery sees the peer come
    online - so a message typed while the other device was away arrives
    by itself once both are back on the same network.

    Delivery/read receipts ride the same port as messages (type RECEIPT):
    the receiver confirms "delivered" as soon as it stores a message and
    "seen" when the chat is actually on screen. Receipts are idempotent
    and deduplicated by msg_id, so retries never duplicate a message."""

    def __init__(self, display_name, store, on_message, on_receipt=None,
                 on_typing=None, port=None):
        self.display_name = display_name
        self.store = store
        self.on_message = on_message
        self.on_receipt = on_receipt  # callback(msg_id, status) - worker thread
        self.on_typing = on_typing  # callback(peer_id, bool) - worker thread
        self.port = port or MESSAGE_PORT
        self._running = False
        self._flush_locks = {}  # peer_id -> Lock: one flush per peer at a time

    def start(self):
        self._running = True
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
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
            # _sender_id is authenticated by successful decryption - trust
            # it over any from_id the JSON body itself might claim.
            sender_id = msg["_sender_id"]
            msg_type = msg.get("type")

            if msg_type == "RECEIPT":
                kind = msg.get("kind")
                if kind not in ("delivered", "seen"):
                    return
                for mid in msg.get("msg_ids", []):
                    if self.store.set_message_status(mid, kind) and self.on_receipt:
                        self.on_receipt(mid, kind)
                return

            if msg_type == "TYPING":
                # Ephemeral - never stored, never notified, just relayed
                # to the UI if it's watching this chat.
                if self.on_typing:
                    self.on_typing(sender_id, bool(msg.get("typing")))
                return

            if msg_type != "MSG":
                return
            msg_id = msg.get("msg_id")
            duplicate = self.store.has_message(sender_id, msg_id)
            if not duplicate:
                self.store.add_message(sender_id, msg.get("from_name", "Unknown"),
                                        "in", msg.get("text", ""), msg_id=msg_id)
            # Confirm delivery even for duplicates - a resend means the
            # sender never got (or lost) the first receipt.
            if msg_id:
                self.send_receipt(sender_id, addr[0], [msg_id], "delivered",
                                  background=True)
            if duplicate:
                return
            msg["ip"] = addr[0]
            msg["from_id"] = sender_id
            self.on_message(msg)

    def send_receipt(self, peer_id, peer_ip, msg_ids, kind, background=False):
        """Best-effort: a lost 'delivered' recovers on resend, a lost
        'seen' just leaves the sender at double-gray ticks."""
        msg_ids = [m for m in msg_ids if m]
        if not msg_ids or not peer_ip:
            return

        def do():
            pubkey_b64 = self.store.get_peer_pubkey(peer_id)
            if not pubkey_b64:
                return
            try:
                send_encrypted_tcp(peer_ip, MESSAGE_PORT,
                                   {"type": "RECEIPT", "kind": kind,
                                    "msg_ids": msg_ids},
                                   base64.b64decode(pubkey_b64))
            except OSError:
                pass

        if background:
            threading.Thread(target=do, daemon=True).start()
        else:
            do()

    def send_typing(self, peer_id, peer_ip, typing):
        """Fire-and-forget typing state: best-effort, short timeout, no
        retries - a lost one just means no indicator for a moment."""
        def do():
            pubkey_b64 = self.store.get_peer_pubkey(peer_id)
            if not pubkey_b64 or not peer_ip:
                return
            try:
                send_encrypted_tcp(peer_ip, MESSAGE_PORT,
                                   {"type": "TYPING", "typing": bool(typing)},
                                   base64.b64decode(pubkey_b64), timeout=1.5)
            except OSError:
                pass

        threading.Thread(target=do, daemon=True).start()

    def queue_message(self, peer_id, peer_name, text):
        """Store the message as pending and enqueue it. Returns the
        msg_id. Actual delivery happens via flush_outbox()."""
        msg_id = uuid.uuid4().hex[:16]
        self.store.add_message(peer_id, peer_name, "out", text,
                                msg_id=msg_id, status="pending")
        self.store.outbox_add(msg_id, peer_id)
        return msg_id

    def flush_outbox(self, peer_id, peer_ip=None):
        """Deliver everything queued for one peer, oldest first. Blocking -
        call from a background thread. Stops at the first failure so
        ordering is preserved; the next flush picks up from there."""
        lock = self._flush_locks.setdefault(peer_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return  # already flushing this peer
        try:
            pubkey_b64 = self.store.get_peer_pubkey(peer_id)
            if not pubkey_b64:
                return
            if peer_ip is None:
                for p in self.store.get_known_peers():
                    if p["id"] == peer_id:
                        peer_ip = p["ip"]
                        break
            if not peer_ip:
                return
            peer_pubkey = base64.b64decode(pubkey_b64)
            for item in self.store.outbox_pending(peer_id):
                payload = {
                    "type": "MSG",
                    "msg_id": item["msg_id"],
                    "from_name": self.display_name,
                    "text": item["text"],
                    "timestamp": item["timestamp"],
                }
                try:
                    send_encrypted_tcp(peer_ip, MESSAGE_PORT, payload, peer_pubkey)
                except OSError:
                    break
                self.store.outbox_remove(item["msg_id"])
        finally:
            lock.release()


# ---------------------------------------------------------------------------
# Call manager
# ---------------------------------------------------------------------------

class CallManager:
    """Handles call signaling (TCP) and audio streaming (UDP).

    Ring timeouts, WhatsApp-style: an unanswered incoming call becomes
    "missed" after RING_TIMEOUT (event call_missed); an unanswered
    outgoing call gives up shortly after (event call_no_answer) and
    tells the peer to stop ringing."""

    RING_TIMEOUT = 45.0

    def __init__(self, display_name, store, on_event):
        self.display_name = display_name
        self.store = store
        self.on_event = on_event  # callback(event_dict) - worker threads
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
                threading.Thread(target=self._incoming_ring_watch,
                                  args=(sender_id,), daemon=True).start()
                return

            # ACCEPT/REJECT/HANGUP only apply to messages from the peer we're
            # actually talking to. sender_id is cryptographically
            # authenticated (forging it would derive the wrong decryption
            # key), unlike the IP-based check this used to rely on, which a
            # device on the same LAN could spoof.
            reject_reason = ""
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
                    reject_reason = msg.get("reason", "")
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
                self.on_event({"event": "call_rejected", "peer_name": peer_name,
                                "reason": reject_reason})
            elif action == "ended":
                self._stop_audio()
                self.on_event({"event": "call_ended", "peer_name": peer_name})

    # -- outgoing actions ---------------------------------------------------
    # Signaling sends run on a background thread so callers never block
    # on a slow/dead peer (send_encrypted_tcp has up to a 4s timeout).

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
                return
            # The invite reached the device, so it's now ringing there -
            # distinct from "unreachable" so the caller can tell whether
            # the other side is getting the call at all.
            with self._lock:
                ringing = self.state == "calling" and self.peer_ip == peer_ip
            if ringing:
                self.on_event({"event": "call_ringing", "peer_name": peer_name})
                self._outgoing_ring_watch(peer_id, peer_ip, peer_name,
                                          base64.b64decode(pubkey_b64))

        threading.Thread(target=send_invite, daemon=True).start()
        return True

    def _incoming_ring_watch(self, peer_id):
        """Unanswered incoming call -> missed after RING_TIMEOUT."""
        deadline = time.time() + self.RING_TIMEOUT
        while time.time() < deadline:
            time.sleep(0.5)
            with self._lock:
                if not (self.state == "ringing" and self.peer_id == peer_id):
                    return
        with self._lock:
            if not (self.state == "ringing" and self.peer_id == peer_id):
                return
            peer_name = self.peer_name
            self._log_call(peer_id, peer_name, "in", "missed")
            self._reset_to_idle()
        self.on_event({"event": "call_missed", "peer_name": peer_name})

    def _outgoing_ring_watch(self, peer_id, peer_ip, peer_name, peer_pubkey):
        """Unanswered outgoing call -> give up a little after the callee's
        own missed-call timeout, and tell it to stop ringing. Runs on the
        send_invite thread (which has nothing left to do)."""
        deadline = time.time() + self.RING_TIMEOUT + 5.0
        while time.time() < deadline:
            time.sleep(0.5)
            with self._lock:
                if not (self.state == "calling" and self.peer_id == peer_id):
                    return
        with self._lock:
            if not (self.state == "calling" and self.peer_id == peer_id):
                return
            self._log_call(peer_id, peer_name, "out", "cancelled")
            self._reset_to_idle()
        try:
            send_encrypted_tcp(peer_ip, CALL_SIGNAL_PORT, {"type": "HANGUP"},
                                peer_pubkey)
        except OSError:
            pass
        self.on_event({"event": "call_no_answer", "peer_name": peer_name})

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
        self.on_event = on_event  # callback(event_dict) - worker threads
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
        final_path = ""
        saved_to = ""
        if status == "completed":
            final_path = dest_path[:-len(".partial")] if dest_path.endswith(".partial") else dest_path
            try:
                os.replace(dest_path, final_path)
            except OSError:
                final_path = dest_path
            # Hand the file to the user: private app storage is invisible
            # to them. Images keep the private copy too (the chat renders
            # an inline preview from it); other files - potentially
            # GB-scale - are moved rather than duplicated.
            saved_to = export_to_downloads(final_path, filename)
            if saved_to and not filename.lower().endswith(IMAGE_EXTS):
                try:
                    os.remove(final_path)
                except OSError:
                    pass
                final_path = ""
            self.store.finish_transfer(transfer_id, "completed")
            self.store.add_file_record(peer_id, peer_name, "in", filename, size, final_path, "completed")
        self.on_event({"event": "file_received", "peer_id": peer_id, "peer_name": peer_name,
                        "filename": filename, "size": size, "status": status,
                        "path": final_path, "saved_to": saved_to})

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
                            "filename": filename, "size": size, "status": "failed",
                            "path": file_path})
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
# Orchestration
# ---------------------------------------------------------------------------

class NetworkCore:
    """Owns the store + all four network subsystems and funnels their
    callbacks into one JSON-safe event stream:

      {"kind": "message",     "msg":   {...}}
      {"kind": "receipt",     "msg_id": ..., "status": ...}
      {"kind": "call",        "event": {...}}
      {"kind": "file",        "event": {...}}
      {"kind": "peer_online", "peer_id": ..., "ip": ...}
    """

    def __init__(self, data_dir, on_event=None):
        global IDENTITY
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        IDENTITY = crypto_util.Identity.load_or_create(
            os.path.join(data_dir, "identity.key"))
        self.identity = IDENTITY
        self.store = Store(os.path.join(data_dir, "lancom.db"),
                           IDENTITY.storage_key())
        self.on_event = on_event or (lambda ev: None)
        self.display_name = None
        self.discovery = None
        self.message_server = None
        self.call_manager = None
        self.file_manager = None
        self.started = False

    def _emit(self, ev):
        try:
            self.on_event(ev)
        except Exception:
            pass

    def start(self, display_name):
        if self.started:
            return
        self.started = True
        self.display_name = display_name

        self.discovery = PeerDiscovery(display_name, self.store,
                                        on_peer_online=self._peer_online)
        self.message_server = MessageServer(display_name, self.store,
                                             self._message,
                                             on_receipt=self._receipt,
                                             on_typing=self._typing)
        self.call_manager = CallManager(display_name, self.store, self._call_event)
        files_dir = os.path.join(self.data_dir, "received_files")
        self.file_manager = FileTransferManager(display_name, self.store,
                                                 files_dir, self._file_event)

        self.discovery.start()
        self.message_server.start()
        self.call_manager.start()
        self.file_manager.start()

    def stop(self):
        for part in (self.discovery, self.message_server,
                     self.call_manager, self.file_manager):
            if part:
                part.stop()
        self.started = False

    # -- internal event fan-in -----------------------------------------

    def _peer_online(self, peer_id, ip):
        threading.Thread(target=self.message_server.flush_outbox,
                          args=(peer_id, ip), daemon=True).start()
        self._emit({"kind": "peer_online", "peer_id": peer_id, "ip": ip})

    def _message(self, msg):
        self._emit({"kind": "message",
                    "msg": {k: v for k, v in msg.items()
                            if not k.startswith("_")}})

    def _receipt(self, msg_id, status):
        self._emit({"kind": "receipt", "msg_id": msg_id, "status": status})

    def _typing(self, peer_id, typing):
        self._emit({"kind": "typing", "peer_id": peer_id, "typing": typing})

    def _call_event(self, event):
        self._emit({"kind": "call", "event": event})

    def _file_event(self, event):
        self._emit({"kind": "file", "event": event})

    # -- public API (mirrored 1:1 by the IPC command set) ---------------

    def get_state(self):
        if not self.started or not self.discovery:
            return {"started": False, "peers": []}
        return {"started": True,
                "local_ip": self.discovery.local_ip,
                "broadcast_ip": self.discovery.broadcast_ip,
                "peers": self.discovery.get_known_peers()}

    def send_message(self, peer_id, peer_name, peer_ip, text):
        msg_id = self.message_server.queue_message(peer_id, peer_name, text)
        threading.Thread(target=self.message_server.flush_outbox,
                          args=(peer_id, peer_ip), daemon=True).start()
        return msg_id

    def chat_opened(self, peer_id, peer_ip):
        self.mark_seen(peer_id, peer_ip,
                       self.store.get_unseen_incoming_ids(peer_id))
        threading.Thread(target=self.message_server.flush_outbox,
                          args=(peer_id, peer_ip), daemon=True).start()

    def mark_seen(self, peer_id, peer_ip, msg_ids):
        msg_ids = [m for m in msg_ids if m]
        if not msg_ids:
            return
        self.store.mark_seen_receipts_sent(peer_id, msg_ids)
        self.message_server.send_receipt(peer_id, peer_ip, msg_ids, "seen",
                                         background=True)

    def send_typing(self, peer_id, peer_ip, typing):
        if self.message_server:
            self.message_server.send_typing(peer_id, peer_ip, typing)

    def delete_message(self, peer_id, row_id=None, msg_id=None):
        self.store.delete_message(peer_id, row_id=row_id, msg_id=msg_id)

    def clear_chat(self, peer_id):
        self.store.clear_chat(peer_id)

    def probe_ip(self, ip):
        return self.discovery.probe_ip(ip) if self.discovery else False

    def call(self, peer_id, peer_ip, peer_name):
        return self.call_manager.call(peer_id, peer_ip, peer_name)

    def accept_call(self):
        self.call_manager.accept()

    def reject_call(self):
        self.call_manager.reject()

    def hang_up(self):
        self.call_manager.hang_up()

    def call_state(self):
        cm = self.call_manager
        if not cm:
            return {"state": "idle", "peer_name": None}
        return {"state": cm.state, "peer_name": cm.peer_name}

    def send_file(self, peer_id, peer_name, peer_ip, path):
        threading.Thread(target=self.file_manager.send_file,
                          args=(peer_id, peer_name, peer_ip, path),
                          daemon=True).start()


class EventPolicy:
    """Single place for the notify-or-mark-seen decision, shared by the
    Android service and the desktop in-process backend. The UI keeps it
    updated about visibility (foreground + which chat is open); events
    for anything the user isn't looking at become notifications, and
    messages they ARE looking at get auto 'seen' receipts.

    Calls RING like a phone: notify_call(peer_name, is_foreground) must
    start a looping ringtone (plus a full-screen call notification when
    backgrounded); stop_call_alert() silences it. Both are no-ops off
    Android."""

    CALL_END_EVENTS = ("call_active", "call_ended", "call_rejected",
                       "call_failed", "call_missed")

    def __init__(self, core, notify_message, notify_call,
                 stop_call_alert=None):
        self.core = core
        self.notify_message = notify_message
        self.notify_call = notify_call
        self.stop_call_alert = stop_call_alert or (lambda: None)
        self.foreground = False
        self.viewing = None  # peer_id whose chat is on screen

    def set_state(self, foreground, viewing):
        self.foreground = bool(foreground)
        self.viewing = viewing or None

    def handle(self, ev):
        kind = ev.get("kind")
        if kind == "message":
            m = ev.get("msg", {})
            if self.foreground and self.viewing == m.get("from_id"):
                if m.get("msg_id"):
                    self.core.mark_seen(m["from_id"], m.get("ip"),
                                        [m["msg_id"]])
            else:
                self.notify_message(m.get("from_name", "Unknown"),
                                    m.get("text", ""))
        elif kind == "call":
            e = ev.get("event", {})
            ce = e.get("event")
            if ce == "incoming_call":
                self.notify_call(e.get("peer_name", "Unknown"),
                                 self.foreground)
            elif ce in self.CALL_END_EVENTS:
                self.stop_call_alert()
                if ce == "call_missed":
                    self.notify_message(e.get("peer_name", "Unknown"),
                                        "Missed call")
        elif kind == "file":
            e = ev.get("event", {})
            if (not self.foreground and e.get("event") == "file_received"
                    and e.get("status") == "completed"):
                self.notify_message(e.get("peer_name", "Unknown"),
                                    f"Sent you {e.get('filename', 'a file')}")
