def fmt(n) -> str:
    """Trim trailing zeros: 0.46933000 -> 0.46933."""
    try:
        return f"{float(n):,.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(n)
