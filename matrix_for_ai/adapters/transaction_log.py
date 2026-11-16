import os
import sqlite3
from pathlib import Path


TRANSACTIONS_KEPT = 1000
DATABASE_FILENAME = "transactions.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL UNIQUE,
    events_done INTEGER NOT NULL
);
"""


class TransactionLog:
    def __init__(self, folder: Path) -> None:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / DATABASE_FILENAME
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        self._database = sqlite3.connect(path)
        self._database.execute("PRAGMA journal_mode = WAL")
        self._database.execute("PRAGMA synchronous = NORMAL")
        self._database.executescript(_SCHEMA)

    def events_done(self, transaction_id: str) -> int:
        row = self._database.execute(
            "SELECT events_done FROM transactions WHERE transaction_id = ?",
            (transaction_id,)).fetchone()
        return 0 if row is None else row[0]

    def record_events_done(self, transaction_id: str, events_done: int) -> None:
        with self._database as database:
            database.execute(
                "INSERT INTO transactions (transaction_id, events_done) VALUES (?, ?) "
                "ON CONFLICT (transaction_id) DO UPDATE SET events_done = excluded.events_done",
                (transaction_id, events_done))
            database.execute(
                "DELETE FROM transactions WHERE position <= "
                "(SELECT max(position) FROM transactions) - ?", (TRANSACTIONS_KEPT,))

    def close(self) -> None:
        self._database.close()
