"""Final execution gate.

Combines data health and Trade Lawyer without replacing the signal engine.
The caller keeps generating signals as before, then asks this gate whether
execution is allowed.
"""

from dataclasses import asdict

from trade_lawyer import review_trade

try:
    from twelve_data_health import DataHealthState
except Exception:
    DataHealthState = None


BLOCKED_LIVE_STATES = {
    "LIVE_STALE",
    "QUOTA_EXHAUSTED",
    "DATA_UNAVAILABLE",
    "TRADING_BLOCKED",
}


def evaluate_execution(signal: dict, context: dict | None = None) -> dict:
    context = context or {}
    health = str(context.get("data_state", "")).upper()

    if health in BLOCKED_LIVE_STATES and not context.get("analysis_only"):
        return {
            "allowed": False,
            "decision": "REJECT",
            "reason": "Live market data unavailable",
            "health": health,
        }

    lawyer = review_trade(signal, context)
    result = asdict(lawyer)

    result["allowed"] = lawyer.decision != "REJECT"
    result["execution_decision"] = lawyer.decision
    return result


def arabic_health_message(snapshot: dict) -> str:
    return (
        "📡 حالة السوق\n"
        f"• Twelve Data: {snapshot.get('provider', 'Twelve Data')}\n"
        f"• الحالة: {snapshot.get('state', 'غير معروف')}\n"
        f"• عمر السعر: {snapshot.get('quote_age', 'N/A')}\n"
        f"• عمر M15: {snapshot.get('m15_age', 'N/A')}\n"
        f"• عمر H1: {snapshot.get('h1_age', 'N/A')}\n"
        f"• التداول: {'مسموح' if snapshot.get('trading_allowed') else 'متوقف'}"
    )
