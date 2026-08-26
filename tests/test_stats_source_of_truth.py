import sqlite3

import production_fix


class FakeBot:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")

    def get_db_connection(self):
        return self.conn

    def release_db_connection(self, conn):
        return None

    def is_postgres(self):
        return False


def setup_db(bot):
    cur = bot.conn.cursor()
    cur.execute(
        "CREATE TABLE trades ("
        "id INTEGER PRIMARY KEY, signal_type TEXT, outcome TEXT, trade_status TEXT, "
        "entry_price REAL, sl REAL, tp1 REAL, tp2 REAL)"
    )
    cur.execute(
        "CREATE TABLE runtime_decision_events ("
        "id INTEGER PRIMARY KEY, direction TEXT, final_decision TEXT)"
    )
    bot.conn.commit()


def test_stats_do_not_report_zero_when_trade_ledger_has_signal():
    bot = FakeBot()
    setup_db(bot)
    bot.conn.execute(
        "INSERT INTO trades(id, signal_type, outcome, trade_status, entry_price, sl, tp1, tp2) "
        "VALUES (1, '🔴 بيع مرن', NULL, 'OPEN', 4636.5, 4645.0, 4624.17, 4617.79)"
    )
    bot.conn.commit()

    report = production_fix._stats_message(bot)

    assert "الإشارات المسجلة فعلياً: 1" in report
    assert "BUY: 0" in report
    assert "SELL: 1" in report
    assert "الصفقات النشطة: 1" in report
    assert "أحداث تدقيق القرار: 0" in report
