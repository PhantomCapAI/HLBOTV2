def fmt(n) -> str:
    """Trim trailing zeros: 0.46933000 -> 0.46933."""
    try:
        return f"{float(n):,.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(n)


def fmt_price(p) -> str:
    """Human price with sane precision (never scientific, e.g. no 6.028e+04).

    >=1000 -> thousands, no decimals; >=1 -> 2dp; smaller -> more dp, trimmed.
    """
    try:
        p = float(p)
    except (TypeError, ValueError):
        return str(p)
    ap = abs(p)
    if ap >= 1000:
        return f"{p:,.0f}"
    if ap >= 1:
        return f"{p:,.2f}"
    if ap >= 0.01:
        return f"{p:.4f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")
