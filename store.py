"""
Local persistent storage for LANCOM - contacts, message history, call log,
and file-transfer records. SQLite, one file in the app's private data dir.
Survives app restarts and works fully offline (it's the only "backend").

Message text and filenames are encrypted at rest with a key derived from
this device's own identity key (see crypto_util.Identity.storage_key) -
never transmitted, purely local protection if the DB file is pulled off
the device. Everything else (peer ids, timestamps, direction, sizes) stays
in plaintext since it's needed for indexing/sorting and isn't sensitive
on its own.
"""

import base64
import sqlite3
import threading
import time

import crypto_util


class Store:
    def __init__(self, db_path, storage_key):
        self._lock = threading.Lock()
        self._storage_key = storage_key
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS peers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    last_ip TEXT,
                    pubkey TEXT,
                    last_seen REAL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_id TEXT NOT NULL,
                    peer_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    text TEXT,
                    timestamp REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS call_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_id TEXT NOT NULL,
                    peer_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    duration REAL,
                    timestamp REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_id TEXT NOT NULL,
                    peer_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size INTEGER,
                    path TEXT,
                    status TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer_id);
                CREATE INDEX IF NOT EXISTS idx_calllog_peer ON call_log(peer_id);
                CREATE INDEX IF NOT EXISTS idx_files_peer ON files(peer_id);
            """)
            # Upgrading an older DB that predates the pubkey column.
            try:
                self._conn.execute("ALTER TABLE peers ADD COLUMN pubkey TEXT")
            except sqlite3.OperationalError:
                pass
            self._conn.commit()

    def _seal(self, plaintext):
        if plaintext is None:
            return None
        blob = crypto_util.encrypt(self._storage_key, plaintext.encode("utf-8"))
        return base64.b64encode(blob).decode("ascii")

    def _unseal(self, sealed):
        if sealed is None:
            return None
        try:
            blob = base64.b64decode(sealed)
            return crypto_util.decrypt(self._storage_key, blob).decode("utf-8")
        except Exception:
            return "[unreadable]"

    # -- peers / contacts -------------------------------------------------

    def upsert_peer(self, peer_id, name, ip, pubkey_b64):
        with self._lock:
            self._conn.execute(
                "INSERT INTO peers (id, name, last_ip, pubkey, last_seen) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "last_ip=excluded.last_ip, pubkey=excluded.pubkey, last_seen=excluded.last_seen",
                (peer_id, name, ip, pubkey_b64, time.time()),
            )
            self._conn.commit()

    def get_known_peers(self):
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, name, last_ip, pubkey, last_seen FROM peers ORDER BY name COLLATE NOCASE"
            )
            return [
                {"id": r[0], "name": r[1], "ip": r[2], "pubkey": r[3], "last_seen": r[4]}
                for r in cur.fetchall()
            ]

    def get_peer_pubkey(self, peer_id):
        with self._lock:
            cur = self._conn.execute("SELECT pubkey FROM peers WHERE id=?", (peer_id,))
            row = cur.fetchone()
            return row[0] if row else None

    # -- messages -----------------------------------------------------------

    def add_message(self, peer_id, peer_name, direction, text):
        ts = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (peer_id, peer_name, direction, text, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (peer_id, peer_name, direction, self._seal(text), ts),
            )
            self._conn.commit()
        return ts

    def get_messages(self, peer_id, limit=300):
        with self._lock:
            cur = self._conn.execute(
                "SELECT direction, text, timestamp FROM messages "
                "WHERE peer_id=? ORDER BY timestamp ASC LIMIT ?",
                (peer_id, limit),
            )
            rows = cur.fetchall()
        return [
            {"direction": r[0], "text": self._unseal(r[1]), "timestamp": r[2]}
            for r in rows
        ]

    # -- call log -------------------------------------------------------

    def add_call_log(self, peer_id, peer_name, direction, outcome, duration=0.0):
        ts = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO call_log (peer_id, peer_name, direction, outcome, duration, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (peer_id, peer_name, direction, outcome, duration, ts),
            )
            self._conn.commit()

    def get_call_log(self, peer_id=None, limit=100):
        with self._lock:
            if peer_id:
                cur = self._conn.execute(
                    "SELECT peer_id, peer_name, direction, outcome, duration, timestamp "
                    "FROM call_log WHERE peer_id=? ORDER BY timestamp DESC LIMIT ?",
                    (peer_id, limit),
                )
            else:
                cur = self._conn.execute(
                    "SELECT peer_id, peer_name, direction, outcome, duration, timestamp "
                    "FROM call_log ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
            return [
                {
                    "peer_id": r[0], "peer_name": r[1], "direction": r[2],
                    "outcome": r[3], "duration": r[4], "timestamp": r[5],
                }
                for r in cur.fetchall()
            ]

    # -- file transfers ---------------------------------------------------

    def add_file_record(self, peer_id, peer_name, direction, filename, size, path, status):
        ts = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO files (peer_id, peer_name, direction, filename, size, path, status, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (peer_id, peer_name, direction, self._seal(filename), size,
                 self._seal(path), status, ts),
            )
            self._conn.commit()

    def get_files(self, peer_id, limit=100):
        with self._lock:
            cur = self._conn.execute(
                "SELECT direction, filename, size, path, status, timestamp FROM files "
                "WHERE peer_id=? ORDER BY timestamp ASC LIMIT ?",
                (peer_id, limit),
            )
            rows = cur.fetchall()
        return [
            {
                "direction": r[0], "filename": self._unseal(r[1]), "size": r[2],
                "path": self._unseal(r[3]), "status": r[4], "timestamp": r[5],
            }
            for r in rows
        ]
