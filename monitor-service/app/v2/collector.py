"""Local file reader. Cursor and immutable batches commit in the same SQLite transaction."""

import fcntl
import glob
import hashlib
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .config import AppConfig
from .telemetry import Batch, ExcludedRecord, aggregate, parse_record


class Collector:
    def __init__(self, config: AppConfig, spool: Path, max_mb: int = 512):
        self.config, self.spool, self.max_bytes = config, spool, max_mb * 1024 * 1024
        spool.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(str(spool) + ".lock", "a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another collector owns this spool") from None
        self.db = sqlite3.connect(spool)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS cursors (identity TEXT PRIMARY KEY, offset INTEGER NOT NULL, anchor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS config (fingerprint TEXT NOT NULL);
        """)
        current = self.db.execute("SELECT fingerprint FROM config").fetchone()
        if (
            current
            and current[0] != config.fingerprint
            and self.db.execute("SELECT 1 FROM pending LIMIT 1").fetchone()
        ):
            self.close()
            raise RuntimeError("Drain pending batches before changing collector configuration")
        with self.db:
            self.db.execute("DELETE FROM config")
            self.db.execute("INSERT INTO config VALUES (?)", (config.fingerprint,))
        os.chmod(spool, 0o600)

    def pending(self):
        for batch_id, body in self.db.execute(
            "SELECT id, body FROM pending ORDER BY created, rowid LIMIT 100"
        ).fetchall():
            yield batch_id, Batch.model_validate_json(body)

    def acknowledge(self, batch_id: str):
        with self.db:
            self.db.execute("DELETE FROM pending WHERE id=?", (batch_id,))

    def scan(self) -> dict:
        pending_bytes = self.db.execute(
            "SELECT coalesce(sum(length(body)),0) FROM pending"
        ).fetchone()[0]
        if pending_bytes >= self.max_bytes:
            return {"state": "backpressure", "pending_bytes": pending_bytes}
        paths = [Path(p) for p in glob.glob(self.config.rotated_glob) if not p.endswith(".gz")]
        current = Path(self.config.log_path)
        if current.exists():
            paths.append(current)
        paths = sorted(set(paths), key=lambda p: p.stat().st_mtime)
        if not paths:
            return {"state": "missing_log", "pending_bytes": pending_bytes}
        total, invalid, excluded, newest = 0, 0, 0, None
        budget = 8 * 1024 * 1024
        seen = set()
        for path in paths:
            try:
                handle = path.open("rb")
            except FileNotFoundError:
                continue
            with handle:
                stat = os.fstat(handle.fileno())
                identity = f"{stat.st_dev}:{stat.st_ino}"
                if identity in seen:
                    continue
                seen.add(identity)
                existing = self.db.execute(
                    "SELECT offset, anchor FROM cursors WHERE identity=?", (identity,)
                ).fetchone()
                offset, anchor = existing or (0, "")
                if offset:
                    handle.seek(max(0, offset - 128))
                    actual = hashlib.sha256(handle.read(min(128, offset))).hexdigest()
                    if stat.st_size < offset or actual != anchor:
                        offset = 0  # Truncated/reused inode, detected by bytes before the cursor.
                handle.seek(offset)
                records, bad, skipped, consumed = [], 0, 0, 0
                while consumed < budget:
                    start = handle.tell()
                    raw = handle.readline(65537)
                    if not raw:
                        break
                    if len(raw) > 65536:
                        # Discard an oversized complete line without retaining its sensitive content.
                        while raw and not raw.endswith(b"\n"):
                            raw = handle.readline(65537)
                        if not raw.endswith(b"\n"):
                            handle.seek(start)
                            break
                        bad += 1
                    elif not raw.endswith(b"\n"):
                        handle.seek(start)
                        break
                    else:
                        try:
                            record = parse_record(raw, self.config)
                            if record["time"] > time.time() + 300:
                                raise ValueError("future timestamp")
                            records.append(record)
                        except ExcludedRecord:
                            skipped += 1
                        except (
                            ValueError,
                            TypeError,
                            KeyError,
                            UnicodeError,
                            AttributeError,
                        ):
                            bad += 1
                    consumed += handle.tell() - start
                end = handle.tell()
                if end == offset:
                    continue
                handle.seek(max(0, end - 128))
                anchor = hashlib.sha256(handle.read(min(128, end))).hexdigest()
                batch = Batch(
                    id=str(uuid.uuid4()),
                    source=identity,
                    config_hash=self.config.fingerprint,
                    application=self.config.application,
                    environment=self.config.environment,
                    rows=aggregate(records),
                    invalid=bad,
                    excluded=skipped,
                )
                with self.db:
                    self.db.execute(
                        "INSERT INTO pending VALUES (?,?,?)",
                        (batch.id, batch.model_dump_json(), time.time()),
                    )
                    self.db.execute(
                        "INSERT INTO cursors VALUES (?,?,?) ON CONFLICT(identity) DO UPDATE SET offset=excluded.offset, anchor=excluded.anchor",
                        (identity, end, anchor),
                    )
                total += len(records)
                invalid += bad
                excluded += skipped
                if records:
                    newest = max(newest or 0, max(r["time"] for r in records))
                budget -= consumed
                if budget <= 0:
                    break
        return {
            "state": "running",
            "requests": total,
            "invalid_lines": invalid,
            "excluded_lines": excluded,
            "newest_event": newest,
            "pending_bytes": pending_bytes,
        }

    def close(self):
        self.db.close()
        self.lock.close()
