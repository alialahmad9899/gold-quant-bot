from pathlib import Path

text = Path("bot.py").read_text(encoding="utf-8")

assert "open_val" not in text, "undefined backtest variable open_val remains"
assert "WHERE outcome IS NULL AND trade_status IN ('OPEN', 'TP1_HIT')" in text, "stats must count open lifecycle trades"
assert '"provider":"legacy provider"' not in text, "legacy provider label remains"
assert '"provider":"Yahoo Finance"' in text, "reset state must keep canonical provider"

print("REPO_REGRESSIONS_OK")
