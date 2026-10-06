from __future__ import annotations

import re

import pytest

from tests.conftest import BASE_URL, add_user, make_config, open_client, second_client
from tests.test_progress import CATALOG

JSON_HEADERS = {"Origin": BASE_URL, "X-Workbook": "1"}
API = "/api/progress/demo"


@pytest.fixture
def config(tmp_path):
    config = make_config(tmp_path)
    themed = CATALOG.replace(
        "  title: Demo\n", '  title: Demo\n  stylesheet:\n    accent: "#0F4C3A"\n'
    ).replace("requirement: Tu etwas.\n", "requirement: Tu etwas.\n            hints: Ein Tipp.\n")
    (config.paths.workbooks / "demo.yaml").write_text(themed, encoding="utf-8")
    add_user(config, "boss", "trainer", name="Chefin Boss")
    add_user(config, "anna", "apprentice", name="Anna Azubi")
    add_user(config, "ben", "apprentice", name="Ben Azubi")
    return config


def fill(trainer, anna) -> None:
    anna.patch(f"{API}/tasks/t1", json={
        "answers": {"a1": "Zeile 1\nZeile <2>", "cl": ["Kette"], "dt": "2026-10-06"},
        "done": True,
    }, headers=JSON_HEADERS)  # fmt: skip
    anna.patch(f"{API}/header", json={"start": "2026-10-05"}, headers=JSON_HEADERS)
    review = {"status": "redo", "comment": "Mehr Details"}
    trainer.patch(f"{API}/users/anna/tasks/t1/review", json=review, headers=JSON_HEADERS)
    signoff = {"comment": "Gut gemacht", "date": "2026-10-07"}
    trainer.patch(f"{API}/users/anna/signoff", json=signoff, headers=JSON_HEADERS)


def test_apprentice_export_is_self_contained_snapshot(config):
    with open_client(config, "boss") as trainer:
        anna = second_client(trainer, "anna")
        fill(trainer, anna)
        response = anna.get("/workbooks/demo/export")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert re.fullmatch(
        r'attachment; filename="arbeitsheft_demo_anna_\d{4}-\d{2}-\d{2}\.html"', disposition
    )
    html = response.text
    # Offline: everything inlined, nothing loaded from the server.
    assert not re.search(r'(href|src)="/', html)  # no server-relative resources
    assert "theme.css?v=" not in html and "<script" not in html
    assert "data:font/woff2;base64," in html
    assert "--accent: #0F4C3A;" in html  # workbook theme inlined
    # Content with answers, escaped.
    assert "Arbeitsheft von Anna Azubi" in html
    assert "Zeile 1\nZeile &lt;2&gt;</div>" in html
    assert 'value="Kette" checked disabled' in html
    assert "06.10.2026" in html and "05.10.2026" in html
    assert "Erledigt am" in html and "Noch nicht erledigt" in html
    assert "(keine Antwort)" in html
    assert 'class="hints" open' in html
    # Review result for the apprentice, but no trainer content.
    assert "Nacharbeiten" in html and "Mehr Details" in html
    assert "Abgenommen am 07.10.2026" in html
    assert "GEHEIM" not in html and "Bereich Fachbetreuer" not in html
    assert not re.search(r"<body class=\"[^\"]*is-trainer-export", html)


def test_trainer_export_of_apprentice(config):
    with open_client(config, "boss") as trainer:
        anna = second_client(trainer, "anna")
        fill(trainer, anna)
        response = trainer.get("/workbooks/demo/export?user=anna")
    html = response.text
    assert response.status_code == 200
    assert "arbeitsheft_demo_anna_" in response.headers["content-disposition"]
    assert re.search(r"<body class=\"[^\"]*is-trainer-export", html)
    assert "GEHEIM" in html  # expectations included for the Fachbetreuer
    assert re.search(r'class="trainer-result">\s*<strong>Status:</strong>\s*Nacharbeiten', html)
    assert "Zuletzt bewertet von Chefin Boss" in html
    assert "Gut gemacht" in html and "Geprüft durch: Chefin Boss" in html
    assert "<select" not in html and "data-review-status" not in html


def test_trainer_blank_export(config):
    with open_client(config, "boss") as trainer:
        response = trainer.get("/workbooks/demo/export")
    assert response.status_code == 200
    assert "arbeitsheft_demo_leer_" in response.headers["content-disposition"]
    assert "Leeres Arbeitsheft" in response.text


def test_export_permissions(config):
    add_user(config, "carl", "apprentice", workbooks=[])
    with open_client(config, "anna") as anna:
        assert anna.get("/workbooks/demo/export?user=ben").status_code == 403
        assert anna.get("/workbooks/demo/export?user=anna").status_code == 200
    with open_client(config, "boss") as trainer:
        assert trainer.get("/workbooks/demo/export?user=ghost").status_code == 404
        assert trainer.get("/workbooks/demo/export?user=boss").status_code == 200
        assert trainer.get("/workbooks/demo/export?user=..%2Fx").status_code == 404
        assert trainer.get("/workbooks/nope/export").status_code == 404
    with open_client(config) as anonymous:
        assert anonymous.get("/workbooks/demo/export", follow_redirects=False).status_code == 303


def test_export_link_on_pages(config):
    with open_client(config, "boss") as trainer:
        anna = second_client(trainer, "anna")
        assert 'href="/workbooks/demo/export"' in anna.get("/workbooks/demo").text
        review = trainer.get("/workbooks/demo/users/anna").text
        assert 'href="/workbooks/demo/export?user=anna"' in review
