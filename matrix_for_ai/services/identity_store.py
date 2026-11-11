import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


UNREAD_EVENTS_KEPT_PER_CHANNEL = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    room TEXT PRIMARY KEY,
    last_ts INTEGER,
    dropped_unread INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS unread_events (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    room TEXT NOT NULL REFERENCES channels (room) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    sender TEXT,
    ts INTEGER,
    undecryptable INTEGER NOT NULL DEFAULT 0,
    UNIQUE (room, event_id)
);
CREATE TABLE IF NOT EXISTS api_token (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    token_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    url TEXT NOT NULL,
    secret TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    tts TEXT NOT NULL,
    stt TEXT NOT NULL,
    transcribe_audio INTEGER NOT NULL
);
"""
DATABASE_FILENAME = "identity.sqlite3"


@dataclass(frozen=True)
class UnreadEvent:
    event_id: str
    sender: str | None
    ts: int | None


@dataclass(frozen=True)
class ChannelState:
    room: str
    unread: int
    last_unread: UnreadEvent | None
    last_ts: int | None


@dataclass(frozen=True)
class Webhook:
    url: str
    secret: str


@dataclass(frozen=True)
class Profile:
    tts: dict = field(default_factory=dict)
    stt: dict = field(default_factory=dict)
    transcribe_audio: bool = True


class IdentityStore:
    def __init__(self, folder: Path) -> None:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / DATABASE_FILENAME
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        self._database = sqlite3.connect(path)
        self._database.execute("PRAGMA journal_mode = WAL")
        self._database.execute("PRAGMA synchronous = NORMAL")
        self._database.execute("PRAGMA foreign_keys = ON")
        self._database.executescript(_SCHEMA)
        self.mailbox = Mailbox(self._database)

    def token_sha256(self) -> str | None:
        row = self._database.execute("SELECT token_sha256 FROM api_token").fetchone()
        return None if row is None else row[0]

    def set_token_sha256(self, token_sha256: str) -> None:
        with self._database:
            self._database.execute(
                "INSERT OR REPLACE INTO api_token (singleton, token_sha256) VALUES (1, ?)",
                (token_sha256,))

    def delete_token(self) -> bool:
        with self._database:
            return bool(self._database.execute("DELETE FROM api_token").rowcount)

    def webhook(self) -> Webhook | None:
        row = self._database.execute("SELECT url, secret FROM webhook").fetchone()
        return None if row is None else Webhook(*row)

    def set_webhook(self, webhook: Webhook) -> None:
        with self._database:
            self._database.execute(
                "INSERT OR REPLACE INTO webhook (singleton, url, secret) VALUES (1, ?, ?)",
                (webhook.url, webhook.secret))

    def delete_webhook(self) -> bool:
        with self._database:
            return bool(self._database.execute("DELETE FROM webhook").rowcount)

    def profile(self) -> Profile:
        row = self._database.execute(
            "SELECT tts, stt, transcribe_audio FROM profile").fetchone()
        if row is None:
            return Profile()
        tts, stt, transcribe_audio = row
        return Profile(json.loads(tts), json.loads(stt), bool(transcribe_audio))

    def set_profile(self, profile: Profile) -> None:
        with self._database:
            self._database.execute(
                "INSERT OR REPLACE INTO profile (singleton, tts, stt, transcribe_audio) "
                "VALUES (1, ?, ?, ?)",
                (json.dumps(profile.tts), json.dumps(profile.stt), int(profile.transcribe_audio)))


class Mailbox:
    def __init__(self, database: sqlite3.Connection) -> None:
        self._database = database

    def touch(self, room_id: str, ts) -> None:
        if ts is None:
            return
        with self._database as database:
            _advance_channel(database, room_id, ts)

    def note_unread(self, room_id: str, event: dict) -> None:
        event_id = event.get("event_id") or ""
        if not event_id:
            return
        ts = event.get("origin_server_ts")
        with self._database as database:
            _advance_channel(database, room_id, ts)
            inserted = database.execute(
                "INSERT OR IGNORE INTO unread_events (room, event_id, sender, ts) VALUES (?, ?, ?, ?)",
                (room_id, event_id, event.get("sender"), ts)).rowcount
            if inserted:
                _drop_beyond_capacity(database, room_id)

    def unread_event_ids(self, room_id: str) -> list[str]:
        return [event_id for (event_id,) in self._database.execute(
            "SELECT event_id FROM unread_events WHERE room = ? ORDER BY position", (room_id,))]

    def undecryptable_event_ids(self, room_id: str) -> set[str]:
        return {event_id for (event_id,) in self._database.execute(
            "SELECT event_id FROM unread_events WHERE room = ? AND undecryptable ORDER BY position",
            (room_id,))}

    def channel(self, room_id: str) -> ChannelState | None:
        row = self._database.execute(
            "SELECT room, last_ts, dropped_unread FROM channels WHERE room = ?",
            (room_id,)).fetchone()
        return None if row is None else self._state_of(*row)

    def channels(self) -> list[ChannelState]:
        rows = self._database.execute(
            "SELECT room, last_ts, dropped_unread FROM channels").fetchall()
        return [self._state_of(*row) for row in rows]

    def unread_count(self, room_id: str) -> int:
        state = self.channel(room_id)
        return 0 if state is None else state.unread

    def mark_undecryptable(self, room_id: str, event_ids) -> None:
        with self._database as database:
            database.executemany(
                "UPDATE unread_events SET undecryptable = 1 WHERE room = ? AND event_id = ?",
                [(room_id, event_id) for event_id in event_ids])

    def forget(self, room_id: str, event_ids) -> None:
        with self._database as database:
            database.executemany(
                "DELETE FROM unread_events WHERE room = ? AND event_id = ?",
                [(room_id, event_id) for event_id in event_ids])

    def ack_through(self, room_id: str, event_id: str) -> None:
        with self._database as database:
            row = database.execute(
                "SELECT position FROM unread_events WHERE room = ? AND event_id = ?",
                (room_id, event_id)).fetchone()
            if row is None:
                return
            drained = database.execute(
                "DELETE FROM unread_events WHERE room = ? AND position <= ? AND NOT undecryptable",
                (room_id, row[0])).rowcount
            if drained:
                database.execute(
                    "UPDATE channels SET dropped_unread = 0 WHERE room = ?", (room_id,))

    def remove_channel(self, room_id: str) -> None:
        with self._database as database:
            database.execute("DELETE FROM channels WHERE room = ?", (room_id,))

    def _state_of(self, room_id, last_ts, dropped_unread) -> ChannelState:
        stored, = self._database.execute(
            "SELECT count(*) FROM unread_events WHERE room = ?", (room_id,)).fetchone()
        newest = self._database.execute(
            "SELECT event_id, sender, ts FROM unread_events WHERE room = ? "
            "ORDER BY position DESC LIMIT 1", (room_id,)).fetchone()
        return ChannelState(
            room=room_id, unread=dropped_unread + stored,
            last_unread=None if newest is None else UnreadEvent(*newest), last_ts=last_ts)


def _advance_channel(database: sqlite3.Connection, room_id: str, ts) -> None:
    database.execute(
        "INSERT INTO channels (room, last_ts) VALUES (?, ?) ON CONFLICT (room) DO UPDATE "
        "SET last_ts = excluded.last_ts WHERE excluded.last_ts IS NOT NULL "
        "AND (channels.last_ts IS NULL OR excluded.last_ts >= channels.last_ts)",
        (room_id, ts))


def _drop_beyond_capacity(database: sqlite3.Connection, room_id: str) -> None:
    stored, = database.execute(
        "SELECT count(*) FROM unread_events WHERE room = ?", (room_id,)).fetchone()
    excess = stored - UNREAD_EVENTS_KEPT_PER_CHANNEL
    if excess <= 0:
        return
    database.execute(
        "DELETE FROM unread_events WHERE position IN (SELECT position FROM unread_events "
        "WHERE room = ? ORDER BY position LIMIT ?)", (room_id, excess))
    database.execute(
        "UPDATE channels SET dropped_unread = dropped_unread + ? WHERE room = ?",
        (excess, room_id))
