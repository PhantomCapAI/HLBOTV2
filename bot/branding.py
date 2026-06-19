"""
HL Intel — 32-bit retro-futurist branding layer.

Composites a styled header bar onto chart images using Pillow.
Palette sourced from HL_Intel_Design_Packet.docx.
"""

import io
import math
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Official palette — HL_Intel_Design_Packet.docx
# ---------------------------------------------------------------------------
BG_DARK     = (10, 14, 23, 255)     # Deep Navy    #0A0E17
HEADER_BG   = (18, 24, 38, 255)     # Dark Panel   #121826
CYAN        = (0, 245, 255, 255)    # Cyan         #00F5FF
CYAN_MID    = (0, 160, 180, 160)
CYAN_DIM    = (0, 80, 100, 80)
MAGENTA     = (255, 45, 158, 255)   # Signal Mag   #FF2D9E
VIOLET      = (123, 97, 255, 255)   # Sec Violet   #7B61FF
MAGENTA_DIM = (255, 0, 120, 120)
WHITE_COOL  = (200, 240, 255, 255)
TEXT_DIM    = (120, 180, 200, 200)

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
_FONT_PATHS = [
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "/System/Library/Fonts/Supplemental/Andale Mono.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Radar logo mark
# ---------------------------------------------------------------------------

def _draw_glow(draw: ImageDraw.ImageDraw, cx: int, cy: int,
               r: int, color_rgb: tuple, layers: int = 4) -> None:
    """Draw a soft glow ring by stacking semi-transparent ellipses."""
    for i in range(layers):
        gr = r + (layers - i) * 2
        alpha = 30 - i * 6
        if alpha <= 0:
            continue
        draw.ellipse(
            [cx - gr, cy - gr, cx + gr, cy + gr],
            outline=(*color_rgb, alpha),
            width=1,
        )


def generate_logo(size: int = 200) -> Image.Image:
    """
    Generate the HL Intel radar logo mark as a transparent RGBA PNG.
    Designed to read clearly at both large and small (thumbnail) sizes.

    size: pixel dimension of the square canvas
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx, cy = size // 2, size // 2
    max_r  = int(size * 0.44)
    lw     = max(1, size // 120)  # scale line widths with size

    # --- background circle ---
    draw.ellipse(
        [cx - max_r, cy - max_r, cx + max_r, cy + max_r],
        fill=(1, 11, 19, 255),
    )

    # --- concentric rings — only 2, more readable at small sizes ---
    for frac, alpha in [(0.5, 50), (1.0, 100)]:
        r = int(max_r * frac)
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            outline=(0, 220, 255, alpha),
            width=lw,
        )

    # --- crosshair lines ---
    for dx, dy in [(1, 0), (0, 1)]:
        draw.line(
            [cx - dx * max_r, cy - dy * max_r, cx + dx * max_r, cy + dy * max_r],
            fill=(0, 220, 255, 45),
            width=lw,
        )

    # --- sweep trail ---
    sweep_deg  = 310
    trail_span = 80
    arc_bbox   = [cx - max_r + lw + 1, cy - max_r + lw + 1,
                  cx + max_r - lw - 1, cy + max_r - lw - 1]
    steps = 40
    for i in range(steps):
        t       = i / steps
        start_a = sweep_deg - trail_span + int(t * trail_span)
        end_a   = start_a + (trail_span // steps) + 1
        alpha   = int(180 * t)
        w       = lw + 1 if t > 0.7 else lw
        draw.arc(arc_bbox, start=start_a, end=end_a,
                 fill=(0, 220, 255, alpha), width=w)

    # --- sweep arm — thicker for visibility at small size ---
    arm_rad = math.radians(sweep_deg)
    arm_x   = cx + int(max_r * 0.88 * math.cos(arm_rad))
    arm_y   = cy + int(max_r * 0.88 * math.sin(arm_rad))
    draw.line([cx, cy, arm_x, arm_y], fill=(0, 240, 255, 255), width=lw * 2 + 1)

    # --- target blip — larger, more prominent ---
    blip_deg  = 38
    blip_dist = max_r * 0.52
    blip_rad  = math.radians(blip_deg)
    bx = cx + int(blip_dist * math.cos(blip_rad))
    by = cy + int(blip_dist * math.sin(blip_rad))

    blip_r = max(4, size // 28)
    _draw_glow(draw, bx, by, blip_r + 2, (255, 0, 120), layers=6)
    draw.ellipse([bx - blip_r, by - blip_r, bx + blip_r, by + blip_r],
                 fill=(255, 0, 120, 240))
    draw.ellipse([bx - blip_r // 2, by - blip_r // 2,
                  bx + blip_r // 2, by + blip_r // 2],
                 fill=(255, 160, 200, 255))
    draw.ellipse([bx - 2, by - 2, bx + 2, by + 2], fill=(255, 150, 190, 255))

    # --- "HL" monogram — readable at thumbnail size ---
    font_sz  = max(10, int(size * 0.22))
    mono_fnt = _load_font(font_sz)
    # Measure text for centering
    try:
        tw = int(draw.textlength("HL", font=mono_fnt))
    except Exception:
        tw = font_sz * 2
    th = font_sz
    # Shadow pass — offset dark text for depth
    for ox, oy in [(-1, 1), (1, 1), (0, 2)]:
        draw.text(
            (cx - tw // 2 + ox, cy - th // 2 + oy),
            "HL",
            fill=(0, 0, 0, 180),
            font=mono_fnt,
        )
    # Main text
    draw.text(
        (cx - tw // 2, cy - th // 2 - 1),
        "HL",
        fill=(0, 240, 255, 255),
        font=mono_fnt,
    )

    # --- outer ring border (bright, thicker) ---
    draw.ellipse(
        [cx - max_r, cy - max_r, cx + max_r, cy + max_r],
        outline=(0, 220, 255, 200),
        width=lw * 2,
    )
    # Glow ring
    draw.ellipse(
        [cx - max_r - 1, cy - max_r - 1, cx + max_r + 1, cy + max_r + 1],
        outline=(0, 220, 255, 60),
        width=1,
    )

    return img


# ---------------------------------------------------------------------------
# Chart header compositor
# ---------------------------------------------------------------------------

_HEADER_H = 72


def add_chart_header(
    chart_bytes: bytes,
    coin: str,
    side: str,
    alert_type: str,       # e.g. "CONFLUENCE" or "WHALE MOVE"
    subtitle: str = "",    # e.g. "3 wallets  |  $45,000,000"
) -> bytes:
    """
    Composite a Tron-style header bar onto a chart PNG.

    Returns the modified image as PNG bytes.
    """
    chart_img = Image.open(io.BytesIO(chart_bytes)).convert("RGBA")
    w, h = chart_img.size

    # --- header canvas ---
    header = Image.new("RGBA", (w, _HEADER_H), HEADER_BG)
    draw   = ImageDraw.Draw(header)

    # Subtle scanline texture — every other row slightly lighter
    for y in range(0, _HEADER_H, 2):
        draw.line([0, y, w, y], fill=(0, 30, 50, 30), width=1)

    # Bottom border — double cyan line for depth
    draw.line([0, _HEADER_H - 1, w, _HEADER_H - 1], fill=(0, 220, 255, 200), width=1)
    draw.line([0, _HEADER_H - 3, w, _HEADER_H - 3], fill=(0, 220, 255, 40),  width=1)

    # Left accent bar
    draw.rectangle([0, 0, 3, _HEADER_H], fill=(0, 220, 255, 180))

    # --- radar logo ---
    logo = generate_logo(size=56)
    header.paste(logo, (12, (_HEADER_H - 56) // 2), logo)

    # --- fonts ---
    font_brand    = _load_font(20)
    font_coin     = _load_font(13)
    font_alert    = _load_font(17)
    font_sub      = _load_font(11)

    # "HL INTEL" — cyan, left of center
    draw.text((76, 10), "HL INTEL", fill=CYAN, font=font_brand)

    # Coin + direction under branding
    direction = "LONG" if side == "long" else "SHORT"
    dir_color = (0, 220, 160, 220) if side == "long" else (255, 80, 80, 220)
    coin_line = f"{coin}-PERP  |  {direction}"
    draw.text((78, 36), coin_line, fill=dir_color, font=font_coin)

    # Optional subtitle (wallet count, combined notional)
    if subtitle:
        draw.text((78, 53), subtitle, fill=TEXT_DIM, font=font_sub)

    # Alert type — magenta, right-aligned
    try:
        alert_w = int(draw.textlength(alert_type, font=font_alert))
    except Exception:
        alert_w = len(alert_type) * 11
    draw.text((w - alert_w - 16, 24), alert_type, fill=MAGENTA, font=font_alert)

    # --- composite ---
    final = Image.new("RGBA", (w, h + _HEADER_H), BG_DARK)
    final.paste(header, (0, 0))
    final.paste(chart_img, (0, _HEADER_H))

    buf = io.BytesIO()
    final.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()
