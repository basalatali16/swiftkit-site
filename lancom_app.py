"""
LANCOM app UI.

This process draws the screens - it does NO networking of its own. All
discovery/messaging/calls/files run in netcore.NetworkCore, which lives:

- on Android: inside the sticky foreground service (service.py), so
  everything keeps working when this UI is closed, the phone is locked,
  or the task is swiped away. The UI talks to it over the localhost
  control channel (lancom_ipc) and re-attaches whenever it reconnects.
- on desktop: inside this process (DirectBackend), purely so the app
  can be run and tested without an Android device.

The UI reads chat history straight from the shared SQLite store
(read-only - the networking process owns all writes; WAL makes the
cross-process read safe).
"""

import base64
import os
import threading
import time

from kivy.app import App
from kivy.clock import Clock
from kivy.graphics import Color, Ellipse, RoundedRectangle
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import StringProperty, BooleanProperty
from kivy.resources import resource_find
from kivy.storage.jsonstore import JsonStore
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.image import Image as KivyImage
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen, SlideTransition
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import platform, escape_markup

import android_notify
import crypto_util
from lancom_ipc import ControlClient, control_token
from netcore import IMAGE_EXTS, EventPolicy, NetworkCore
from store import Store

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

# Kivy's default Roboto has no check-mark glyph (U+2713 renders as a
# hollow box), but Kivy also bundles DejaVuSans which does - the message
# status ticks switch to it via a [font=...] markup tag. The fonts dir
# isn't on Kivy's resource path, so resolve it against the package.
import kivy as _kivy  # noqa: E402  (kivy already imported via submodules)

TICK_FONT = os.path.join(os.path.dirname(_kivy.__file__),
                         "data", "fonts", "DejaVuSans.ttf")
if not os.path.exists(TICK_FONT):
    TICK_FONT = resource_find("DejaVuSans.ttf")  # last-ditch fallback


def ticks_markup(status):
    """WhatsApp-style status suffix for an outgoing bubble: '…' queued,
    dim double-check delivered, gold double-check seen."""
    if status == "pending":
        return " [color=#8d8d99]…[/color]"
    if status in ("delivered", "seen"):
        color = "#d8b26c" if status == "seen" else "#8d8d99"
        if TICK_FONT:
            return f" [color={color}][font={TICK_FONT}]✓✓[/font][/color]"
        return f" [color={color}]✓✓[/color]"
    return ""


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


def resolve_android_content_uri(uri_str, dest_dir):
    """Android's Storage Access Framework file picker returns a content://
    URI rather than a plain filesystem path - open(path, "rb") can't read
    that directly. Copy it via ContentResolver to a real local file first
    and send that instead. Returns the local path. UI-process only (needs
    the activity's ContentResolver + the picker's grant)."""
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


def android_pick_file(callback):
    """Open Android's system document picker (Storage Access Framework,
    ACTION_OPEN_DOCUMENT). Needs no storage permission on ANY Android
    version - the system UI hands us a content:// URI for just the file
    the user chose. callback(uri_string) runs on the Kivy main thread."""
    from android import activity as android_activity
    from jnius import autoclass

    Intent = autoclass("android.content.Intent")
    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    RESULT_OK = -1
    request_code = 0x4C46  # "LF" - LANCOM file

    def on_result(request, result, data):
        if request != request_code:
            return
        android_activity.unbind(on_activity_result=on_result)
        if result != RESULT_OK or data is None:
            return
        uri = data.getData()
        if uri is None:
            return
        uri_str = uri.toString()
        Clock.schedule_once(lambda dt: callback(uri_str), 0)

    android_activity.bind(on_activity_result=on_result)
    intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
    intent.addCategory(Intent.CATEGORY_OPENABLE)
    intent.setType("*/*")
    PythonActivity.mActivity.startActivityForResult(intent, request_code)


# ---------------------------------------------------------------------------
# Backends - how the UI reaches the networking core
# ---------------------------------------------------------------------------

