"""Per-workbook color themes: generated ``theme.css`` and WCAG contrast checks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from app.models.catalog import HEX_COLOR_PATTERN, Stylesheet

TOKENS_CSS = Path(__file__).resolve().parent / "static" / "css" / "tokens.css"

STYLESHEET_PROPERTIES: dict[str, str] = {
    "paper": "--paper",
    "card": "--card",
    "ink": "--ink",
    "muted": "--muted",
    "line": "--line",
    "accent": "--accent",
    "accent_hover": "--accent-hover",
    "accent_soft": "--accent-soft",
    "ok": "--ok",
    "redo": "--redo",
    "hint_bg": "--hint-bg",
    "trainer_bg": "--trainer-bg",
    "bonus_bg": "--bonus-bg",
}
LEVEL_PROPERTIES: dict[str, str] = {
    color: f"--level-{color}" for color in ("blue", "ochre", "green", "grey", "red")
}
_HEX_RE = re.compile(HEX_COLOR_PATTERN)

MIN_CONTRAST = 4.5
WHITE = "#FFFFFF"


def theme_declarations(stylesheet: Stylesheet | None) -> list[tuple[str, str]]:
    if stylesheet is None:
        return []
    decls: list[tuple[str, str]] = []
    for key, prop in STYLESHEET_PROPERTIES.items():
        value = getattr(stylesheet, key)
        if value is not None:
            decls.append((prop, value))
    if stylesheet.level_palette is not None:
        for key, prop in LEVEL_PROPERTIES.items():
            value = getattr(stylesheet.level_palette, key)
            if value is not None:
                decls.append((prop, value))
    for prop, value in decls:
        # Defense in depth: the model already validated these.
        if not _HEX_RE.fullmatch(value):
            raise ValueError(f"Refusing non-hex color for {prop}")
    return decls


def theme_css(stylesheet: Stylesheet | None) -> str:
    """Generated per-workbook overrides: one ``:root`` block with only the set keys."""
    decls = theme_declarations(stylesheet)
    if not decls:
        return ""
    body = "".join(f"  {prop}: {value};\n" for prop, value in decls)
    return f":root {{\n{body}}}\n"


def theme_hash(css: str) -> str:
    return hashlib.sha256(css.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- contrast


@cache
def default_colors() -> dict[str, str]:
    """Hex defaults from tokens.css (color-mix derived values are skipped)."""
    css = TOKENS_CSS.read_text(encoding="utf-8")
    return dict(re.findall(r"(--[a-z-]+):\s*(#[0-9A-Fa-f]{3,6})\s*;", css))


def effective_colors(stylesheet: Stylesheet | None) -> dict[str, str]:
    colors = dict(default_colors())
    colors.update(dict(theme_declarations(stylesheet)))
    return colors


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    digits = hex_color.lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    r, g, b = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(a: str, b: str) -> float:
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@dataclass(frozen=True)
class ContrastWarning:
    foreground: str  # CSS property or "white"
    background: str
    ratio: float


# (foreground, background) pairs checked on the effective colors.
CONTRAST_PAIRS = (
    ("--ink", "--card"),
    ("--ink", "--paper"),
    ("white", "--accent"),
    ("--accent", "--card"),
)


def contrast_warnings(stylesheet: Stylesheet | None) -> list[ContrastWarning]:
    colors = effective_colors(stylesheet)
    warnings = []
    for fg, bg in CONTRAST_PAIRS:
        fg_value = WHITE if fg == "white" else colors.get(fg)
        bg_value = colors.get(bg)
        if not fg_value or not bg_value:
            continue
        ratio = contrast_ratio(fg_value, bg_value)
        if ratio < MIN_CONTRAST:
            warnings.append(ContrastWarning(fg, bg, round(ratio, 2)))
    return warnings
