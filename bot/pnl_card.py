"""
pnl_card.py — Generates a custom GEMBOT-branded PnL card when a token hits 2x.

Uses the diagonal card layout from degen-pnl-bot/pnl_card.py (1255x838):
left panel = mascot/art, right panel = stats with GEMBOT branding.

Usage:
    from bot.pnl_card import generate_pnl_card
  
    img_bytes = generate_pnl_card(
        token_symbol="PEPE",
        pnl_pct=142.5,
        entry_mcap=250_000,
        current_mcap=607_000,
        peak_mcap=700_000,
        duration="2h 14m",
        wallet="7xKX...9fQa",
        is_win=True,
    )
    # send via Telegram send_photo(photo=img_bytes)
"""

import io
import math
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W2, H2 = 1255, 838
split_x = int(W2 * 0.46)

# ── Color palette (GEMBOT dark theme) ────────────────────────────────
GREEN = (44, 224, 128)
RED = (255, 82, 82)
NEON_GREEN = (0, 255, 136, 180)
NEON_RED = (255, 59, 59, 180)
BG_TOP = (8, 10, 14)
BG_BOTTOM = (5, 6, 9)
CARD = (13, 17, 23)
MUTED = (140, 150, 160)
WHITE = (240, 242, 245)
GEMBOT_BLUE = (1, 131, 189)  # From the logo accent

# ── Fonts ─────────────────────────────────────────────────────────────
FONT_DIR = "/system/fonts/"
F_BOLD = FONT_DIR + "DroidSans-Bold.ttf"
F_REG = FONT_DIR + "DroidSans.ttf"
F_MONO = FONT_DIR + "DroidSansMono.ttf"


def _font(path, size):
    return ImageFont.truetype(path, size)


def _vgradient(draw, box, top_color, bottom_color):
    x0, y0, x1, y1 = box
    height = y1 - y0
    for i in range(height):
        t = i / height
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        draw.line([(x0, y0 + i), (x1, y0 + i)], fill=(r, g, b))


def _shade(img, box, base_color, light=(-0.35, -0.4), intensity=95):
    x0, y0, x1, y1 = box
    w, h = int(x1 - x0), int(y1 - y0)
    if w <= 1 or h <= 1:
        return
    patch = Image.new("RGB", (w, h), base_color)
    hi = tuple(min(255, c + intensity) for c in base_color)
    lo = tuple(max(0, c - intensity) for c in base_color)
    maxr = math.hypot(w, h) * 0.75
    grad_hi = Image.new("L", (w, h), 0)
    gd = ImageDraw.Draw(grad_hi)
    cx, cy = w / 2 + light[0] * w / 2, h / 2 + light[1] * h / 2
    for r in range(int(maxr), 0, -6):
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=int(255 * (1 - r / maxr)))
    grad_hi = grad_hi.filter(ImageFilter.GaussianBlur(w * 0.18 + 1))
    patch = Image.composite(Image.new("RGB", (w, h), hi), patch, grad_hi)
    grad_lo = Image.new("L", (w, h), 0)
    gd2 = ImageDraw.Draw(grad_lo)
    cx2, cy2 = w / 2 - light[0] * w / 2, h / 2 - light[1] * h / 2
    for r in range(int(maxr), 0, -6):
        gd2.ellipse([cx2 - r, cy2 - r, cx2 + r, cy2 + r], fill=int(185 * (1 - r / maxr)))
    grad_lo = grad_lo.filter(ImageFilter.GaussianBlur(w * 0.2 + 1))
    patch = Image.composite(Image.new("RGB", (w, h), lo), patch, grad_lo)
    spec = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(spec)
    scx, scy = w / 2 + light[0] * w * 0.55, h / 2 + light[1] * h * 0.55
    sr = min(w, h) * 0.16
    sd.ellipse([scx - sr, scy - sr, scx + sr, scy + sr], fill=200)
    spec = spec.filter(ImageFilter.GaussianBlur(sr * 0.6))
    patch = Image.composite(Image.new("RGB", (w, h), (255, 255, 255)), patch, spec)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, w - 1, h - 1], fill=255)
    img.paste(patch, (int(x0), int(y0)), mask)


