"""Private local configuration and recoverable mirror state."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import tomli_w


def home() -> Path:
    override = os.environ.get("FGBRIDGE_HOME")
    if override:
        path = Path(override).expanduser().resolve()
        if path.is_relative_to(Path(__file__).resolve().parents[2]):
            raise ValueError("Data directory must be outside the source repository")
        return path
    return Path.home() / "Library" / "Application Support" / "fgbridge"


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Data directory cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def atomic_private_write(path: Path, content: bytes) -> None:
    private_dir(path.parent)
    if path.is_symlink():
        raise ValueError("Credential file cannot be a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".fgbridge-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_config(value: dict[str, Any]) -> None:
    atomic_private_write(home() / "config.toml", tomli_w.dumps(value).encode("utf-8"))


def load_config() -> dict[str, Any]:
    path = home() / "config.toml"
    if path.is_symlink():
        raise ValueError("Configuration file cannot be a symlink")
    return tomllib.loads(path.read_text("utf-8"))


def save_secrets(value: dict[str, str]) -> None:
    atomic_private_write(home() / "secrets.json", json.dumps(value).encode("utf-8"))


def load_secrets() -> dict[str, str]:
    path = home() / "secrets.json"
    if path.is_symlink():
        raise ValueError("Credential file cannot be a symlink")
    return json.loads(path.read_text("utf-8"))


@contextmanager
def process_lock() -> Iterator[None]:
    private_dir(home())
    path = home() / "sync.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning("A sync is already running") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class AlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class Mapping:
    key: str
    series_key: str
    google_id: str
    generation: int
    last_start: str
    last_end: str
    misses: int


class State:
    def __init__(self) -> None:
        private_dir(home())
        path = home() / "state.sqlite3"
        if path.is_symlink():
            raise ValueError("State database cannot be a symlink")
        self.conn = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mappings (
              source_key TEXT PRIMARY KEY,
              series_key TEXT NOT NULL,
              google_id TEXT NOT NULL,
              generation INTEGER NOT NULL,
              last_start TEXT NOT NULL,
              last_end TEXT NOT NULL,
              misses INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS intents (
              source_key TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              google_id TEXT NOT NULL,
              generation INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )

    def close(self) -> None:
        self.conn.close()

    def reset_for_new_binding(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM mappings")
            self.conn.execute("DELETE FROM intents")
            self.conn.execute("DELETE FROM meta")

    def mappings(self) -> dict[str, Mapping]:
        rows = self.conn.execute("SELECT * FROM mappings").fetchall()
        return {
            row["source_key"]: Mapping(
                key=row["source_key"],
                series_key=row["series_key"],
                google_id=row["google_id"],
                generation=row["generation"],
                last_start=row["last_start"],
                last_end=row["last_end"],
                misses=row["misses"],
            )
            for row in rows
        }

    def put_mapping(self, mapping: Mapping) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO mappings VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                series_key=excluded.series_key, google_id=excluded.google_id,
                generation=excluded.generation, last_start=excluded.last_start,
                last_end=excluded.last_end, misses=excluded.misses""",
                (
                    mapping.key, mapping.series_key, mapping.google_id, mapping.generation,
                    mapping.last_start, mapping.last_end, mapping.misses,
                ),
            )

    def delete_mapping(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM mappings WHERE source_key=?", (key,))
            self.conn.execute("DELETE FROM intents WHERE source_key=?", (key,))

    def intent(self, key: str, kind: str, google_id: str, generation: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO intents VALUES (?, ?, ?, ?)",
                (key, kind, google_id, generation),
            )

    def intents(self) -> dict[str, sqlite3.Row]:
        return {row["source_key"]: row for row in self.conn.execute("SELECT * FROM intents")}

    def clear_intent(self, key: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM intents WHERE source_key=?", (key,))

    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
