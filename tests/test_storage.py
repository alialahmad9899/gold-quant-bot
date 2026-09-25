import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import Storage


def test_supabase_pooler_username_normalization(monkeypatch):
    monkeypatch.setenv("SUPABASE_PROJECT_REF", "demo123")
    raw = "postgresql://postgres:secret@aws-1-eu-west-1.pooler.supabase.com:5432/postgres"
    normalized = Storage._effective_database_url(raw)
    assert "postgres.demo123:secret@" in normalized
    assert normalized.startswith("postgresql://postgres.demo123:secret@")


def test_storage_falls_back_to_sqlite_when_postgres_unavailable(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_STRICT", raising=False)
    obj = Storage.__new__(Storage)
    obj.database_url = "postgresql://postgres:secret@pooler.example.com:5432/postgres"
    obj.sqlite_path = str(tmp_path / "radar.db")
    obj.pg = True
    obj.database_error = None
    obj._lock = __import__("threading").RLock()

    def fail():
        raise RuntimeError("temporary postgres outage")

    def init_sqlite():
        import sqlite3
        with obj._lock, sqlite3.connect(obj.sqlite_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS radar_candidates (token_key TEXT PRIMARY KEY, chain TEXT NOT NULL, address TEXT NOT NULL, symbol TEXT, name TEXT, score REAL, security_status TEXT, data_completeness REAL, snapshot TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS radar_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, token_key TEXT NOT NULL, score REAL NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL)")

    obj._init_db_once = fail
    monkeypatch.setattr(obj, "_init_db_once", lambda: init_sqlite() if obj.pg is False else fail())

    Storage._init_db(obj)

    assert obj.pg is False
    assert obj.database_error == "temporary postgres outage"
