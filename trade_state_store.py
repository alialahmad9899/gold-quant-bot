"""Persistent storage helpers for Active Trade Intelligence."""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock


class TradeStateStore:
    """Small JSON state layer for lifecycle state across process restarts."""

    def __init__(self, path: str | None = None):
        self.path = Path(path or os.getenv("TRADE_STATE_FILE", "trade_state.json"))
        self.lock = Lock()

    def save(self, payload: dict) -> None:
        serialized = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(serialized, encoding="utf-8")
            tmp.replace(self.path)

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return {}
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                return {}
            return payload if isinstance(payload, dict) else {}

    def clear(self) -> None:
        with self.lock:
            for candidate in (self.path, self.path.with_suffix(self.path.suffix + ".tmp")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
