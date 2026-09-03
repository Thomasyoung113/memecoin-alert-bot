"""
pnl_card.py — GEMBOT PnL / milestone card generator.

Design follows the researched spec for bot PnL cards (Trady / Maestro /
GemsBot conventions measured from a 67-card reference corpus):

  Canvas     1280x840, ~3:2 landscape
  Background #0d0d12 -> #14161e vertical gradient + accent radial tint
  Border     rounded 20px, 4-layer glow border
  Numbers    JetBrains Mono Bold (all of them)
  Colors     win #00ff88 · loss #ff3b3b · milestone gold #ffd24a
             labels #8a8f98 · values #ffffff · Solana badge #0099ff

  Sell card order:   header (avatar+ticker+SOL badge+brand) -> hero PnL%
                     -> "Received ◎ X" -> sparkline -> detail rows
                     (multiplier, entry→exit mcap, spent/received, held,
                     wallet tag) -> footer t.me/GEMBOT

  Milestone card:    the multiplier IS the card (~200px, gold->green
                     gradient fill + glow). Escalation tiers:
                       1.5x-3x  regular card, multiplier as gold badge
                       5x+      gold border tint + rocket glyph
                       10x+     GEM CALL ribbon + confetti
                       50x+     full celebration (purple accents, max confetti)

Implementation note: this Pillow build does NOT alpha-blend ImageDraw fills
on the base image (a translucent fill overwrites pixels), so every
translucent element goes through an overlay layer composited with
alpha_composite().

Usage:
    from bot.pnl_card import generate_pnl_card
    png_bytes = generate_pnl_card(token_symbol="PEPE", pnl_pct=312.4, ...)
"""

import io
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 840
MARGIN = 56
RADIUS = 20

# ── Palette (research spec) ───────────────────────────────────────────
WIN = (0, 255, 136)          # #00ff88
LOSS = (255, 59, 59)         # #ff3b3b
GOLD = (255, 210, 74)        # #ffd24a
PURPLE = (171, 99, 250)      # celebration accent (50x+)
LABEL = (138, 143, 152)      # #8a8f98
VALUE = (255, 255, 255)
SOL_BLUE = (0, 153, 255)     # #0099ff
BG_TOP = (13, 13, 18)        # #0d0d12
BG_BOTTOM = (20, 22, 30)     # #14161e
BRAND = (0, 255, 136)
INK = (10, 10, 14)

CONFETTI_COLORS = [GOLD, WIN, SOL_BLUE, (255, 122, 69), PURPLE, VALUE]

_FONT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "assets", "JetBrainsMono-Bold.ttf")

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _font_cache:
        _font_cache[size] = ImageFont.truetype(_FONT_PATH, size)
    return _font_cache[size]


def _fmt_mcap(n) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:.0f}"


def _fmt_mult(m) -> str:
    return f"{m:g}x"


# ── Canvas primitives ─────────────────────────────────────────────────

def _background(accent: tuple, accent_alpha: int = 26) -> Image.Image:
    """Vertical gradient + soft radial tint of the accent color."""
    img = Image.new("RGB", (W, H), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)],
               fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    tint = Image.new("L", (W, H), 0)
    td = ImageDraw.Draw(tint)
    cx, cy, r = W * 0.78, H * 0.30, W * 0.55
    for rr in range(int(r), 0, -8):
        td.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                   fill=int(accent_alpha * (1 - rr / r)))
    tint = tint.filter(ImageFilter.GaussianBlur(60))
    overlay = Image.new("RGB", (W, H), accent)
    img = Image.composite(overlay, img, tint)
    return img.convert("RGBA")


def _overlay(img: Image.Image) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Fresh transparent layer + its draw handle (composite when done)."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    return layer, ImageDraw.Draw(layer)


def _composite(img: Image.Image, layer: Image.Image):
    img.alpha_composite(layer)


def _blend_rect(img: Image.Image, box, radius, fill_color, alpha,
                outline_color=None, outline_alpha=0, width=1):
    """Rounded rect with TRUE alpha blending (overlay + composite)."""
    layer, d = _overlay(img)
    d.rounded_rectangle(box, radius=radius,
                        fill=fill_color + (alpha,))
    if outline_color and outline_alpha:
        d.rounded_rectangle(box, radius=radius,
                            outline=outline_color + (outline_alpha,), width=width)
    _composite(img, layer)


