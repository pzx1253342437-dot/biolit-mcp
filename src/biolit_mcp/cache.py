"""A small SQLite-backed TTL cache for API responses.

PubMed records and UniProt entries change rarely, and an agent often asks for the
same record several times in one session. Caching keeps us well under the NCBI
rate limit and makes repeated eval runs cheap and reproducible.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class TTLCache:
    def __init__(self, path: Path | None, clock=time.time) -> None:
        self._clock = clock
        if path is None:
            self._db = sqlite3.connect(":memory:")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(path))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cache "
            "(k TEXT PRIMARY KEY, v TEXT NOT NULL, exp REAL NOT NULL)"
        )
        self._db.commit()

    @staticmethod
    def make_key(url: str, params: dict[str, Any]) -> str:
        # Never let secrets become part of the key.
        clean = {k: v for k, v in sorted(params.items()) if k not in {"api_key", "email"}}
        raw = url + "?" + json.dumps(clean, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> Any | None:
        row = self._db.execute("SELECT v, exp FROM cache WHERE k = ?", (key,)).fetchone()
        if row is None:
            return None
        value, exp = row
        if exp < self._clock():
            self._db.execute("DELETE FROM cache WHERE k = ?", (key,))
            self._db.commit()
            return None
        return json.loads(value)

    def set(self, key: str, value: Any, ttl_s: float) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO cache (k, v, exp) VALUES (?, ?, ?)",
            (key, json.dumps(value), self._clock() + ttl_s),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
