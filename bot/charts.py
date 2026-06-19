import io
import time
import asyncio
from datetime import datetime, timezone

import pandas as pd
import config

from integrations.hyperliquid import get_candles

# Heavy chart deps are optional. If unavailable (or charts disabled), the system
# runs chart-less and photo alerts fall back to text.
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import mplfinance as mpf
    from bot.branding import add_chart_header
    _CHARTS_AVAILABLE = True
except Exception:
    _CHARTS_AVAILABLE = False


LONG_COLOR  = "#26a69a"
SHORT_COLOR = "#ef5350"
BG_COLOR    = "#0a0e17"   # Deep Navy — HL Intel Design Packet
GRID_COLOR  = "#121826"   # Dark Panel
TEXT_COLOR  = "#848e9c"
UP_COLOR    = "#26a69a"
DOWN_COLOR  = "#ef5350"


def _build_style() -> object:
    mc = mpf.make_marketcolors(
        up=UP_COLOR, down=DOWN_COLOR,
        edge="inherit", wick="inherit",
        volume={"up": UP_COLOR + "55", "down": DOWN_COLOR + "55"},
    )
    return mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=mc,
        gridstyle="-",
        gridcolor=GRID_COLOR,
        gridaxis="both",
        y_on_right=True,
        facecolor=BG_COLOR,
        figcolor=BG_COLOR,
        rc={
            "axes.labelcolor": TEXT_COLOR,
            "axes.edgecolor": GRID_COLOR,
            "xtick.color": TEXT_COLOR,
            "ytick.color": TEXT_COLOR,
            "text.color": "#eaecef",
            "font.family": "monospace",
            "font.size": 9,
        },
    )


CURRENT_PX_COLOR = "#f0b90b"  # yellow — distinct from entry lines


async def generate_confluence_chart(
    coin: str,
    side: str,
    whales: list[dict],
    current_px: float = 0.0,
) -> bytes | None:
    if not config.ENABLE_CHARTS or not _CHARTS_AVAILABLE:
        return None
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - (30 * 24 * 60 * 60 * 1000)

    try:
        candles = await get_candles(coin, "4h", start_ms, end_ms)
    except Exception:
        return None

    if not candles:
        return None

    df = pd.DataFrame([{
        "Open":   float(c["o"]),
        "High":   float(c["h"]),
        "Low":    float(c["l"]),
        "Close":  float(c["c"]),
        "Volume": float(c["v"]),
    } for c in candles])
    df.index = pd.to_datetime([c["t"] for c in candles], unit="ms", utc=True)
    df.index = df.index.tz_localize(None)

    direction    = "LONG" if side == "long" else "SHORT"
    entry_color  = LONG_COLOR if side == "long" else SHORT_COLOR
    whale_count  = len(whales)
    total_notional = sum(w["notional"] for w in whales)

    # Entry price lines — only whales that have a valid entry price
    valid_whales = [w for w in whales if w.get("entry_px", 0) > 0]
    hline_prices = [w["entry_px"] for w in valid_whales]

    hlines_cfg = {}
    if hline_prices:
        hlines_cfg = dict(
            hlines=hline_prices,
            colors=[entry_color] * len(hline_prices),
            linewidths=1.2,
            linestyle="--",
            alpha=0.85,
        )

    style = _build_style()

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        volume=True,
        hlines=hlines_cfg if hlines_cfg else dict(hlines=[]),
        returnfig=True,
        figsize=(14, 8),
        tight_layout=True,
        datetime_format="%b %d",
        xrotation=0,
    )

    ax_main = axes[0]

    # Entry price labels
    price_range = df["High"].max() - df["Low"].min()
    offset      = price_range * 0.005

    for whale in sorted(valid_whales, key=lambda x: x["rank"]):
        label = f"#{whale['rank']}  ${whale['notional'] / 1e6:.1f}M"
        ax_main.axhline(y=whale["entry_px"], color=entry_color, linewidth=0)
        ax_main.text(
            0.99, whale["entry_px"] + offset, label,
            transform=ax_main.get_yaxis_transform(),
            color=entry_color, fontsize=8, fontfamily="monospace",
            va="bottom", ha="right",
        )

    # Current price line
    if current_px > 0:
        ax_main.axhline(y=current_px, color=CURRENT_PX_COLOR, linewidth=1.0, linestyle="-", alpha=0.9)
        ax_main.text(
            0.99, current_px + offset, f"NOW  ${current_px:,.2f}",
            transform=ax_main.get_yaxis_transform(),
            color=CURRENT_PX_COLOR, fontsize=8, fontweight="bold",
            fontfamily="monospace", va="bottom", ha="right",
        )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=BG_COLOR)
    plt.close(fig)
    buf.seek(0)

    subtitle = f"{whale_count} wallets  |  ${total_notional:,.0f}"
    return add_chart_header(buf.getvalue(), coin=coin, side=side,
                            alert_type="CONFLUENCE", subtitle=subtitle)


