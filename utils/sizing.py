"""Single source of position-size math (consolidates the 3 prior copies)."""
import config


def position_size(entry: float, stop: float, account_equity: float | None = None,
                  risk_pct: float | None = None) -> dict:
    equity = config.ACCOUNT_EQUITY if account_equity is None else account_equity
    risk_pct = config.RISK_PCT if risk_pct is None else risk_pct
    risk_usd = equity * risk_pct
    stop_dist = abs(float(entry) - float(stop))
    if stop_dist == 0:
        return {"risk_usd": round(risk_usd, 2), "size_usd": 0, "size_units": 0}
    size_units = risk_usd / stop_dist
    return {
        "risk_usd": round(risk_usd, 2),
        "size_usd": round(size_units * float(entry), 2),
        "size_units": round(size_units, 4),
    }
