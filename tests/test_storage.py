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
