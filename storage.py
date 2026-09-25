from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """Small persistence layer: PostgreSQL in production, SQLite fallback locally."""

    def __init__(self, database_url: str | None = None, sqlite_path: str = "radar.db"):
        self.database_url = self._effective_database_url((database_url or "").strip())
        self.sqlite_path = sqlite_path
        self.pg = self.database_url.lower().startswith(("postgres://", "postgresql://"))
        self._lock = threading.RLock()
        self._init_db()

    @staticmethod
    def _effective_database_url(raw: str) -> str:
        ref = os.getenv("SUPABASE_PROJECT_REF", "").strip()
        if not raw or not ref:
            return raw
        try:
            parsed = urlsplit(raw)
            host = (parsed.hostname or "").lower()
            user = parsed.username or ""
            if host.endswith(".pooler.supabase.com") and user == "postgres":
                marker = "://postgres"
                if marker in raw:
                    return raw.replace(marker, f"://postgres.{ref}", 1)
        except Exception:
            pass
        return raw
    def _pg_connect(self):
        import psycopg2
        url = self.database_url
        if "sslmode=" not in url.lower():
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)

    def _connect(self):
        if self.pg:
            return self._pg_connect()
        conn = sqlite3.connect(self.sqlite_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock, self._connect() as conn:
            if self.pg:
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS radar_candidates (
                        token_key TEXT PRIMARY KEY,
                        chain TEXT NOT NULL,
                        address TEXT NOT NULL,
                        symbol TEXT,
                        name TEXT,
                        score DOUBLE PRECISION,
                        security_status TEXT,
                        data_completeness DOUBLE PRECISION,
                        snapshot JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS radar_alerts (
                        id BIGSERIAL PRIMARY KEY,
                        token_key TEXT NOT NULL,
                        score DOUBLE PRECISION NOT NULL,
                        snapshot JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                conn.commit()
            else:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS radar_candidates (
                        token_key TEXT PRIMARY KEY,
                        chain TEXT NOT NULL,
                        address TEXT NOT NULL,
                        symbol TEXT,
                        name TEXT,
                        score REAL,
                        security_status TEXT,
                        data_completeness REAL,
                        snapshot TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS radar_alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_key TEXT NOT NULL,
                        score REAL NOT NULL,
                        snapshot TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.commit()

    def save_candidates(self, candidates: list[dict[str, Any]]) -> None:
        with self._lock, self._connect() as conn:
            now = _utc_now()
            for c in candidates:
                key = c["token_key"]
                snap = json.dumps(c, ensure_ascii=False, default=str)
                if self.pg:
                    conn.cursor().execute(
                        """
                        INSERT INTO radar_candidates
                        (token_key, chain, address, symbol, name, score, security_status,
                         data_completeness, snapshot, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (token_key) DO UPDATE SET
                          chain=EXCLUDED.chain, address=EXCLUDED.address,
                          symbol=EXCLUDED.symbol, name=EXCLUDED.name,
                          score=EXCLUDED.score, security_status=EXCLUDED.security_status,
                          data_completeness=EXCLUDED.data_completeness,
                          snapshot=EXCLUDED.snapshot, updated_at=EXCLUDED.updated_at
                        """,
                        (
                            key, c["chain"], c["address"], c.get("symbol"),
                            c.get("name"), float(c.get("score", 0)),
                            c.get("security_status", "UNKNOWN"),
                            float(c.get("data_completeness", 0)),
                            snap, now,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO radar_candidates
                        (token_key, chain, address, symbol, name, score, security_status,
                         data_completeness, snapshot, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(token_key) DO UPDATE SET
                          chain=excluded.chain, address=excluded.address,
                          symbol=excluded.symbol, name=excluded.name,
                          score=excluded.score, security_status=excluded.security_status,
                          data_completeness=excluded.data_completeness,
                          snapshot=excluded.snapshot, updated_at=excluded.updated_at
                        """,
                        (
                            key, c["chain"], c["address"], c.get("symbol"),
                            c.get("name"), float(c.get("score", 0)),
                            c.get("security_status", "UNKNOWN"),
                            float(c.get("data_completeness", 0)),
                            snap, now,
                        ),
                    )
            conn.commit()

    def alert_allowed(self, token_key: str, cooldown_seconds: int) -> bool:
        with self._lock, self._connect() as conn:
            if self.pg:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT created_at FROM radar_alerts
                    WHERE token_key=%s ORDER BY created_at DESC LIMIT 1
                    """,
                    (token_key,),
                )
                row = cur.fetchone()
                if not row:
                    return True
                created = row[0]
            else:
                row = conn.execute(
                    """
                    SELECT created_at FROM radar_alerts
                    WHERE token_key=? ORDER BY created_at DESC LIMIT 1
                    """,
                    (token_key,),
                ).fetchone()
                if not row:
                    return True
                created = row["created_at"]

            if isinstance(created, str):
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                created_dt = created
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - created_dt).total_seconds() >= cooldown_seconds

    def record_alert(self, candidate: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            snap = json.dumps(candidate, ensure_ascii=False, default=str)
            now = _utc_now()
            if self.pg:
                conn.cursor().execute(
                    "INSERT INTO radar_alerts(token_key,score,snapshot,created_at) VALUES (%s,%s,%s,%s)",
                    (candidate["token_key"], float(candidate["score"]), snap, now),
                )
            else:
                conn.execute(
                    "INSERT INTO radar_alerts(token_key,score,snapshot,created_at) VALUES (?,?,?,?)",
                    (candidate["token_key"], float(candidate["score"]), snap, now),
                )
            conn.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            if self.pg:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM radar_candidates")
                candidates = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM radar_alerts")
                alerts = cur.fetchone()[0]
            else:
                candidates = conn.execute("SELECT COUNT(*) FROM radar_candidates").fetchone()[0]
                alerts = conn.execute("SELECT COUNT(*) FROM radar_alerts").fetchone()[0]
            return {"saved_candidates": int(candidates), "alerts": int(alerts), "backend": "postgresql" if self.pg else "sqlite"}