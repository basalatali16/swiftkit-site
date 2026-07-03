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
        # Two processes share this DB on Android (UI reads, the network
        # service writes). WAL allows that, but momentary lock overlap is
        # normal - wait it out instead of raising "database is locked".
        self._conn.execute("PRAGMA busy_timeout=5000")
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
                CREATE TABLE IF NOT EXISTS transfers (
                    id TEXT PRIMARY KEY,
                    peer_id TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    bytes_done INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    msg_id TEXT PRIMARY KEY,
                    peer_id TEXT NOT NULL,
                    created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer_id);
                CREATE INDEX IF NOT EXISTS idx_calllog_peer ON call_log(peer_id);
                CREATE INDEX IF NOT EXISTS idx_files_peer ON files(peer_id);
            """)
            # Upgrading older DBs that predate these columns.
            for ddl in (
                "ALTER TABLE peers ADD COLUMN pubkey TEXT",
                "ALTER TABLE messages ADD COLUMN msg_id TEXT",
                # Outgoing: '' (pre-status rows) | pending | delivered | seen.
                "ALTER TABLE messages ADD COLUMN status TEXT DEFAULT ''",
                # Incoming: whether we've told the sender we displayed it.
                "ALTER TABLE messages ADD COLUMN seen_receipt_sent INTEGER DEFAULT 0",
            ):
                try:
                    self._conn.execute(ddl)
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

    def add_message(self, peer_id, peer_name, direction, text,
                    msg_id=None, status=""):
        ts = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (peer_id, peer_name, direction, text, "
                "timestamp, msg_id, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (peer_id, peer_name, direction, self._seal(text), ts, msg_id, status),
            )
            self._conn.commit()
        return ts

    def get_messages(self, peer_id, limit=300):
        with self._lock:
            cur = self._conn.execute(
                "SELECT direction, text, timestamp, msg_id, status, id "
                "FROM messages WHERE peer_id=? ORDER BY timestamp ASC LIMIT ?",
                (peer_id, limit),
            )
            rows = cur.fetchall()
        return [
            {"direction": r[0], "text": self._unseal(r[1]), "timestamp": r[2],
             "msg_id": r[3], "status": r[4] or "", "row_id": r[5]}
            for r in rows
        ]

    def delete_message(self, peer_id, row_id=None, msg_id=None):
        """Delete one message locally ("delete for me"). Also removes any
        outbox entry so a deleted still-pending message never gets sent."""
        with self._lock:
            if msg_id:
                self._conn.execute("DELETE FROM outbox WHERE msg_id=?", (msg_id,))
                self._conn.execute(
                    "DELETE FROM messages WHERE peer_id=? AND msg_id=?",
                    (peer_id, msg_id))
            elif row_id is not None:
                self._conn.execute(
                    "DELETE FROM messages WHERE peer_id=? AND id=?",
                    (peer_id, row_id))
            self._conn.commit()

    def clear_chat(self, peer_id):
        """Wipe the whole local timeline with one contact - messages, call
        log, and file records. Files already saved to disk stay there."""
        with self._lock:
            self._conn.execute("DELETE FROM outbox WHERE peer_id=?", (peer_id,))
            self._conn.execute("DELETE FROM messages WHERE peer_id=?", (peer_id,))
            self._conn.execute("DELETE FROM call_log WHERE peer_id=?", (peer_id,))
            self._conn.execute("DELETE FROM files WHERE peer_id=?", (peer_id,))
            self._conn.commit()

    def has_message(self, peer_id, msg_id):
        if not msg_id:
            return False
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM messages WHERE peer_id=? AND msg_id=? LIMIT 1",
                (peer_id, msg_id))
            return cur.fetchone() is not None

    def set_message_status(self, msg_id, status):
        """Upgrade an outgoing message's status. 'seen' beats 'delivered'
        beats 'pending' - receipts can arrive out of order, never downgrade."""
        rank = {"": 0, "pending": 1, "delivered": 2, "seen": 3}
        with self._lock:
            cur = self._conn.execute(
                "SELECT status FROM messages WHERE msg_id=?", (msg_id,))
            row = cur.fetchone()
            if row is None or rank.get(status, 0) <= rank.get(row[0] or "", 0):
                return False
            self._conn.execute(
                "UPDATE messages SET status=? WHERE msg_id=?", (status, msg_id))
            self._conn.commit()
            return True

    def get_unseen_incoming_ids(self, peer_id):
        """Incoming messages we haven't sent a 'seen' receipt for yet."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT msg_id FROM messages WHERE peer_id=? AND direction='in' "
                "AND msg_id IS NOT NULL AND seen_receipt_sent=0", (peer_id,))
            return [r[0] for r in cur.fetchall()]

    def mark_seen_receipts_sent(self, peer_id, msg_ids):
        if not msg_ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE messages SET seen_receipt_sent=1 "
                "WHERE peer_id=? AND msg_id=?",
                [(peer_id, mid) for mid in msg_ids])
            self._conn.commit()

    # -- outbox (store-and-forward for offline peers) --------------------

    def outbox_add(self, msg_id, peer_id):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO outbox (msg_id, peer_id, created) "
                "VALUES (?, ?, ?)", (msg_id, peer_id, time.time()))
            self._conn.commit()

    def outbox_remove(self, msg_id):
        with self._lock:
            self._conn.execute("DELETE FROM outbox WHERE msg_id=?", (msg_id,))
            self._conn.commit()

    def outbox_pending(self, peer_id):
        """Queued messages for one peer, oldest first, with their text."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT o.msg_id, m.text, m.timestamp FROM outbox o "
                "JOIN messages m ON m.msg_id = o.msg_id "
                "WHERE o.peer_id=? ORDER BY m.timestamp ASC", (peer_id,))
            rows = cur.fetchall()
        return [{"msg_id": r[0], "text": self._unseal(r[1]), "timestamp": r[2]}
                for r in rows]

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

    # -- resumable transfers ------------------------------------------
    # A transfer is tracked separately from the completed-history "files"
    # record (add_file_record/get_files above) - this table only holds
    # in-progress/resumable state; a finished transfer also gets a
    # permanent files row and can be cleared from here.

    def start_transfer(self, transfer_id, peer_id, direction, filename, path, size, salt_b64):
        """(Re)initializes a transfer to bytes_done=0. Call only once the
        caller has already decided this is a fresh attempt, not a resume -
        the resume-or-fresh decision belongs in the caller (it needs to
        check the on-disk partial file too, not just this DB row)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO transfers (id, peer_id, direction, filename, path, size, "
                "bytes_done, status, salt, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, 'in_progress', ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET peer_id=excluded.peer_id, direction=excluded.direction, "
                "filename=excluded.filename, path=excluded.path, size=excluded.size, bytes_done=0, "
                "status='in_progress', salt=excluded.salt, updated_at=excluded.updated_at",
                (transfer_id, peer_id, direction, self._seal(filename), self._seal(path),
                 size, salt_b64, time.time()),
            )
            self._conn.commit()

    def update_transfer_progress(self, transfer_id, bytes_done):
        with self._lock:
            self._conn.execute(
                "UPDATE transfers SET bytes_done=?, updated_at=? WHERE id=?",
                (bytes_done, time.time(), transfer_id),
            )
            self._conn.commit()

    def finish_transfer(self, transfer_id, status):
        with self._lock:
            self._conn.execute(
                "UPDATE transfers SET status=?, updated_at=? WHERE id=?",
                (status, time.time(), transfer_id),
            )
            self._conn.commit()

    def get_transfer(self, transfer_id):
        with self._lock:
            cur = self._conn.execute(
                "SELECT peer_id, direction, filename, path, size, bytes_done, status, salt "
                "FROM transfers WHERE id=?",
                (transfer_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "peer_id": row[0], "direction": row[1], "filename": self._unseal(row[2]),
            "path": self._unseal(row[3]), "size": row[4], "bytes_done": row[5],
            "status": row[6], "salt": row[7],
        }