def _shaded_circle(img, cx, cy, r, color, **kw):
    _shade(img, (cx - r, cy - r, cx + r, cy + r), color, **kw)


# ── Mascots (simplified versions for alert context) ──────────────────

def _draw_bull(img, draw, box, is_win):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx, cy = x0 + w / 2, y0 + h * 0.55
    color = (50, 130, 90) if is_win else (110, 90, 90)
    outline = (15, 20, 18)
    head_w, head_h = w * 0.6, h * 0.5
    _shade(img, (cx - head_w / 2, cy - head_h / 2, cx + head_w / 2, cy + head_h / 2), color)
    draw.ellipse([cx - head_w / 2, cy - head_h / 2, cx + head_w / 2, cy + head_h / 2], outline=outline, width=5)
    accent = GREEN if is_win else RED
    eye_y = cy - head_h * 0.08
    for sx in (cx - w * 0.15, cx + w * 0.15):
        if is_win:
            draw.ellipse([sx - w * 0.04, eye_y - w * 0.04, sx + w * 0.04, eye_y + w * 0.04], fill=(255, 255, 220))
            draw.ellipse([sx - w * 0.015, eye_y - w * 0.015, sx + w * 0.015, eye_y + w * 0.015], fill=(20, 20, 18))
        else:
            draw.arc([sx - w * 0.04, eye_y - w * 0.01, sx + w * 0.04, eye_y + w * 0.06], 180, 360, fill=accent, width=4)
    mouth_y = cy + head_h * 0.28
    if is_win:
        draw.arc([cx - w * 0.08, mouth_y - w * 0.02, cx + w * 0.08, mouth_y + w * 0.06], 10, 170, fill=outline, width=4)
    else:
        draw.arc([cx - w * 0.08, mouth_y + w * 0.02, cx + w * 0.08, mouth_y + w * 0.08], 190, 350, fill=outline, width=4)
    for sign in (-1, 1):
        ear_x = cx + sign * head_w * 0.4
        ear_y = cy - head_h * 0.4
        draw.ellipse([ear_x - w * 0.08, ear_y - w * 0.08, ear_x + w * 0.08, ear_y + w * 0.08], fill=color, outline=outline, width=3)
        horn_x = cx + sign * head_w * 0.3
        horn_y = cy - head_h * 0.52
        draw.polygon([(horn_x - w * 0.03, horn_y), (horn_x + w * 0.03, horn_y), (horn_x, horn_y - w * 0.1)], fill=(200, 200, 195), outline=outline, width=2)


def _draw_bear(img, draw, box, is_win):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx, cy = x0 + w / 2, y0 + h * 0.55
    color = (100, 80, 80) if not is_win else (130, 140, 135)
    outline = (20, 15, 15)
    head_w, head_h = w * 0.6, h * 0.5
    _shade(img, (cx - head_w / 2, cy - head_h / 2, cx + head_w / 2, cy + head_h / 2), color)
    draw.ellipse([cx - head_w / 2, cy - head_h / 2, cx + head_w / 2, cy + head_h / 2], outline=outline, width=5)
    for sx in (cx - w * 0.28, cx + w * 0.28):
        _shaded_circle(img, sx, cy - h * 0.4, w * 0.12, color)
        draw.ellipse([sx - w * 0.12, cy - h * 0.4 - w * 0.12, sx + w * 0.12, cy - h * 0.4 + w * 0.12], outline=outline, width=3)
    snout_w, snout_h = w * 0.3, h * 0.16
    snout_cy = cy + head_h * 0.25
    _shade(img, (cx - snout_w / 2, snout_cy - snout_h / 2, cx + snout_w / 2, snout_cy + snout_h / 2), (180, 165, 155))
    draw.ellipse([cx - snout_w / 2, snout_cy - snout_h / 2, cx + snout_w / 2, snout_cy + snout_h / 2], outline=outline, width=3)
    draw.ellipse([cx - w * 0.02, snout_cy - w * 0.02, cx + w * 0.02, snout_cy + w * 0.02], fill=outline)
    accent = GREEN if is_win else RED
    eye_y = cy - head_h * 0.08
    for sx in (cx - w * 0.14, cx + w * 0.14):
        if is_win:
            draw.arc([sx - w * 0.04, eye_y - w * 0.02, sx + w * 0.04, eye_y + w * 0.04], 200, 340, fill=outline, width=4)
        else:
            draw.line([(sx - w * 0.04, eye_y - w * 0.02), (sx + w * 0.04, eye_y + w * 0.02)], fill=accent, width=4)


