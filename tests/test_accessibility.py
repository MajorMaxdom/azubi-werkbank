"""Accessibility checks: labels for every form control, skip link, language,
titles, reduced motion and WCAG AA contrast of the colour combinations used."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from app.renderer import STATIC_DIR
from app.theme import contrast_ratio, default_colors
from tests.test_csp import pages  # noqa: F401  (fixture with all rendered pages)


class LabelAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.label_for: set[str] = set()
        self.controls: list[tuple[str, dict]] = []
        self.label_depth = 0
        self.wrapped: set[int] = set()
        self.title = ""
        self._in_title = False
        self.lang = None
        self.ids: set[str] = set()
        self.skip_target = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "title":
            self._in_title = True
        if tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        if tag == "a" and "skip-link" in (a.get("class") or ""):
            self.skip_target = a.get("href")
        if (
            tag in ("input", "textarea", "select")
            and a.get("type") != "hidden"
            and "hidden" not in a
        ):
            if self.label_depth:
                self.wrapped.add(len(self.controls))
            self.controls.append((tag, a))

    def handle_endtag(self, tag):
        if tag == "label":
            self.label_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def unlabelled(self) -> list[str]:
        missing = []
        for index, (tag, a) in enumerate(self.controls):
            if index in self.wrapped or a.get("aria-label"):
                continue
            if a.get("id") and a["id"] in self.label_for:
                continue
            missing.append(f"<{tag} {a}>")
        return missing


def test_every_form_control_has_a_label(pages):  # noqa: F811
    problems = {}
    for name, html in pages.items():
        audit = LabelAudit()
        audit.feed(html)
        if audit.unlabelled():
            problems[name] = audit.unlabelled()
    assert problems == {}


def test_language_title_and_skip_link(pages):  # noqa: F811
    for name, html in pages.items():
        audit = LabelAudit()
        audit.feed(html)
        assert audit.lang == "de", name
        assert audit.title.strip(), name
        assert audit.skip_target == "#main", name
        assert "main" in audit.ids, name


def test_reduced_motion_and_focus_styles():
    css = (STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in css
    for selector in ("input:focus-visible", "summary:focus-visible", "a:focus-visible",
                     ".button:focus-visible", ".topbar-button:focus-visible",
                     ".link-button:focus-visible", ".skip-link:focus"):  # fmt: skip
        assert selector in css, selector


# ------------------------------------------------------------------ contrast


def _rgb(color: str) -> list[int]:
    h = color.lstrip("#")
    return [int(h[i : i + 2], 16) for i in (0, 2, 4)]


def mix(a: str, share: float, b: str) -> str:
    """color-mix(in srgb, a share, b) — as the browser computes it."""
    ca, cb = _rgb(a), _rgb(b)
    return "#" + "".join(f"{round(ca[i] * share + cb[i] * (1 - share)):02X}" for i in range(3))


C = default_colors()
WHITE, BLACK = "#FFFFFF", "#000000"
ACCENT_SOFT = mix(C["--accent"], 0.10, WHITE)
DARKEN = 0.85  # color-mix(<color> 85%, var(--ink)) used for small coloured text

PAIRS = {
    "body text on paper": (C["--ink"], C["--paper"]),
    "muted text on paper": (C["--muted"], C["--paper"]),
    "muted text / placeholder on card": (C["--muted"], C["--card"]),
    "top bar text": (C["--card"], C["--accent"]),
    "day label on banner": (mix(C["--card"], 0.70, C["--accent"]), C["--accent"]),
    "day subtitle on banner": (mix(C["--card"], 0.78, C["--accent"]), C["--accent"]),
    "day load / code on accent-soft": (C["--accent"], ACCENT_SOFT),
    "accent on card": (C["--accent"], C["--card"]),
    "hint summary": (C["--accent"], C["--hint-bg"]),
    "bonus summary": (C["--muted"], C["--bonus-bg"]),
    "trainer area text": (C["--ink"], C["--trainer-bg"]),
    "trainer heading": (mix(C["--level-ochre"], DARKEN, C["--ink"]), C["--trainer-bg"]),
    "form error": (mix(C["--redo"], DARKEN, C["--ink"]), mix(C["--redo"], 0.08, WHITE)),
    "done label": (C["--ok"], C["--card"]),
    "redo text on card": (C["--redo"], C["--card"]),
    "button text": (C["--card"], C["--accent"]),
    "button hover": (C["--card"], mix(C["--accent"], 0.75, BLACK)),
    **{
        f"level badge {name}": (
            mix(C[f"--level-{name}"], DARKEN, C["--ink"]),
            mix(C[f"--level-{name}"], 0.08, WHITE),
        )
        for name in ("blue", "ochre", "green", "grey", "red")
    },
}


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_default_colours_meet_wcag_aa(name):
    fg, bg = PAIRS[name]
    assert contrast_ratio(fg, bg) >= 4.5, f"{name}: {contrast_ratio(fg, bg):.2f}"


def test_css_uses_the_darkened_variants():
    css = (STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    assert "color: color-mix(in srgb, var(--level) 85%, var(--ink));" in css
    assert re.search(r"\.trainer-head \{[^}]*var\(--level-ochre\) 85%, var\(--ink\)", css)
    assert re.search(r"\.form-error \{[^}]*var\(--redo\) 85%, var\(--ink\)", css)
    assert "textarea::placeholder { color: var(--muted)" in css