async def generate_whale_chart(
    coin: str,
    side: str,
    rank: int,
    notional: float,
    entry_px: float,
    account_value: float,
    day_pnl: float,
    address: str,
    current_px: float = 0.0,
) -> bytes | None:
    if not config.ENABLE_CHARTS or not _CHARTS_AVAILABLE:
        return None
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - (30 * 24 * 60 * 60 * 1000)

    try:
        candles = await get_candles(coin, "4h", start_ms, end_ms)
    except Exception:
        return None

    if not candles:
        return None

    df = pd.DataFrame([{
        "Open":   float(c["o"]),
        "High":   float(c["h"]),
        "Low":    float(c["l"]),
        "Close":  float(c["c"]),
        "Volume": float(c["v"]),
    } for c in candles])
    df.index = pd.to_datetime([c["t"] for c in candles], unit="ms", utc=True)
    df.index = df.index.tz_localize(None)

    direction   = "LONG" if side == "long" else "SHORT"
    entry_color = LONG_COLOR if side == "long" else SHORT_COLOR
    pnl_str     = f"+${day_pnl:,.0f}" if day_pnl >= 0 else f"-${abs(day_pnl):,.0f}"

    hlines_cfg = {}
    if entry_px > 0:
        hlines_cfg = dict(
            hlines=[entry_px],
            colors=[entry_color],
            linewidths=1.2,
            linestyle="--",
            alpha=0.85,
        )

    style = _build_style()

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        volume=True,
        hlines=hlines_cfg if hlines_cfg else dict(hlines=[]),
        returnfig=True,
        figsize=(14, 8),
        tight_layout=True,
        datetime_format="%b %d",
        xrotation=0,
    )

    ax_main = axes[0]

    price_range = df["High"].max() - df["Low"].min()
    offset      = price_range * 0.005

    if entry_px > 0:
        ax_main.text(
            0.99, entry_px + offset, f"#{rank} entry  ${entry_px:,.2f}",
            transform=ax_main.get_yaxis_transform(),
            color=entry_color, fontsize=8, fontfamily="monospace",
            va="bottom", ha="right",
        )

    if current_px > 0:
        ax_main.axhline(y=current_px, color=CURRENT_PX_COLOR, linewidth=1.0, linestyle="-", alpha=0.9)
        ax_main.text(
            0.99, current_px + offset, f"NOW  ${current_px:,.2f}",
            transform=ax_main.get_yaxis_transform(),
            color=CURRENT_PX_COLOR, fontsize=8, fontweight="bold",
            fontfamily="monospace", va="bottom", ha="right",
        )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=BG_COLOR)
    plt.close(fig)
    buf.seek(0)

    subtitle = f"Rank #{rank}  |  ${notional:,.0f}  |  Day PnL: {pnl_str}"
    return add_chart_header(buf.getvalue(), coin=coin, side=side,
                            alert_type="WHALE MOVE", subtitle=subtitle)
