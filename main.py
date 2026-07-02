"""
LANCOM - LAN-only voice calling and text messaging over WiFi.

No internet, no server. Devices discover each other via UDP broadcast and
talk directly, peer-to-peer, over the local WiFi network.

Ports:
  55555/UDP - peer discovery (broadcast)
  55556/TCP - text messaging
  55557/TCP - call signaling (invite/accept/reject/hangup)
  55558/UDP - call audio (raw PCM16 frames, best-effort)
"""

import json
import socket
import struct
import threading
import time
import uuid

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.properties import StringProperty, BooleanProperty
from kivy.storage.jsonstore import JsonStore
from kivy.uix.screenmanager import Screen, SlideTransition
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.utils import platform, escape_markup

BROADCAST_PORT = 55555
MESSAGE_PORT = 55556
CALL_SIGNAL_PORT = 55557
AUDIO_PORT = 55558

BROADCAST_INTERVAL = 2.0
PEER_TIMEOUT = 7.0

DEVICE_ID = uuid.uuid4().hex[:12]


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
                    n = self._record.read(buf, 0, chunk)
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
    def __init__(self, display_name):
        self.display_name = display_name
        self.local_ip = get_local_ip()
        self.broadcast_ip = get_broadcast_ip(self.local_ip)
        self.peers = {}  # device_id -> {"name", "ip", "last_seen"}
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
        while self._running:
            try:
                data = json.dumps(payload).encode("utf-8")
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
            with self._lock:
                self.peers[msg["id"]] = {
                    "id": msg["id"],
                    "name": msg.get("name", "Unknown"),
                    "ip": addr[0],
                    "last_seen": time.time(),
                }
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

    def get_peers(self):
        with self._lock:
            return sorted(self.peers.values(), key=lambda p: p["name"].lower())


# ---------------------------------------------------------------------------
# Text messaging
# ---------------------------------------------------------------------------

class MessageServer:
    def __init__(self, display_name, on_message):
        self.display_name = display_name
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
            self.on_message(msg)

    def send_message(self, peer_ip, text):
        payload = {
            "type": "MSG",
            "from_id": DEVICE_ID,
            "from_name": self.display_name,
            "text": text,
            "timestamp": time.time(),
        }
        send_json_tcp(peer_ip, MESSAGE_PORT, payload)
        return payload


# ---------------------------------------------------------------------------
# Call manager
# ---------------------------------------------------------------------------

class CallManager:
    """Handles call signaling (TCP) and audio streaming (UDP)."""

    def __init__(self, display_name, on_event):
        self.display_name = display_name
        self.on_event = on_event  # callback(event_dict) - marshalled to main thread by caller
        self._running = False
        self._lock = threading.Lock()
        self.peer_ip = None
        self.peer_name = None
        self.state = "idle"  # idle | calling | ringing | active
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
        self.peer_ip = None
        self.peer_name = None

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
                        self.peer_ip = addr[0]
                        self.peer_name = msg.get("from_name", "Unknown")
                        self.state = "ringing"
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
                peer_name = self.peer_name
                if msg_type == "ACCEPT" and from_current_peer and self.state == "calling":
                    self.state = "active"
                    action = "activate"
                elif msg_type == "REJECT" and from_current_peer and self.state == "calling":
                    self._reset_to_idle()
                    action = "rejected"
                elif msg_type == "HANGUP" and from_current_peer and self.state in ("active", "ringing", "calling"):
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

    def call(self, peer_ip, peer_name):
        with self._lock:
            if self.state != "idle":
                return False
            self.peer_ip = peer_ip
            self.peer_name = peer_name
            self.state = "calling"

        def send_invite():
            try:
                send_json_tcp(peer_ip, CALL_SIGNAL_PORT,
                              {"type": "INVITE", "from_id": DEVICE_ID, "from_name": self.display_name})
            except OSError:
                with self._lock:
                    if self.peer_ip == peer_ip:
                        self._reset_to_idle()
                self.on_event({"event": "call_failed", "peer_name": peer_name})

        threading.Thread(target=send_invite, daemon=True).start()
        return True

    def accept(self):
        with self._lock:
            if self.state != "ringing":
                return
            peer_ip = self.peer_ip
            peer_name = self.peer_name

        def send_accept():
            try:
                send_json_tcp(peer_ip, CALL_SIGNAL_PORT, {"type": "ACCEPT", "from_id": DEVICE_ID})
            except OSError:
                with self._lock:
                    if self.peer_ip == peer_ip:
                        self._reset_to_idle()
                self.on_event({"event": "call_failed", "peer_name": peer_name})
                return
            with self._lock:
                if self.peer_ip == peer_ip:
                    self.state = "active"
            self._start_audio()
            self.on_event({"event": "call_active", "peer_name": peer_name})

        threading.Thread(target=send_accept, daemon=True).start()

    def reject(self):
        with self._lock:
            if self.state != "ringing":
                return
            peer_ip = self.peer_ip
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
            peer_ip = self.peer_ip
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
# UI
# ---------------------------------------------------------------------------