def _blend_line(img: Image.Image, xy0, xy1, color, alpha, width=1):
    layer, d = _overlay(img)
    d.line([xy0, xy1], fill=color + (alpha,), width=width)
    _composite(img, layer)


def _glow_border(img: Image.Image, accent: tuple):
    """4-layer rounded glow border."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for inset, width, alpha in [(6, 10, 90), (12, 7, 60), (18, 5, 38), (24, 3, 22)]:
        d.rounded_rectangle([inset, inset, W - inset, H - inset],
                            radius=RADIUS + inset,
                            outline=accent + (alpha,), width=width)
    layer = layer.filter(ImageFilter.GaussianBlur(7))
    img.alpha_composite(layer)
    d2 = ImageDraw.Draw(img)
    d2.rounded_rectangle([4, 4, W - 4, H - 4], radius=RADIUS,
                         outline=accent + (255,), width=2)


def _glow(img: Image.Image, xy, text, font, color,
          glow_radius: int = 18, glow_alpha: int = 160,
          align: str = "left") -> tuple[int, int]:
    """Glow halo ONLY (no solid text) — pair with a text draw after."""
    x, y = xy
    bbox = font.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if align == "center":
        x = x - tw / 2
    pad = glow_radius * 3
    layer = Image.new("RGBA", (int(tw) + pad * 2, int(th) + pad * 2), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=color + (glow_alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(glow_radius))
    img.alpha_composite(layer, (int(x - pad), int(y - pad)))
    return int(x), int(y)


def _text(img: Image.Image, xy, text, font, color,
          align: str = "left", anchor_xy: str = "la") -> tuple[int, int]:
    """Solid text; align='center' centers horizontally on xy[0]."""
    x, y = xy
    if align == "center":
        bbox = font.getbbox(text)
        x = x - (bbox[2] - bbox[0]) / 2 - bbox[0]
        y = y - bbox[1]
    d = ImageDraw.Draw(img)
    d.text((x, y), text, font=font, fill=color + (255,))
    return int(x), int(y)


def _gradient_text(img: Image.Image, xy, text, font, top_color, bottom_color,
                   glow_color=None, align: str = "center") -> tuple[int, int]:
    """Vertical gradient fill inside the glyphs + soft glow behind."""
    x, y = xy
    bbox = font.getbbox(text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if align == "center":
        x = x - tw / 2
    if glow_color:
        _glow(img, (x, y), text, font, glow_color, glow_radius=26, glow_alpha=110)
    mask = Image.new("L", (int(tw) + 4, int(th) + 4), 0)
    md = ImageDraw.Draw(mask)
    md.text((-bbox[0] + 2, -bbox[1] + 2), text, font=font, fill=255)
    grad = Image.new("RGB", mask.size, top_color)
    gd = ImageDraw.Draw(grad)
    for gy in range(mask.size[1]):
        t = gy / max(1, mask.size[1])
        gd.line([(0, gy), (mask.size[0], gy)],
                fill=tuple(int(a + (b - a) * t) for a, b in zip(top_color, bottom_color)))
    img.paste(grad, (int(x), int(y)), mask)
    return int(x), int(y)


def _confetti(img: Image.Image, seed, count: int, region, colors=None):
    rnd = random.Random(seed)
    layer, d = _overlay(img)
    x0, y0, x1, y1 = region
    for _ in range(count):
        cx = rnd.uniform(x0, x1)
        cy = rnd.uniform(y0, y1)
        size = rnd.uniform(3, 7)
        angle = rnd.uniform(0, math.pi)
        dx, dy = math.cos(angle) * size, math.sin(angle) * size
        color = rnd.choice(colors or CONFETTI_COLORS)
        alpha = rnd.randint(120, 220)
        d.line([(cx - dx, cy - dy), (cx + dx, cy + dy)],
               fill=color + (alpha,), width=int(max(2, size * 0.8)))
    _composite(img, layer)


def _sparkline(img: Image.Image, box, seed, color, final_pct: float):
    """Price curve ending at the final PnL, with translucent fill under it."""
    x0, y0, x1, y1 = box
    rnd = random.Random(seed)
    n = 40
    vals, v = [], 0.5
    for _ in range(n):
        v += rnd.uniform(-0.05, 0.05)
        v = max(0.05, min(0.95, v))
        vals.append(v)
    end = 0.9 if final_pct >= 0 else 0.12
    for i in range(n - 8, n):
        t = (i - (n - 8)) / 8
        vals[i] = vals[i] * (1 - t) + end * t
    pts = [(x0 + (x1 - x0) * i / (n - 1),
            y1 - (y1 - y0) * v) for i, v in enumerate(vals)]
    fill_layer, fd = _overlay(img)
    fd.polygon(pts + [(x1, y1), (x0, y1)], fill=color + (25,))
    _composite(img, fill_layer)
    line_layer, ld = _overlay(img)
    ld.line(pts, fill=color + (255,), width=4, joint="curve")
    _composite(img, line_layer)


def _rocket(img: Image.Image, cx, cy, scale: float = 1.0,
            body=(220, 225, 235), flame=GOLD, window=SOL_BLUE):
    """Small vector rocket glyph (emoji-free, renders everywhere)."""
    layer, d = _overlay(img)
    s = scale
    d.polygon([(cx, cy - 34 * s), (cx + 14 * s, cy - 6 * s),
               (cx + 10 * s, cy + 18 * s), (cx - 10 * s, cy + 18 * s),
               (cx - 14 * s, cy - 6 * s)], fill=body + (255,))
    d.ellipse([cx - 5 * s, cy - 16 * s, cx + 5 * s, cy - 6 * s],
              fill=window + (255,))
    d.polygon([(cx - 14 * s, cy + 2 * s), (cx - 24 * s, cy + 20 * s),
               (cx - 10 * s, cy + 18 * s)], fill=flame + (255,))
    d.polygon([(cx + 14 * s, cy + 2 * s), (cx + 24 * s, cy + 20 * s),
               (cx + 10 * s, cy + 18 * s)], fill=flame + (255,))
    d.polygon([(cx - 6 * s, cy + 18 * s), (cx + 6 * s, cy + 18 * s),
               (cx, cy + 34 * s)], fill=flame + (255,))
    _composite(img, layer)


# ── Layout blocks ─────────────────────────────────────────────────────

def _header(img: Image.Image, symbol: str, brand: str = "GEMBOT"):
    d = ImageDraw.Draw(img)
    y = 44
    # Token avatar: colored circle + first letter
    rnd = random.Random(symbol)
    avatar_colors = [WIN, SOL_BLUE, GOLD, PURPLE, (255, 122, 69)]
    ac = rnd.choice(avatar_colors)
    cx, cy, r = MARGIN + 34, y + 34, 34
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ac + (255,))
    letter = symbol[:1].upper()
    f_av = _font(34)
    bb = f_av.getbbox(letter)
    d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy - (bb[3] - bb[1]) / 2 - bb[1]),
           letter, font=f_av, fill=INK + (255,))
    # Ticker
    f_tick = _font(38)
    tick = f"${symbol.upper()}"
    d.text((MARGIN + 84, y + 12), tick, font=f_tick, fill=VALUE + (255,))
    # SOL chain badge
    f_badge = _font(20)
    bw = f_badge.getbbox("SOL")[2]
    bx = MARGIN + 84 + f_tick.getbbox(tick)[2] + 18
    _blend_rect(img, [bx, y + 20, bx + bw + 20, y + 52], 8, SOL_BLUE, 40,
                outline_color=SOL_BLUE, outline_alpha=180, width=1)
    d.text((bx + 10, y + 26), "SOL", font=f_badge, fill=SOL_BLUE + (255,))
    # Brand top-right + rocket mark
    f_brand = _font(26)
    tw = f_brand.getbbox(brand)[2]
    bx2 = W - MARGIN - tw - 56
    _glow(img, (bx2, y + 18), brand, f_brand, BRAND,
          glow_radius=8, glow_alpha=110)
    _text(img, (bx2, y + 18), brand, f_brand, BRAND)
    _rocket(img, W - MARGIN - 20, y + 34, scale=0.75)


def _detail_rows(img: Image.Image, rows: list[tuple[str, str]], y0: int,
                 color_map: dict[str, tuple] | None = None) -> int:
    """Label-left / value-right rows. Returns the y after the last row."""
    d = ImageDraw.Draw(img)
    f_label = _font(24)
    f_value = _font(28)
    y = y0
    color_map = color_map or {}
    for i, (label, value) in enumerate(rows):
        d.text((MARGIN + 8, y), label, font=f_label, fill=LABEL + (255,))
        vb = f_value.getbbox(value)
        vw = vb[2] - vb[0]
        color = color_map.get(label, VALUE)
        d.text((W - MARGIN - 8 - vw, y - 4), value, font=f_value,
               fill=color + (255,))
        y += 52
        if i < len(rows) - 1:
            _blend_line(img, (MARGIN + 8, y - 12), (W - MARGIN - 8, y - 12),
                        VALUE, 14, width=1)
    return y


def _footer(img: Image.Image, handle: str | None):
    d = ImageDraw.Draw(img)
    f_foot = _font(22)
    d.text((MARGIN + 8, H - 52), "t.me/GEMBOT", font=f_foot, fill=BRAND + (255,))
    if handle:
        tag = "@" + handle.lstrip("@")
        tb = f_foot.getbbox(tag)
        d.text((W - MARGIN - 8 - (tb[2] - tb[0]), H - 52), tag,
               font=f_foot, fill=LABEL + (255,))


# ── Main entry ────────────────────────────────────────────────────────

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
    seed=None,
    logo_bytes: bytes = None,
    multiplier: float = None,
    sol_spent: float = None,
    sol_received: float = None,
    milestone_mode: bool = False,
) -> bytes:
    """
    Generate a GEMBOT PnL card (1280x840 PNG bytes).

    Regular/sell card: hero = PnL %, secondary = "Received ◎ X",
    then sparkline + detail rows + footer.

    Milestone card (milestone_mode=True): the multiplier becomes the hero
    with gold gradient + escalation tiers by size:
      1.5x-3x badge · 5x+ gold border + rocket · 10x+ GEM CALL ribbon +
      confetti · 50x+ purple full celebration.
    """
    seed = seed if seed is not None else sum(ord(c) for c in token_symbol)
    accent = WIN if (is_win and pnl_pct >= 0) else LOSS

    img = _background(accent)
    _header(img, token_symbol)

    f_hero = _font(120)
    hero_y = 150

    # ── Milestone escalation tier ────────────────────────────────────
    tier = 0
    if milestone_mode and multiplier:
        if multiplier >= 50:
            tier = 3
        elif multiplier >= 10:
            tier = 2
        elif multiplier >= 5:
            tier = 1

    border_color = GOLD if tier >= 1 else accent

    # GEM CALL ribbon (10x+) — blended pill so the label stays legible
    if tier >= 2:
        f_rib = _font(24)
        rib = "GEM CALL"
        rb = f_rib.getbbox(rib)
        rw = rb[2] - rb[0]
        rx = (W - rw - 44) / 2
        rib_color = PURPLE if tier >= 3 else GOLD
        _blend_rect(img, [rx, hero_y - 8, rx + rw + 44, hero_y + 34], 17,
                    rib_color, 36,
                    outline_color=rib_color, outline_alpha=230, width=2)
        _text(img, (W / 2, hero_y + 2), rib, f_rib, rib_color, align="center")
        hero_y += 56

    # ── Hero number (drawn EXACTLY ONCE) ─────────────────────────────
    if milestone_mode and multiplier and tier >= 1:
        # THE MULTIPLIER IS THE CARD: ~200px gold gradient + glow
        f_big = _font(200)
        txt = _fmt_mult(multiplier)
        bb = f_big.getbbox(txt)
        hx, hy = W / 2, hero_y + 10
        _gradient_text(img, (hx, hy), txt, f_big,
                       (255, 236, 160), GOLD, glow_color=GOLD)
        hero_h = bb[3] - bb[1] + 30
        # PnL% demoted to secondary
        f_sec = _font(56)
        sec = f"+{pnl_pct:.0f}%" if pnl_pct >= 0 else f"{pnl_pct:.0f}%"
        _glow(img, (W / 2, hy + hero_h + 8), sec, f_sec, WIN,
              glow_radius=10, glow_alpha=90)
        _text(img, (W / 2, hy + hero_h + 8), sec, f_sec, WIN, align="center")
        hero_end = hy + hero_h + 8 + 56 + 26
        _rocket(img, W / 2 + bb[2] / 2 + 60, hy + 90, scale=1.15)
    else:
        # Regular hero: PnL % (solid draw, single pass)
        txt = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%"
        bb = f_hero.getbbox(txt)
        _glow(img, (W / 2, hero_y), txt, f_hero, accent,
              glow_radius=22, glow_alpha=150)
        _text(img, (W / 2, hero_y), txt, f_hero, accent, align="center")
        hero_end = hero_y + (bb[3] - bb[1]) + 34
        # small gold multiplier badge (1.5x-3x milestones) — blended, not solid
        if milestone_mode and multiplier:
            f_bad = _font(30)
            bad = _fmt_mult(multiplier)
            bbb = f_bad.getbbox(bad)
            bw = bbb[2] - bbb[0]
            bx = W / 2 + bb[2] / 2 + 24
            by = hero_y + 6
            _blend_rect(img, [bx, by, bx + bw + 32, by + 46], 12,
                        GOLD, 30, outline_color=GOLD, outline_alpha=230, width=2)
            _text(img, (bx + 16, by + 8), bad, f_bad, GOLD)

    # ── Secondary line: SOL received (the flex number) ───────────────
    y = hero_end
    f_sub = _font(34)
    if sol_received is not None:
        sub = f"Received ◎ {sol_received:.4f}"
        if sol_spent:
            sub += f"  (from ◎ {sol_spent:.4f})"
        sub_color = GOLD if milestone_mode else accent
        _glow(img, (W / 2, y), sub, f_sub, sub_color,
              glow_radius=8, glow_alpha=70)
        _text(img, (W / 2, y), sub, f_sub, sub_color, align="center")
        y += 34 + 22
    elif milestone_mode and multiplier and tier == 0:
        sub = f"+{pnl_pct:.0f}%" if pnl_pct >= 0 else f"{pnl_pct:.0f}%"
        _text(img, (W / 2, y), sub, f_sub, WIN, align="center")
        y += 34 + 22

    # ── Sparkline (skipped on big milestones to keep rows breathing) ─
    if not (milestone_mode and tier >= 1):
        _sparkline(img, (MARGIN + 20, y + 6, W - MARGIN - 20, y + 116),
                   seed, accent, pnl_pct)
        y += 140

    # ── Detail rows ──────────────────────────────────────────────────
    rows: list[tuple[str, str]] = []
    cmap: dict[str, tuple] = {}
    if multiplier:
        rows.append(("MULTIPLIER", _fmt_mult(multiplier)))
        cmap["MULTIPLIER"] = GOLD if milestone_mode else accent
    if entry_mcap is not None and current_mcap is not None:
        rows.append(("MCAP JOURNEY", f"{_fmt_mcap(entry_mcap)} → {_fmt_mcap(current_mcap)}"))
    if sol_spent is not None:
        rows.append(("SOL SPENT", f"◎ {sol_spent:.4f}"))
    if sol_received is not None:
        rows.append(("SOL RECEIVED", f"◎ {sol_received:.4f}"))
        cmap["SOL RECEIVED"] = GOLD
    if peak_mcap is not None and not milestone_mode:
        rows.append(("PEAK MCAP", _fmt_mcap(peak_mcap)))
    if duration:
        rows.append(("HELD", duration))
    if wallet:
        short = wallet if len(wallet) <= 16 else f"{wallet[:6]}…{wallet[-4:]}"
        rows.append(("WALLET", short))
    max_rows = max(2, int((H - 92 - y) // 52))
    rows = rows[:max_rows]
    if rows:
        _detail_rows(img, rows, y, cmap)

    # ── Confetti tiers ───────────────────────────────────────────────
    if milestone_mode:
        if tier >= 3:
            _confetti(img, seed, 90, (60, 40, W - 60, hero_end + 80),
                      colors=CONFETTI_COLORS + [PURPLE, GOLD])
            _confetti(img, seed + 1, 50, (60, hero_end + 80, W - 60, H - 120))
        elif tier >= 2:
            _confetti(img, seed, 55, (60, 40, W - 60, hero_end + 60))
        elif tier >= 1:
            _confetti(img, seed, 24, (60, 40, W - 60, 340))

    # ── Border + footer last (crisp on top) ──────────────────────────
    _glow_border(img, border_color)
    _footer(img, telegram_username)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()
