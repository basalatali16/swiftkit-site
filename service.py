"""
LANCOM foreground service - the process that actually IS the app.

Runs the entire networking stack (netcore.NetworkCore) inside a sticky
Android foreground service, so messages, calls, and file transfers keep
arriving when the UI is closed, the screen is locked, or the task is
swiped away (Android restarts the service). The UI process is just a
remote control talking to this over lancom_ipc.

Never imports Kivy - this process has no window, no event loop to pause,
and must stay lean.
"""

import json
import os
import time

import android_notify
from lancom_ipc import ControlServer, control_token
from netcore import EventPolicy, IS_ANDROID, NetworkCore, app_data_dir

if IS_ANDROID:
    try:
        from jnius import autoclass
        # Restart the service if the user swipes the task away - "closed
        # app still receives calls" is the whole point of this process.
        PythonService = autoclass("org.kivy.android.PythonService")
        PythonService.mService.setAutoRestartService(True)
    except Exception:
        pass

DATA_DIR = app_data_dir()

core = NetworkCore(DATA_DIR)
policy = EventPolicy(
    core, android_notify.notify_message,
    lambda peer, fg: android_notify.start_ringing(peer,
                                                  show_notification=not fg),
    stop_call_alert=android_notify.stop_ringing)
server = None  # set below; on_event guards against early events


def on_core_event(ev):
    try:
        policy.handle(ev)
    except Exception:
        pass
    if server is not None:
        server.broadcast(ev)


core.on_event = on_core_event

# Multicast/wifi/wake locks live in THIS process now - it does the
# networking, so it's the one that must survive the screen going off.
android_notify.acquire_background_locks()


def load_profile_name():
    """The UI persists the display name via Kivy's JsonStore - plain JSON,
    readable here without Kivy: {"profile": {"name": "..."}}."""
    try:
        with open(os.path.join(DATA_DIR, "lancom.json"), encoding="utf-8") as f:
            return (json.load(f).get("profile") or {}).get("name") or ""
    except Exception:
        return ""


def handle_command(cmd):
    op = cmd.get("cmd")
    if op == "start":
        name = cmd.get("name") or load_profile_name()
        if name:
            core.start(name)
        return {"ok": core.started}
    if op == "state":
        state = core.get_state()
        state["ok"] = True
        return state
    if op == "set_view":
        policy.set_state(cmd.get("foreground"), cmd.get("viewing"))
        return {"ok": True}
    if op == "send_message":
        msg_id = core.send_message(cmd["peer_id"], cmd["peer_name"],
                                   cmd.get("peer_ip"), cmd["text"])
        return {"ok": True, "msg_id": msg_id}
    if op == "chat_opened":
        core.chat_opened(cmd["peer_id"], cmd.get("peer_ip"))
        return {"ok": True}
    if op == "typing":
        core.send_typing(cmd["peer_id"], cmd.get("peer_ip"), cmd.get("typing"))
        return {"ok": True}
    if op == "delete_message":
        core.delete_message(cmd["peer_id"], row_id=cmd.get("row_id"),
                            msg_id=cmd.get("msg_id"))
        return {"ok": True}
    if op == "clear_chat":
        core.clear_chat(cmd["peer_id"])
        return {"ok": True}
    if op == "probe_ip":
        return {"ok": bool(core.probe_ip(cmd["ip"]))}
    if op == "call":
        ok = core.call(cmd["peer_id"], cmd["peer_ip"], cmd["peer_name"])
        return {"ok": bool(ok)}
    if op == "accept":
        android_notify.stop_ringing()
        core.accept_call()
        return {"ok": True}
    if op == "reject":
        android_notify.stop_ringing()
        core.reject_call()
        return {"ok": True}
    if op == "hang_up":
        android_notify.stop_ringing()
        core.hang_up()
        return {"ok": True}
    if op == "call_state":
        state = core.call_state()
        state["ok"] = True
        return state
    if op == "send_file":
        core.send_file(cmd["peer_id"], cmd["peer_name"], cmd["peer_ip"],
                       cmd["path"])
        return {"ok": True}
    if op == "pending_transfers":
        return {"ok": True, "transfers": core.pending_transfers(cmd["peer_id"])}
    return {"ok": False, "error": f"unknown command {op!r}"}


server = ControlServer(control_token(core.identity), handle_command)
server.start()

# Come up on our own after a reboot-less restart (sticky/swipe-away):
# the UI isn't there to send "start", so use the persisted profile.
name = load_profile_name()
if name:
    core.start(name)

while True:
    time.sleep(60)