KV = """
#:import dp kivy.metrics.dp

ScreenManager:
    SetupScreen:
    UsersScreen:
    ChatScreen:
    CallScreen:

<SetupScreen>:
    name: "setup"
    BoxLayout:
        orientation: "vertical"
        padding: dp(24)
        spacing: dp(16)
        canvas.before:
            Color:
                rgba: 0.043, 0.059, 0.102, 1
            Rectangle:
                pos: self.pos
                size: self.size

        Widget:
            size_hint_y: 0.3

        Label:
            text: "LANCOM"
            font_size: dp(36)
            bold: True
            size_hint_y: None
            height: dp(48)

        Label:
            text: "LAN voice + messaging, no internet needed"
            color: 0.6, 0.6, 0.65, 1
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

        Label:
            id: setup_error
            text: ""
            color: 0.9, 0.3, 0.3, 1
            size_hint_y: None
            height: dp(20)

        Button:
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
                rgba: 0.043, 0.059, 0.102, 1
            Rectangle:
                pos: self.pos
                size: self.size

        BoxLayout:
            size_hint_y: None
            height: dp(56)
            padding: dp(12), dp(8)
            Label:
                text: "Nearby devices"
                bold: True
                font_size: dp(18)
                halign: "left"
                text_size: self.size

        Label:
            id: debug_info
            text: ""
            font_size: dp(11)
            color: 0.45, 0.45, 0.5, 1
            size_hint_y: None
            height: dp(18)

        ScrollView:
            BoxLayout:
                id: peer_list
                orientation: "vertical"
                size_hint_y: None
                height: self.minimum_height
                padding: dp(8)
                spacing: dp(8)

<ChatScreen>:
    name: "chat"
    BoxLayout:
        orientation: "vertical"
        canvas.before:
            Color:
                rgba: 0.043, 0.059, 0.102, 1
            Rectangle:
                pos: self.pos
                size: self.size

        BoxLayout:
            size_hint_y: None
            height: dp(56)
            padding: dp(8)
            spacing: dp(8)
            Button:
                text: "< Back"
                size_hint_x: None
                width: dp(80)
                on_release: root.on_back()
            Label:
                text: root.peer_name
                bold: True
                font_size: dp(18)
            Button:
                text: "Call"
                size_hint_x: None
                width: dp(70)
                on_release: root.on_call()

        ScrollView:
            id: chat_scroll
            BoxLayout:
                id: message_list
                orientation: "vertical"
                size_hint_y: None
                height: self.minimum_height
                padding: dp(8)
                spacing: dp(6)

        BoxLayout:
            size_hint_y: None
            height: dp(56)
            padding: dp(8)
            spacing: dp(8)
            TextInput:
                id: chat_input
                multiline: False
                hint_text: "Message"
                on_text_validate: root.on_send(chat_input.text)
            Button:
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
                rgba: 0.043, 0.059, 0.102, 1
            Rectangle:
                pos: self.pos
                size: self.size

        Widget:
            size_hint_y: 0.3

        Label:
            text: root.peer_name
            font_size: dp(28)
            bold: True
            size_hint_y: None
            height: dp(40)

        Label:
            text: root.status_text
            color: 0.6, 0.6, 0.65, 1
            size_hint_y: None
            height: dp(28)

        Widget:

        BoxLayout:
            size_hint_y: None
            height: dp(64)
            spacing: dp(16)
            Button:
                text: "Accept"
                opacity: 1 if root.show_accept else 0
                disabled: not root.show_accept
                on_release: root.on_accept()
            Button:
                text: "Reject" if root.show_accept else "Hang Up"
                background_color: 0.8, 0.2, 0.2, 1
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


class PeerRow(BoxLayout):
    def __init__(self, peer, on_open_chat, on_call, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None, height=56,
                          spacing=8, **kwargs)
        self.peer = peer
        info = BoxLayout(orientation="vertical")
        info.add_widget(Label(text=peer["name"], bold=True, halign="left",
                               text_size=(None, None)))
        info.add_widget(Label(text=peer["ip"], color=(0.55, 0.55, 0.6, 1), font_size=12))
        self.add_widget(info)

        from kivy.uix.button import Button
        chat_btn = Button(text="Chat", size_hint_x=None, width=70)
        chat_btn.bind(on_release=lambda *_: on_open_chat(peer))
        self.add_widget(chat_btn)

        call_btn = Button(text="Call", size_hint_x=None, width=70)
        call_btn.bind(on_release=lambda *_: on_call(peer))
        self.add_widget(call_btn)


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
        peers = app.discovery.get_peers()
        container = self.ids.peer_list
        container.clear_widgets()
        if not peers:
            container.add_widget(Label(text="Searching for devices on this WiFi...",
                                        size_hint_y=None, height=40,
                                        color=(0.5, 0.5, 0.55, 1)))
        for peer in peers:
            container.add_widget(PeerRow(peer, self.open_chat, self.call_peer))

    def open_chat(self, peer):
        app = App.get_running_app()
        chat = self.manager.get_screen("chat")
        chat.set_peer(peer)
        self.manager.transition = SlideTransition(direction="left")
        self.manager.current = "chat"

    def call_peer(self, peer):
        App.get_running_app().start_call(peer["ip"], peer["name"])


class ChatBubble(BoxLayout):
    def __init__(self, text, sender, mine, **kwargs):
        super().__init__(orientation="vertical", size_hint_y=None, **kwargs)
        safe_sender = escape_markup(sender)
        safe_text = escape_markup(text)
        label = Label(text=f"[b]{safe_sender}[/b]\n{safe_text}", markup=True,
                       halign="left" if not mine else "right",
                       size_hint_y=None)
        label.bind(texture_size=lambda inst, val: setattr(label, "height", val[1] + 10))
        label.bind(width=lambda inst, val: setattr(label, "text_size", (val, None)))
        self.add_widget(label)
        self.bind(minimum_height=self.setter("height"))


class ChatScreen(Screen):
    peer_name = StringProperty("")
    peer = None

    def set_peer(self, peer):
        self.peer = peer
        self.peer_name = peer["name"]
        self.ids.message_list.clear_widgets()

    def on_back(self):
        self.manager.transition = SlideTransition(direction="right")
        self.manager.current = "users"

    def on_call(self):
        App.get_running_app().start_call(self.peer["ip"], self.peer["name"])

    def on_send(self, text):
        text = text.strip()
        if not text:
            return
        app = App.get_running_app()
        peer_ip = self.peer["ip"]
        peer_name = self.peer_name

        def do_send():
            try:
                app.message_server.send_message(peer_ip, text)
            except OSError:
                Clock.schedule_once(lambda dt: self._on_send_failed(peer_name), 0)

        threading.Thread(target=do_send, daemon=True).start()
        self.append_message(app.display_name, text, mine=True)
        self.ids.chat_input.text = ""

    def _on_send_failed(self, peer_name):
        if self.peer_name == peer_name:
            self.append_message("System", "Failed to send: peer unreachable", mine=False)

    def append_message(self, sender, text, mine):
        bubble = ChatBubble(text, sender, mine)
        self.ids.message_list.add_widget(bubble)
        Clock.schedule_once(lambda dt: setattr(self.ids.chat_scroll, "scroll_y", 0), 0.05)

    def receive_message(self, msg):
        if self.peer and msg.get("ip") == self.peer.get("ip"):
            self.append_message(msg.get("from_name", "Unknown"), msg.get("text", ""), mine=False)


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
        self.store = JsonStore(self.user_data_dir + "/lancom.json")

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
                ])
            except Exception:
                pass

        if self.store.exists("profile"):
            name = self.store.get("profile").get("name", "")
            if name:
                self.set_display_name(name)
                self.root.current = "users"
                return
        self.root.current = "setup"

    def set_display_name(self, name):
        self.display_name = name
        self.store.put("profile", name=name)

        self.discovery = PeerDiscovery(name)
        self.discovery.start()

        self.message_server = MessageServer(name, self._on_message_received)
        self.message_server.start()

        self.call_manager = CallManager(name, self._on_call_event)
        self.call_manager.start()

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

    def start_call(self, ip, name):
        if self.call_manager.call(ip, name):
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


if __name__ == "__main__":
    LancomApp().run()
