"""Persistent storage helpers for Active Trade Intelligence.

Small JSON based state layer. Keeps lifecycle state across restarts without
forcing a database migration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock


class TradeStateStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.getenv("TRADE_STATE_FILE", "trade_state.json"))
        self.lock = Lock()

    def save(self, payload: dict):
        with self.lock:
            self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return {}
            return json.loads(self.path.read_text(encoding="utf-8"))

    def clear(self):
        with self.lock:
            if self.path.exists():
                self.path.unlink()
