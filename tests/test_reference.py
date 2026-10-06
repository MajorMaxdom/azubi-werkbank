"""The converted network-security workbook must carry every piece of content of
the hand-written reference (docs/reference/arbeitsheft.html)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from app.loader import load_file
from app.renderer import build_view, render_static
from tests.conftest import ROOT

REFERENCE = ROOT / "docs" / "reference" / "arbeitsheft.html"
CATALOG = ROOT / "workbooks" / "network-security.yaml"

# Elements that do not split text (inline formatting).
INLINE = {"strong", "em", "code", "b", "i"}
# Reference UI chrome that is intentionally replaced by locale strings or the server.
SKIP_TAGS = {"script", "style", "title", "button", "option", "summary"}
SKIP_TEXT = {"0 / 0 erledigt"}
# Labels that the reference writes inline and the app renders as micro labels.
LABEL_PREFIXES = ("Anforderung: ", "Lernziel: ")
# Agreed changes since the conversion (workbook 4.1.0): the header field
# "Ausbilder/in" was removed and "Ausbilder" is called "Fachbetreuer".
REMOVED = {"Ausbilder/in", "Name Ausbilder/in"}


def adapt(text: str) -> str:
    return text.replace("Ausbilder", "Fachbetreuer")


class Segments(HTMLParser):
    """Collect normalized text segments (split at non-inline elements) and placeholders."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[str] = []
        self.placeholders: list[str] = []
        self._buf: list[str] = []
        self._skip = 0

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf).replace("\xa0", " ")).strip()
        if text:
            self.segments.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        if tag not in INLINE:
            self._flush()
        placeholder = dict(attrs).get("placeholder")
        if placeholder:
            self.placeholders.append(re.sub(r"\s+", " ", placeholder).strip())

    def handle_endtag(self, tag):
        if tag not in INLINE:
            self._flush()
        if tag in SKIP_TAGS:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)


def parse(html: str) -> Segments:
    parser = Segments()
    parser.feed(html)
    parser.close()
    return parser


@pytest.fixture(scope="module")
def catalog():
    result = load_file(CATALOG)
    assert result.errors == []
    return result.catalog


@pytest.fixture(scope="module")
def reference():
    return parse(REFERENCE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rendered(catalog):
    return parse(render_static(catalog, trainer=True))


def test_every_reference_text_is_rendered(reference, rendered):
    haystack = "\n".join(rendered.segments)
    missing = []
    for segment in reference.segments:
        if segment in SKIP_TEXT or segment in REMOVED:
            continue
        segment = adapt(segment)
        for prefix in LABEL_PREFIXES:
            segment = segment.removeprefix(prefix)
        if segment not in haystack:
            missing.append(segment)
    assert missing == []


def test_every_reference_placeholder_is_rendered(reference, rendered):
    missing = [
        adapt(p)
        for p in reference.placeholders
        if p not in REMOVED and adapt(p) not in rendered.placeholders
    ]
    # The reference trainer/signoff placeholders come from locales and may differ in punctuation.
    missing = [p for p in missing if not p.startswith(("Kommentar / Feedback", "Gesamteindruck"))]
    assert missing == []


def test_structure_matches_reference(catalog):
    html = REFERENCE.read_text(encoding="utf-8")
    view = build_view(catalog)
    assert len(view.days) == html.count('class="day-banner"')
    assert sum(len(d.modules) for d in view.days) == html.count('class="module-code"')
    assert view.task_count == html.count('class="task" data-task')
    answers = sum(len(tv.task.answers) for tv in view.tasks_by_id.values())
    # Answer labels inside tasks (the sign-off comment uses the same class).
    assert answers == len(
        re.findall(r'class="answer-label">[^<]*</span>\s*<textarea data-answer', html)
    )
    hints = sum(1 for tv in view.tasks_by_id.values() if tv.task.hints)
    bonus = sum(1 for tv in view.tasks_by_id.values() if tv.task.bonus)
    assert hints == html.count('class="hilfe"')
    assert bonus == html.count('class="extra"')
    loads = re.findall(r'class="day-load">([^<]+)<', html)
    assert [d.load for d in view.days] == [x.replace("&middot;", "·") for x in loads]
    numbers = re.findall(r'class="task-id">([^<]+)<', html)
    assert [tv.number for tv in view.tasks_by_id.values()] == numbers


def test_apprentice_view_has_no_trainer_content(catalog):
    html = render_static(catalog, trainer=False)
    for tv in build_view(catalog).tasks_by_id.values():
        for expectation in tv.task.trainer.expectations:
            assert expectation[:40] not in html
    assert "Bereich Fachbetreuer" not in html
    assert "Das sollte drinstehen" not in html
