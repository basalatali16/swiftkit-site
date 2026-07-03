"""
UI <-> service control channel: newline-delimited JSON over a
localhost-only TCP socket.

The service (lancom's foreground service process, which owns ALL
networking) runs ControlServer; the app UI runs ControlClient. Requests
carry an "id" and get exactly one reply with the same id; the server
also pushes {"event": {...}} lines for live events (messages, receipts,
call and file activity).

Auth: the first line from a client must be a hello carrying a token
derived from the device identity's storage key. Both processes read the
same identity file from app-private storage, so only code running as
this app can produce the token - another app on the phone connecting to
the port gets dropped without a byte of data.
"""

import hashlib
import json
import socket
import threading
import time
import uuid

from netcore import CONTROL_PORT


def control_token(identity):
    return hashlib.sha256(identity.storage_key() + b"lancom-control").hexdigest()


class ControlServer:
    def __init__(self, token, handler, port=CONTROL_PORT):
        self.token = token
        self.handler = handler  # callable(cmd_dict) -> reply dict
        self.port = port
        self._clients = []  # authed client sockets
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _accept_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(4)
        srv.settimeout(1.0)
        while self._running:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client_loop, args=(conn,),
                              daemon=True).start()
        srv.close()

    def _client_loop(self, conn):
        conn.settimeout(10.0)
        rfile = conn.makefile("r", encoding="utf-8", newline="\n")
        try:
            hello = json.loads(rfile.readline() or "{}")
            if hello.get("cmd") != "hello" or hello.get("token") != self.token:
                conn.close()
                return
        except Exception:
            conn.close()
            return

        conn.settimeout(None)
        with self._lock:
            self._clients.append(conn)
        try:
            self._send(conn, {"id": hello.get("id"), "ok": True})
            for line in rfile:
                try:
                    cmd = json.loads(line)
                except ValueError:
                    continue
                try:
                    reply = self.handler(cmd) or {"ok": False}
                except Exception:
                    reply = {"ok": False, "error": "handler crashed"}
                if "id" in cmd:
                    reply["id"] = cmd["id"]
                    self._send(conn, reply)
        except OSError:
            pass
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, conn, obj):
        try:
            conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError:
            pass

    def broadcast(self, event):
        """Push a live event line to every connected UI."""
        with self._lock:
            clients = list(self._clients)
        for conn in clients:
            self._send(conn, {"event": event})


class ControlClient:
    """Auto-reconnecting client. request() is synchronous (blocks up to
    `timeout` for the matching reply); events arrive on the reader thread
    via on_event. on_connected fires after every successful (re)connect
    so the caller can replay its session state."""

    def __init__(self, token, on_event, on_connected=None, port=CONTROL_PORT):
        self.token = token
        self.on_event = on_event
        self.on_connected = on_connected
        self.port = port
        self._sock = None
        self._send_lock = threading.Lock()
        self._pending = {}  # id -> [threading.Event, reply]
        self._running = False

    @property
    def connected(self):
        return self._sock is not None

    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._connect_loop, daemon=True).start()

    def stop(self):
        self._running = False
        sock = self._sock
        self._sock = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def _connect_loop(self):
        while self._running:
            try:
                sock = socket.create_connection(("127.0.0.1", self.port),
                                                timeout=3.0)
            except OSError:
                time.sleep(1.5)
                continue
            sock.settimeout(None)
            rfile = sock.makefile("r", encoding="utf-8", newline="\n")
            hello_id = uuid.uuid4().hex[:8]
            try:
                sock.sendall((json.dumps({"cmd": "hello", "id": hello_id,
                                          "token": self.token}) + "\n"
                              ).encode("utf-8"))
                first = json.loads(rfile.readline() or "{}")
                if not first.get("ok"):
                    raise OSError("hello rejected")
            except (OSError, ValueError):
                try:
                    sock.close()
                except OSError:
                    pass
                time.sleep(1.5)
                continue

            self._sock = sock
            if self.on_connected:
                # On its own thread: on_connected may use request(), whose
                # reply is delivered by the read loop below - calling it
                # inline would deadlock until the timeout.
                threading.Thread(target=self._safe_on_connected,
                                  daemon=True).start()
            try:
                for line in rfile:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if "event" in obj:
                        try:
                            self.on_event(obj["event"])
                        except Exception:
                            pass
                    elif "id" in obj:
                        waiter = self._pending.get(obj["id"])
                        if waiter:
                            waiter[1] = obj
                            waiter[0].set()
            except OSError:
                pass
            finally:
                self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
            # fall through -> reconnect (service restart, etc.)

    def _safe_on_connected(self):
        try:
            self.on_connected()
        except Exception:
            pass

    def _send(self, obj):
        sock = self._sock
        if sock is None:
            return False
        try:
            with self._send_lock:
                sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    def notify(self, cmd):
        """Fire-and-forget command (no reply expected)."""
        self._send(cmd)

    def request(self, cmd, timeout=3.0):
        """Send a command and wait for its reply. Returns None when the
        service is unreachable or slow - callers treat that as 'not
        ready yet' and retry on their own schedule."""
        rid = uuid.uuid4().hex[:8]
        cmd = dict(cmd)
        cmd["id"] = rid
        waiter = [threading.Event(), None]
        self._pending[rid] = waiter
        try:
            if not self._send(cmd):
                return None
            if not waiter[0].wait(timeout):
                return None
            return waiter[1]
        finally:
            self._pending.pop(rid, None)
