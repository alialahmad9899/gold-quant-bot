"""Durable storage helpers for Active Trade Intelligence.

PostgreSQL is preferred when DATABASE_URL is configured. The JSON file remains a
local-development fallback so the lifecycle API stays backward compatible.
"""
from __future__ import annotations

import json, os, sqlite3
from pathlib import Path
from threading import Lock

try:
    import psycopg2
except Exception:  # pragma: no cover
    psycopg2 = None


class TradeStateStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.getenv("TRADE_STATE_FILE", "trade_state.json"))
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        self.lock = Lock()
        self._ensure_database()

    def _ensure_database(self) -> None:
        if not self.database_url: return
        conn = None
        try:
            if self.database_url.startswith(("postgres://", "postgresql://")) and psycopg2:
                conn = psycopg2.connect(self.database_url)
                cur = conn.cursor(); cur.execute("CREATE TABLE IF NOT EXISTS trade_runtime_state (state_key TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"); conn.commit()
            elif self.database_url.startswith("sqlite:///"):
                db = self.database_url.removeprefix("sqlite:///"); conn = sqlite3.connect(db); conn.execute("CREATE TABLE IF NOT EXISTS trade_runtime_state (state_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"); conn.commit()
        except Exception:
            try:
                if conn: conn.rollback()
            except Exception: pass
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass

    def save(self, payload: dict) -> None:
        serialized = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        with self.lock:
            if self.database_url and self._db_save(payload, serialized): return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(serialized, encoding="utf-8"); tmp.replace(self.path)

    def _db_save(self, payload: dict, serialized: str) -> bool:
        conn = None
        try:
            if self.database_url.startswith(("postgres://", "postgresql://")) and psycopg2:
                conn = psycopg2.connect(self.database_url); cur = conn.cursor(); cur.execute("INSERT INTO trade_runtime_state(state_key,payload,updated_at) VALUES (%s,%s,CURRENT_TIMESTAMP) ON CONFLICT(state_key) DO UPDATE SET payload=EXCLUDED.payload, updated_at=CURRENT_TIMESTAMP", ("active_trade", json.dumps(payload, ensure_ascii=False, default=str))); conn.commit(); return True
            if self.database_url.startswith("sqlite:///"):
                db = self.database_url.removeprefix("sqlite:///"); conn = sqlite3.connect(db); conn.execute("INSERT INTO trade_runtime_state(state_key,payload,updated_at) VALUES (?,?,CURRENT_TIMESTAMP) ON CONFLICT(state_key) DO UPDATE SET payload=excluded.payload, updated_at=CURRENT_TIMESTAMP", ("active_trade", serialized)); conn.commit(); return True
        except Exception:
            try:
                if conn: conn.rollback()
            except Exception: pass
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass
        return False

    def load(self) -> dict:
        with self.lock:
            if self.database_url:
                payload = self._db_load()
                if payload is not None: return payload
            if not self.path.exists(): return {}
            try: payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError): return {}
            return payload if isinstance(payload, dict) else {}

    def _db_load(self) -> dict | None:
        conn = None
        try:
            if self.database_url.startswith(("postgres://", "postgresql://")) and psycopg2:
                conn = psycopg2.connect(self.database_url); cur = conn.cursor(); cur.execute("SELECT payload FROM trade_runtime_state WHERE state_key=%s", ("active_trade",)); row = cur.fetchone()
                return dict(row[0]) if row and isinstance(row[0], dict) else json.loads(row[0]) if row else {}
            if self.database_url.startswith("sqlite:///"):
                db = self.database_url.removeprefix("sqlite:///"); conn = sqlite3.connect(db); row = conn.execute("SELECT payload FROM trade_runtime_state WHERE state_key=?", ("active_trade",)).fetchone(); return json.loads(row[0]) if row else {}
        except Exception: return None
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass
        return None

    def clear(self) -> None:
        with self.lock:
            conn = None
            try:
                if self.database_url.startswith(("postgres://", "postgresql://")) and psycopg2:
                    conn = psycopg2.connect(self.database_url); conn.cursor().execute("DELETE FROM trade_runtime_state WHERE state_key=%s", ("active_trade",)); conn.commit()
                elif self.database_url.startswith("sqlite:///"):
                    db = self.database_url.removeprefix("sqlite:///"); conn = sqlite3.connect(db); conn.execute("DELETE FROM trade_runtime_state WHERE state_key=?", ("active_trade",)); conn.commit()
            except Exception: pass
            finally:
                if conn is not None:
                    try: conn.close()
                    except Exception: pass
            for candidate in (self.path, self.path.with_suffix(self.path.suffix + ".tmp")):
                try: candidate.unlink()
                except FileNotFoundError: pass