def _draw_chart_whale(img, draw, box, is_win):
    """A whale mascot for big wins — chart whale."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx, cy = x0 + w / 2, y0 + h * 0.55
    color = (70, 130, 180) if is_win else (80, 85, 95)
    outline = (12, 18, 28)
    body_w, body_h = w * 0.7, h * 0.45
    _shade(img, (cx - body_w / 2, cy - body_h / 2, cx + body_w / 2, cy + body_h / 2), color)
    draw.ellipse([cx - body_w / 2, cy - body_h / 2, cx + body_w / 2, cy + body_h / 2], outline=outline, width=5)
    draw.polygon([(cx + body_w * 0.25, cy - body_h * 0.35), (cx + body_w * 0.4, cy - body_h * 0.7), (cx + body_w * 0.5, cy - body_h * 0.3)],
                 fill=color, outline=outline, width=3)
    accent = GREEN if is_win else RED
    eye_cx = cx - body_w * 0.2
    eye_cy = cy - body_h * 0.1
    if is_win:
        draw.ellipse([eye_cx - w * 0.03, eye_cy - w * 0.03, eye_cx + w * 0.03, eye_cy + w * 0.03], fill=(20, 20, 22))
        draw.ellipse([eye_cx - w * 0.012, eye_cy - w * 0.04, eye_cx + w * 0.012, eye_cy - w * 0.02], fill=(255, 255, 255))
    else:
        draw.line([(eye_cx - w * 0.03, eye_cy - w * 0.02), (eye_cx + w * 0.03, eye_cy + w * 0.02)], fill=accent, width=4)
    mouth_y = cy + body_h * 0.25
    if is_win:
        draw.arc([cx - body_w * 0.25, mouth_y - h * 0.04, cx + body_w * 0.05, mouth_y + h * 0.07], 10, 90, fill=outline, width=4)
        for i in range(3):
            sy = cy - body_h * 0.5 - i * h * 0.05
            r = w * 0.012 * (3 - i)
            draw.ellipse([cx + body_w * 0.3 - r, sy - r, cx + body_w * 0.3 + r, sy + r], fill=(180, 210, 235))
    else:
        draw.line([(cx - body_w * 0.18, mouth_y + h * 0.02), (cx + body_w * 0.02, mouth_y)], fill=outline, width=4)


MASCOTS = [_draw_bull, _draw_bear, _draw_chart_whale]


def _mascot_panel(size, is_win, seed=None, mascot=None):
    w, h = size
    layer = Image.new("RGB", size, (10, 12, 14))
    draw = ImageDraw.Draw(layer)
    accent = GREEN if is_win else RED
    base = (16, 38, 28) if is_win else (38, 16, 16)
    _vgradient(draw, (0, 0, w, h), base, (10, 12, 14))
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([w * 0.1, h * 0.12, w * 0.9, h * 0.88], fill=(*accent, 45))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    layer.paste(glow, (0, 0), glow)
    draw = ImageDraw.Draw(layer)
    rnd = random.Random(seed)
    fn = mascot if mascot is not None else rnd.choice(MASCOTS)
    mascot_box = (w * 0.08, h * 0.16, w * 0.92, h * 0.95)
    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    mbx0, mby0, mbx1, mby1 = mascot_box
    sd.ellipse([mbx0 + (mbx1 - mbx0) * 0.15, mby1 - h * 0.05,
                mbx1 - (mbx1 - mbx0) * 0.15, mby1 + h * 0.03], fill=(0, 0, 0, 130))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    layer.paste(shadow, (0, 0), shadow)
    draw = ImageDraw.Draw(layer)
    fn(layer, draw, mascot_box, is_win)
    return layer


def _sparkline_right(draw, box, is_win, seed=None):
    """Simple sparkline on the right panel showing price trend."""
    rnd = random.Random(seed)
    x0, y0, x1, y1 = box
    n = 30
    pts = []
    val = 0.5
    for i in range(n):
        drift = 0.04 if is_win else -0.03
        val += drift + rnd.uniform(-0.06, 0.06)
        val = max(0.02, min(0.98, val))
        pts.append(val)
    target = 0.92 if is_win else 0.08
    for i in range(n - 6, n):
        w_f = (i - (n - 6)) / 6
        pts[i] = pts[i] * (1 - w_f) + target * w_f
    color = GREEN if is_win else RED
    coords = []
    for i, v in enumerate(pts):
        x = x0 + (x1 - x0) * i / (n - 1)
        y = y1 - (y1 - y0) * v
        coords.append((x, y))
    draw.line(coords, fill=color, width=4, joint="curve")


def generate_pnl_card(
    token_symbol: str,
    pnl_pct: float,
    entry_mcap: float = None,
    current_mcap: float = None,
    peak_mcap: float = None,
    duration: str = None,
    wallet: str = None,
    telegram_username: str = None,
    is_win: bool = True,
    seed: int = None,
    logo_bytes: bytes = None,
) -> bytes:
    """
    Generate a GEMBOT-branded PnL card image (1255x838 PNG bytes).

    Args:
        token_symbol: Token symbol (e.g. "PEPE")
        pnl_pct: Percentage gain/loss (e.g. 142.5 for +142.5%)
        entry_mcap: Market cap at entry/alert time
        current_mcap: Current market cap
        peak_mcap: Peak market cap achieved
        duration: Time held (e.g. "2h 14m")
        wallet: Wallet address (shortened)
        telegram_username: @handle shown in footer
        is_win: True = profit (green), False = loss (red)
        seed: Random seed for reproducible mascot
        logo_bytes: PNG bytes of token logo to overlay as watermark

    Returns:
        PNG bytes ready to send via Telegram send_photo
    """
    accent = GREEN if is_win else RED

    img = Image.new("RGB", (W2, H2), BG_BOTTOM)
    draw = ImageDraw.Draw(img)

    # --- Left panel: mascot ---
    fn = None
    if is_win:
        fn = MASCOTS[0]  # bull for win
    else:
        fn = MASCOTS[1]  # bear for loss
    panel = _mascot_panel((split_x + 40, H2), is_win, seed=seed, mascot=fn)
    img.paste(panel, (0, 0))
    draw = ImageDraw.Draw(img)

    # --- Diagonal divider ---
    diag_offset = 90
    draw.polygon(
        [(split_x, 0), (split_x + diag_offset, 0),
         (split_x + diag_offset - int(H2 * 0.12), H2), (split_x - int(H2 * 0.12), H2)],
        fill=CARD,
    )
    draw.line([(split_x, 0), (split_x - int(H2 * 0.12), H2)], fill=(230, 230, 235), width=4)

    # --- Right panel content ---
    panel_x0 = split_x + diag_offset - int(H2 * 0.12) + 40
    right_edge = W2 - 60

    # GEMBOT brand header
    f_brand = _font(F_BOLD, 28)
    brand = "GEMBOT"
    bw = draw.textlength(brand, font=f_brand)
    draw.text((right_edge - bw, 30), brand, font=f_brand, fill=GEMBOT_BLUE)

    # Tagline
    f_tagline = _font(F_REG, 16)
    tagline = "stay ahead"
    tw = draw.textlength(tagline, font=f_tagline)
    draw.text((right_edge - tw, 68), tagline, font=f_tagline, fill=MUTED)

    # Token symbol
    f_symbol = _font(F_BOLD, 40)
    sym = f"${token_symbol.upper()}"
    sw = draw.textlength(sym, font=f_symbol)
    sym_x = panel_x0 + ((right_edge - panel_x0) - sw) / 2
    draw.text((sym_x, 170), sym, font=f_symbol, fill=WHITE)

    # Big PnL %
    f_big = _font(F_BOLD, 72)
    pct_txt = f"{'+' if is_win else ''}{pnl_pct:.1f}%"
    pw = draw.textlength(pct_txt, font=f_big)
    pct_x = panel_x0 + ((right_edge - panel_x0) - pw) / 2
    draw.text((pct_x, 240), pct_txt, font=f_big, fill=accent)

    # Sparkline
    spark_box = (panel_x0 + 20, 340, right_edge - 20, 410)
    _sparkline_right(draw, spark_box, is_win, seed=seed)

    # Stats grid
    f_label = _font(F_REG, 22)
    f_value = _font(F_BOLD, 30)

    stats = []
    if entry_mcap is not None:
        if entry_mcap >= 1_000_000:
            entry_str = f"${entry_mcap/1_000_000:.2f}M"
        elif entry_mcap >= 1_000:
            entry_str = f"${entry_mcap/1_000:.1f}K"
        else:
            entry_str = f"${entry_mcap:.0f}"
        stats.append(("ENTRY MCAP", entry_str))

    if current_mcap is not None:
        if current_mcap >= 1_000_000:
            cur_str = f"${current_mcap/1_000_000:.2f}M"
        elif current_mcap >= 1_000:
            cur_str = f"${current_mcap/1_000:.1f}K"
        else:
            cur_str = f"${current_mcap:.0f}"
        stats.append(("CURRENT MCAP", cur_str))

    if peak_mcap is not None:
        if peak_mcap >= 1_000_000:
            peak_str = f"${peak_mcap/1_000_000:.2f}M"
        elif peak_mcap >= 1_000:
            peak_str = f"${peak_mcap/1_000:.1f}K"
        else:
            peak_str = f"${peak_mcap:.0f}"
        stats.append(("PEAK MCAP", peak_str))

    stat_start_y = 450
    for i, (label, value) in enumerate(stats):
        row_y = stat_start_y + i * 70
        draw.text((panel_x0 + 20, row_y), label, font=f_label, fill=MUTED)
        draw.text((panel_x0 + 20, row_y + 30), value, font=f_value, fill=WHITE)

    # Duration
    if duration:
        f_dur = _font(F_REG, 22)
        dur_txt = f"\u23f1  {duration}"
        draw.text((panel_x0 + 20, H2 - 140), dur_txt, font=f_dur, fill=MUTED)

    # Footer: wallet + telegram handle
    f_foot = _font(F_MONO, 22)
    if telegram_username:
        foot_txt = f"@{telegram_username.lstrip('@')}"
        draw.text((panel_x0 + 20, H2 - 80), foot_txt, font=_font(F_BOLD, 28), fill=WHITE)

    if wallet:
        draw.text((panel_x0 + 20, H2 - 48), wallet, font=_font(F_MONO, 20), fill=MUTED)

    # Token watermark
    if logo_bytes:
        try:
            wm = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
            wm_size = min(W2, H2) // 3
            wm = wm.resize((wm_size, wm_size))
            r, g, b, a = wm.split()
            a = a.point(lambda x: min(x, 50))
            wm = Image.merge("RGBA", (r, g, b, a))
            wx = (W2 - wm_size) // 2
            wy = (H2 - wm_size) // 2
            img.paste(wm, (wx, wy), wm)
        except:
            pass

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()