class DirectBackend:
    """Desktop: run the whole networking core inside the UI process.
    Method surface is identical to ServiceBackend so the screens never
    care which one they're talking to."""

    def __init__(self, data_dir, on_event):
        self._ui_event = on_event
        self.core = NetworkCore(data_dir, on_event=self._handle)
        self.identity = self.core.identity
        self.policy = EventPolicy(self.core, android_notify.notify_message,
                                  android_notify.notify_incoming_call)

    def _handle(self, ev):
        try:
            self.policy.handle(ev)
        except Exception:
            pass
        self._ui_event(ev)

    def start(self, name):
        self.core.start(name)

    def stop(self):
        self.core.stop()

    def get_state(self):
        state = self.core.get_state()
        return state if state.get("started") else None

    def set_view(self, foreground, viewing):
        self.policy.set_state(foreground, viewing)

    def send_message(self, peer_id, peer_name, peer_ip, text):
        return self.core.send_message(peer_id, peer_name, peer_ip, text)

    def chat_opened(self, peer_id, peer_ip):
        self.core.chat_opened(peer_id, peer_ip)

    def probe_ip(self, ip):
        return self.core.probe_ip(ip)

    def call(self, peer_id, peer_ip, peer_name):
        return self.core.call(peer_id, peer_ip, peer_name)

    def accept_call(self):
        self.core.accept_call()

    def reject_call(self):
        self.core.reject_call()

    def hang_up(self):
        self.core.hang_up()

    def send_file(self, peer_id, peer_name, peer_ip, path):
        self.core.send_file(peer_id, peer_name, peer_ip, path)


class ServiceBackend:
    """Android: the networking core lives in the foreground service
    process; this is the remote control. Auto-reconnects (service
    restarts, UI restarts) and replays session state on every connect."""

    def __init__(self, data_dir, on_event):
        self._ui_event = on_event
        self.identity = crypto_util.Identity.load_or_create(
            os.path.join(data_dir, "identity.key"))
        self._name = None
        self._view = (True, None)
        self.client = ControlClient(control_token(self.identity), on_event,
                                    on_connected=self._replay_state)

    def start(self, name):
        self._name = name
        self._start_service()
        self.client.start()

    def stop(self):
        # The service deliberately keeps running - being reachable while
        # the UI is gone is its entire purpose.
        pass

    def _start_service(self):
        try:
            from jnius import autoclass
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            service = autoclass("com.localnet.lancomm.ServiceLancomnet")
            service.start(activity, "", "LANCOM",
                          "Connected - receiving messages and calls", "")
        except Exception:
            pass

    def _replay_state(self):
        """Runs after every (re)connect: make sure the service is started
        with our name, restore visibility state, and surface a call that
        was already ringing (user opened the app from the notification)."""
        if self._name:
            self.client.notify({"cmd": "start", "name": self._name})
        fg, viewing = self._view
        self.client.notify({"cmd": "set_view", "foreground": fg,
                            "viewing": viewing})
        reply = self.client.request({"cmd": "call_state"}, timeout=2.0)
        if reply and reply.get("state") == "ringing":
            self._ui_event({"kind": "call",
                            "event": {"event": "incoming_call",
                                      "peer_name": reply.get("peer_name")
                                      or "Unknown"}})

    def get_state(self):
        reply = self.client.request({"cmd": "state"}, timeout=1.5)
        if reply and reply.get("started"):
            return reply
        return None

    def set_view(self, foreground, viewing):
        self._view = (foreground, viewing)
        self.client.notify({"cmd": "set_view", "foreground": foreground,
                            "viewing": viewing})

    def send_message(self, peer_id, peer_name, peer_ip, text):
        reply = self.client.request({"cmd": "send_message", "peer_id": peer_id,
                                     "peer_name": peer_name, "peer_ip": peer_ip,
                                     "text": text})
        return reply.get("msg_id") if reply else None

    def chat_opened(self, peer_id, peer_ip):
        self.client.notify({"cmd": "chat_opened", "peer_id": peer_id,
                            "peer_ip": peer_ip})

    def probe_ip(self, ip):
        reply = self.client.request({"cmd": "probe_ip", "ip": ip})
        return bool(reply and reply.get("ok"))

    def call(self, peer_id, peer_ip, peer_name):
        reply = self.client.request({"cmd": "call", "peer_id": peer_id,
                                     "peer_ip": peer_ip, "peer_name": peer_name})
        return bool(reply and reply.get("ok"))

    def accept_call(self):
        self.client.notify({"cmd": "accept"})

    def reject_call(self):
        self.client.notify({"cmd": "reject"})

    def hang_up(self):
        self.client.notify({"cmd": "hang_up"})

    def send_file(self, peer_id, peer_name, peer_ip, path):
        self.client.notify({"cmd": "send_file", "peer_id": peer_id,
                            "peer_name": peer_name, "peer_ip": peer_ip,
                            "path": path})


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
        if not app.backend:
            return
        state = app.backend.get_state()
        if not state:
            self.ids.debug_info.text = "Starting network service..."
            return
        self.ids.debug_info.text = (
            f"This device: {state['local_ip']}  "
            f"|  broadcasting to: {state['broadcast_ip']}"
        )
        peers = state["peers"]
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
            if app.backend:
                app.backend.probe_ip(ip)
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

    def __init__(self, text, mine, timestamp=None, status=None, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None,
                          padding=(dp(2), dp(1)), **kwargs)
        self._mine = mine
        self._status = status if mine else None
        self._base = escape_markup(text)
        self._stamp = ""
        if timestamp:
            self._stamp = (f" [size={int(dp(10))}][color=#9a93a4]"
                           f"{format_time(timestamp)}[/color][/size]")

        self._label = Label(text=self._compose(), markup=True, color=COLOR_TEXT,
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

    def _compose(self):
        extra = ticks_markup(self._status) if self._status else ""
        return self._base + self._stamp + extra

    def set_status(self, status):
        """Live receipt update: pending -> delivered -> seen ticks."""
        if not self._mine or status == self._status:
            return
        self._status = status
        self._label.text = self._compose()
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


class ChatImageBubble(BoxLayout):
    """Inline picture preview for image files in the chat - a rounded
    frame around the image, right-aligned for sent, left for received."""

    MAX_FRAC = 0.65
    MAX_H = 280

    def __init__(self, path, mine, **kwargs):
        super().__init__(orientation="horizontal", size_hint_y=None,
                          padding=(dp(2), dp(2)), **kwargs)
        self._img = KivyImage(source=path, size_hint=(None, None),
                               allow_stretch=True, keep_ratio=True)
        self._frame = BoxLayout(size_hint=(None, None), padding=(dp(4), dp(4)))
        with self._frame.canvas.before:
            Color(*(COLOR_BUBBLE_MINE if mine else COLOR_CARD))
            self._rect = RoundedRectangle(pos=self._frame.pos,
                                           size=self._frame.size, radius=[dp(12)])
        self._frame.bind(pos=self._sync, size=self._sync)
        self._frame.add_widget(self._img)

        if mine:
            self.add_widget(Widget())
            self.add_widget(self._frame)
        else:
            self.add_widget(self._frame)
            self.add_widget(Widget())

        self._img.bind(texture=self._fit)
        self.bind(width=self._fit)
        self._fit()

    def _sync(self, *_args):
        self._rect.pos = self._frame.pos
        self._rect.size = self._frame.size

    def _fit(self, *_args):
        tex = self._img.texture
        if tex is None or not tex.width or not tex.height:
            # Texture not loaded (yet) - keep a placeholder footprint.
            self._frame.size = (dp(120), dp(90))
            self.height = self._frame.height + dp(4)
            return
        max_w = max(min(self.width * self.MAX_FRAC, dp(260)), dp(80))
        scale = min(max_w / tex.width, dp(self.MAX_H) / tex.height)
        w, h = tex.width * scale, tex.height * scale
        self._img.size = (w, h)
        self._frame.size = (w + dp(8), h + dp(8))
        self.height = self._frame.height + dp(4)


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
    _bubbles = None

    def set_peer(self, peer):
        self.peer = peer
        self.peer_name = peer["name"]
        self.peer_online = bool(peer.get("online"))
        self.peer_status = "Online" if self.peer_online else f"Last seen {format_last_seen(peer.get('last_seen'))}"
        self._last_day = None
        self._bubbles = {}  # msg_id -> ChatBubble (mine only, for receipts)
        self.ids.message_list.clear_widgets()
        self.load_history()
        App.get_running_app().set_viewing(peer)

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
                self.append_message(item["text"], mine=mine, timestamp=item["timestamp"],
                                    msg_id=item.get("msg_id"), status=item.get("status"))
            elif kind == "call":
                self.append_event(self._call_log_text(item), timestamp=item["timestamp"])
            elif kind == "file":
                path = item.get("path") or ""
                if (item["status"] == "completed" and path
                        and path.lower().endswith(IMAGE_EXTS)
                        and os.path.exists(path)):
                    self.append_image(path, mine=item["direction"] == "out",
                                      timestamp=item["timestamp"])
                else:
                    self.append_event(self._file_log_text(item), timestamp=item["timestamp"])

    def on_back(self):
        App.get_running_app().set_viewing(None)
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
        # The backend queues it (survives restarts and offline peers) and
        # tries to deliver right away. The bubble starts with the pending
        # "…" and upgrades to ticks as receipts come back.
        msg_id = app.backend.send_message(self.peer["id"], self.peer_name,
                                          self.peer["ip"], text)
        self.append_message(text, mine=True, msg_id=msg_id, status="pending")
        self.ids.chat_input.text = ""

    def on_send_file(self):
        if platform == "android":
            # SAF document picker: works on every Android version with no
            # storage permission at all, unlike plyer's filechooser which
            # relies on READ_EXTERNAL_STORAGE (dead for general files on
            # API 33+) and crashes/returns nothing on modern phones.
            try:
                android_pick_file(self._start_file_send)
            except Exception:
                self.append_event("Couldn't open the file picker")
            return
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

        def do_send():
            local = path
            if local.startswith("content://"):
                # Copying out of the ContentResolver can take a while for
                # big files - do it here on the worker, never the UI thread.
                # This must happen in the UI process (the picker's grant is
                # per-process); the service then reads the copied file.
                try:
                    local_dir = os.path.join(app.user_data_dir, "outgoing_tmp")
                    local = resolve_android_content_uri(local, local_dir)
                except Exception:
                    Clock.schedule_once(lambda dt: self.append_event(
                        "Couldn't read that file - try picking it again"), 0)
                    return
            Clock.schedule_once(lambda dt: self.append_event(
                f"Sending {os.path.basename(local)}…"), 0)
            app.backend.send_file(peer["id"], peer["name"], peer["ip"], local)

        threading.Thread(target=do_send, daemon=True).start()

    def update_message_status(self, msg_id, status):
        bubble = self._bubbles.get(msg_id) if self._bubbles else None
        if bubble:
            bubble.set_status(status)

    def _maybe_date_chip(self, ts):
        day = time.localtime(ts)[:3]
        if day != self._last_day:
            self._last_day = day
            self.ids.message_list.add_widget(DateChip(format_date_chip(ts)))

    def append_message(self, text, mine, timestamp=None, msg_id=None, status=None):
        ts = timestamp or time.time()
        self._maybe_date_chip(ts)
        bubble = ChatBubble(text, mine, timestamp=ts, status=status)
        if mine and msg_id and self._bubbles is not None:
            self._bubbles[msg_id] = bubble
        self.ids.message_list.add_widget(bubble)
        Clock.schedule_once(lambda dt: setattr(self.ids.chat_scroll, "scroll_y", 0), 0.05)

    def append_event(self, text, on_retry=None, timestamp=None):
        ts = timestamp or time.time()
        self._maybe_date_chip(ts)
        self.ids.message_list.add_widget(ChatEvent(text, on_retry=on_retry))
        Clock.schedule_once(lambda dt: setattr(self.ids.chat_scroll, "scroll_y", 0), 0.05)

    def append_image(self, path, mine, timestamp=None):
        ts = timestamp or time.time()
        self._maybe_date_chip(ts)
        self.ids.message_list.add_widget(ChatImageBubble(path, mine))
        Clock.schedule_once(lambda dt: setattr(self.ids.chat_scroll, "scroll_y", 0), 0.05)

    def receive_message(self, msg):
        if self.peer and msg.get("from_id") == self.peer.get("id"):
            self.append_message(msg.get("text", ""), mine=False)

    def receive_file_event(self, event):
        if not self.peer or self.peer.get("id") != event.get("peer_id"):
            return
        direction = "in" if event["event"] == "file_received" else "out"
        path = event.get("path") or ""
        if (event.get("status") == "completed" and path
                and path.lower().endswith(IMAGE_EXTS) and os.path.exists(path)):
            self.append_image(path, mine=direction == "out")
            return
        row = {"direction": direction, "filename": event["filename"],
               "size": event.get("size", 0), "status": event.get("status", "failed")}
        text = self._file_log_text(row)
        if event.get("saved_to"):
            text += f" – saved to {event['saved_to']}"
        on_retry = None
        if event["event"] == "file_send_failed" and event.get("path"):
            on_retry = lambda: self._start_file_send(event["path"])
        self.append_event(text, on_retry=on_retry)

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

    def on_ringing(self, peer_name):
        self.peer_name = peer_name
        self.status_text = "Ringing..."
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
        App.get_running_app().backend.accept_call()
        self.status_text = "Connecting..."
        self.show_accept = False

    def on_reject_or_hangup(self):
        app = App.get_running_app()
        if self.show_accept:
            app.backend.reject_call()
            self._return_to_users()
        else:
            app.backend.hang_up()
            self.on_ended("Call ended")


class LancomApp(App):
    display_name = StringProperty("")

    def build(self):
        self.backend = None
        self._is_foreground = True
        self._viewing_peer = None
        self.profile_store = JsonStore(self.user_data_dir + "/lancom.json")

        self.identity = crypto_util.Identity.load_or_create(
            os.path.join(self.user_data_dir, "identity.key"))
        # Read-only view of the shared history DB. All writes happen in
        # the process that owns the networking (the service on Android,
        # the in-process core on desktop); WAL makes this read safe.
        self.store = Store(os.path.join(self.user_data_dir, "lancom.db"),
                           self.identity.storage_key())

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
        # The UI may sleep - the SERVICE keeps communicating. Just tell it
        # we're no longer visible so it starts notifying instead of
        # assuming the user sees the screen.
        self._is_foreground = False
        if self.backend:
            self.backend.set_view(False, self._viewing_peer)
        return True

    def on_resume(self):
        self._is_foreground = True
        if self.backend:
            self.backend.set_view(True, self._viewing_peer)
            # Read-receipt anything that arrived in the open chat while we
            # were paused (its events queued behind the paused Clock).
            if self._viewing_peer and self.root.current == "chat":
                chat = self.root.get_screen("chat")
                if chat.peer:
                    self.backend.chat_opened(chat.peer["id"], chat.peer.get("ip"))

    def set_viewing(self, peer):
        """ChatScreen tells us which conversation is on screen (or None).
        The backend uses it to route notify-vs-mark-seen decisions."""
        self._viewing_peer = peer["id"] if peer else None
        if self.backend:
            self.backend.set_view(self._is_foreground, self._viewing_peer)
            if peer:
                self.backend.chat_opened(peer["id"], peer.get("ip"))

    def set_display_name(self, name):
        self.display_name = name
        self.profile_store.put("profile", name=name)

        def on_event(ev):
            Clock.schedule_once(lambda dt: self._dispatch_event(ev), 0)

        if platform == "android":
            self.backend = ServiceBackend(self.user_data_dir, on_event)
        else:
            self.backend = DirectBackend(self.user_data_dir, on_event)
        self.backend.start(name)
        self.backend.set_view(self._is_foreground, self._viewing_peer)

    def _dispatch_event(self, ev):
        kind = ev.get("kind")
        if kind == "message":
            self.root.get_screen("chat").receive_message(ev.get("msg", {}))
        elif kind == "receipt":
            if self.root.current == "chat":
                self.root.get_screen("chat").update_message_status(
                    ev.get("msg_id"), ev.get("status"))
        elif kind == "call":
            self._dispatch_call_event(ev.get("event", {}))
        elif kind == "file":
            self.root.get_screen("chat").receive_file_event(ev.get("event", {}))
        # "peer_online" needs no UI action - the contacts screen polls.

    def _dispatch_call_event(self, event):
        call_screen = self.root.get_screen("call")
        kind = event.get("event")
        if kind == "incoming_call":
            call_screen.on_incoming(event["peer_name"])
            self.root.transition = SlideTransition(direction="up")
            self.root.current = "call"
        elif kind == "call_ringing":
            call_screen.on_ringing(event["peer_name"])
        elif kind == "call_active":
            call_screen.on_active(event["peer_name"])
        elif kind == "call_rejected":
            reason = ("is busy on another call"
                      if event.get("reason") == "busy" else "declined")
            call_screen.on_ended(f"{event['peer_name']} {reason}")
        elif kind == "call_failed":
            call_screen.on_ended("Call failed")
        elif kind == "call_ended":
            call_screen.on_ended("Call ended")

    def start_call(self, peer_id, ip, name):
        if self.backend and self.backend.call(peer_id, ip, name):
            call_screen = self.root.get_screen("call")
            call_screen.on_calling(name)
            self.root.transition = SlideTransition(direction="up")
            self.root.current = "call"

    def on_stop(self):
        if self.backend:
            self.backend.stop()


if __name__ == "__main__":
    LancomApp().run()
