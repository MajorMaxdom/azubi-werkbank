from __future__ import annotations

import json
import re
import shutil

import pytest

from app.loader import load_file
from app.renderer import build_view, render_markdown, render_markdown_inline
from tests.conftest import BASE_URL, ROOT, add_user, make_config, open_client, second_client
from tests.test_progress import CATALOG

API = "/api/progress/demo"
JSON_HEADERS = {"Origin": BASE_URL, "X-Workbook": "1"}
REVIEW = f"{API}/users/anna/tasks/t1/review"
SIGNOFF = f"{API}/users/anna/signoff"


@pytest.fixture
def config(tmp_path):
    config = make_config(tmp_path)
    (config.paths.workbooks / "demo.yaml").write_text(
        CATALOG.replace(
            "expectations: [GEHEIM]", "expectations: [GEHEIM]\n              notes: NOTIZ-X"
        ),
        encoding="utf-8",
    )
    add_user(config, "boss", "trainer", name="Chefin Boss")
    add_user(config, "kai", "trainer", name="Kai Schulz")
    add_user(config, "anna", "apprentice", name="Anna Azubi")
    add_user(config, "ben", "apprentice", name="Ben Azubi")
    return config


def patch(client, url, body):
    return client.patch(url, json=body, headers=JSON_HEADERS)


def stored(config, user="anna") -> dict:
    return json.loads((config.paths.progress / "demo" / f"{user}.json").read_text())


def set_supervisors(config, block: str) -> None:
    text = config.paths.users.read_text()
    text = text.replace(
        "  anna:\n    name: Anna Azubi\n    role: apprentice\n",
        "  anna:\n    name: Anna Azubi\n    role: apprentice\n" + block,
    )
    config.paths.users.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ trainer content never leaks


def test_no_trainer_content_in_apprentice_html_network_security(tmp_path):
    config = make_config(tmp_path)
    shutil.copy(ROOT / "workbooks" / "network-security.yaml", config.paths.workbooks)
    add_user(config, "anna", "apprentice")
    add_user(config, "boss", "trainer")
    catalog = load_file(ROOT / "workbooks" / "network-security.yaml").catalog
    secrets = []
    for tv in build_view(catalog).tasks_by_id.values():
        if tv.task.trainer:
            secrets += [str(render_markdown_inline(e)) for e in tv.task.trainer.expectations]
            if tv.task.trainer.notes:
                secrets.append(str(render_markdown(tv.task.trainer.notes)).strip())
    assert len(secrets) > 40
    with open_client(config, "anna") as client:
        patch_ok = client.patch(
            "/api/progress/network-security/tasks/sys01-boot",
            json={"done": True},
            headers=JSON_HEADERS,
        )
        assert patch_ok.status_code == 200
        pages = [client.get("/").text, client.get("/workbooks/network-security").text]
    with open_client(config, "boss") as trainer:
        review_html = trainer.get("/workbooks/network-security/users/anna").text
    assert all(s in review_html for s in secrets)  # sanity: the trainer does see them
    for html in pages:
        for secret in secrets:
            assert secret not in html
        for marker in ("Bereich Fachbetreuer", "Das sollte drinstehen", 'class="trainer"',
                       "data-review-status", "Interne Notizen"):  # fmt: skip
            assert marker not in html


def test_apprentice_review_display_never_shows_expectations(config):
    with open_client(config, "boss") as trainer:
        anna = second_client(trainer, "anna")
        patch(anna, f"{API}/tasks/t1", {"done": True})
        patch(trainer, REVIEW, {"status": "redo", "comment": "Bitte Kette ergänzen."})
        html = anna.get("/workbooks/demo").text
    assert "Rückmeldung deines Fachbetreuers" in html
    assert "Nacharbeiten" in html and "Bitte Kette ergänzen." in html
    assert "GEHEIM" not in html and "NOTIZ-X" not in html
    assert 'class="port is-redo" data-port="t1"' in html


# ------------------------------------------------------------------ apprentices get 403


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", "/workbooks/demo/users/anna", None),
        ("GET", "/workbooks/demo/users/ben", None),
        ("GET", "/admin/overview", None),
        ("GET", "/admin/users", None),
        ("GET", "/admin/catalogs", None),
        ("GET", "/admin/users/anna", None),
        ("PATCH", REVIEW, {"status": "ok"}),
        ("PATCH", f"{API}/users/ben/tasks/t1/review", {"status": "ok"}),
        ("PATCH", SIGNOFF, {"comment": "x", "date": ""}),
    ],
)
def test_apprentice_gets_403_on_trainer_endpoints(config, method, url, body):
    with open_client(config, "anna") as client:
        if method == "GET":
            response = client.get(url)
        else:
            response = client.patch(url, json=body, headers=JSON_HEADERS)
    assert response.status_code == 403
    assert not (config.paths.progress / "demo").exists() or not any(
        "review" in p.read_text() or "Abgenommen" in p.read_text()
        for p in (config.paths.progress / "demo").glob("*.json")
    )


# ------------------------------------------------------------------ review API


def test_review_flow(config):
    with open_client(config, "boss") as trainer:
        anna = second_client(trainer, "anna")
        patch(anna, f"{API}/tasks/t1", {"answers": {"a1": "Antwort von Anna"}, "done": True})

        page = trainer.get("/workbooks/demo/users/anna").text
        assert "Bewertungsansicht – Arbeitsheft von Anna Azubi (anna)" in page
        assert "Antwort von Anna</textarea>" in page
        assert 'data-review-user="anna"' in page and "data-autosave" in page
        assert '<textarea id="a-t1-a1"' in page and "disabled>Antwort von Anna" in page
        assert 'id="status-t1" class="review-select" data-review-status>' in page  # editable

        r = patch(trainer, REVIEW, {"status": "ok", "comment": "Sauber!"})
        assert r.status_code == 200, r.text
        review = stored(config)["tasks"]["t1"]["review"]
        assert review["status"] == "ok" and review["comment"] == "Sauber!"
        assert review["reviewed_by"] == "boss" and review["reviewed_at"]
        # Answers are untouched by the review.
        assert stored(config)["tasks"]["t1"]["answers"]["a1"] == "Antwort von Anna"

        page = trainer.get("/workbooks/demo/users/anna").text
        assert '<option value="ok" selected>' in page
        assert "Sauber!</textarea>" in page
        assert "Zuletzt bewertet von Chefin Boss" in page

        # Clearing status and comment removes the review.
        patch(trainer, REVIEW, {"status": None, "comment": ""})
        assert stored(config)["tasks"]["t1"]["review"] is None


def test_review_by_comes_from_session(config):
    with open_client(config, "kai") as trainer:
        r = patch(trainer, REVIEW, {"status": "ok", "reviewed_by": "boss"})
        assert r.status_code == 422
        patch(trainer, REVIEW, {"status": "ok"})
    assert stored(config)["tasks"]["t1"]["review"]["reviewed_by"] == "kai"


@pytest.mark.parametrize(
    ("url", "body", "code"),
    [
        (REVIEW, {"status": "great"}, 422),
        (REVIEW, {"comment": 5}, 422),
        (REVIEW, {"comment": "x" * 20_001}, 422),
        (REVIEW, {"answers": {"a1": "x"}}, 422),
        (REVIEW, {"done": True}, 422),
        (f"{API}/users/anna/tasks/nope/review", {"status": "ok"}, 422),
        (f"{API}/users/ghost/tasks/t1/review", {"status": "ok"}, 404),
        (f"{API}/users/boss/tasks/t1/review", {"status": "ok"}, 404),
        (f"{API}/users/..%2Fx/tasks/t1/review", {"status": "ok"}, 404),
        ("/api/progress/nope/users/anna/tasks/t1/review", {"status": "ok"}, 404),
        (SIGNOFF, {"comment": "x", "date": "morgen"}, 422),
        (SIGNOFF, {"comment": "x", "by": "boss"}, 422),
    ],
)
def test_review_validation(config, url, body, code):
    with open_client(config, "boss") as trainer:
        assert patch(trainer, url, body).status_code == code


def test_review_requires_json_api_headers(config):
    with open_client(config, "boss") as trainer:
        r = trainer.patch(REVIEW, json={"status": "ok"}, headers={"Origin": BASE_URL})
        assert r.status_code == 403


def test_review_respects_apprentice_workbook_list(config):
    (config.paths.workbooks / "other.yaml").write_text(
        CATALOG.replace("id: demo", "id: other"), encoding="utf-8"
    )
    add_user(config, "carl", "apprentice", workbooks=["demo"])
    with open_client(config, "boss") as trainer:
        assert trainer.get("/workbooks/other/users/carl").status_code == 404
        r = patch(trainer, "/api/progress/other/users/carl/tasks/t1/review", {"status": "ok"})
        assert r.status_code == 404


# ------------------------------------------------------------------ sign-off


def test_signoff(config):
    with open_client(config, "boss") as trainer:
        anna = second_client(trainer, "anna")
        assert "Abgenommen" not in anna.get("/workbooks/demo").text
        r = patch(trainer, SIGNOFF, {"comment": "Sehr gute Woche.", "date": "2026-10-06"})
        assert r.status_code == 200
        assert stored(config)["signoff"] == {
            "comment": "Sehr gute Woche.",
            "date": "2026-10-06",
            "by": "boss",
        }
        page = trainer.get("/workbooks/demo/users/anna").text
        assert 'data-signoff="date" type="date" value="2026-10-06"' in page
        assert "Chefin Boss" in page
        html = anna.get("/workbooks/demo").text
        assert "Abgenommen am 06.10.2026" in html and "Sehr gute Woche." in html
        assert 'data-signoff="comment"' not in html  # no form for apprentices

        patch(trainer, SIGNOFF, {"comment": "", "date": ""})
        assert stored(config)["signoff"] == {"comment": "", "date": None, "by": None}


def test_signoff_disabled_workbook(config):
    path = config.paths.workbooks / "demo.yaml"
    path.write_text(
        path.read_text().replace(
            "  title: Demo\n", "  title: Demo\n  signoff:\n    enabled: false\n"
        ),
        encoding="utf-8",
    )
    with open_client(config, "boss") as trainer:
        assert patch(trainer, SIGNOFF, {"comment": "x", "date": ""}).status_code == 422
        assert 'id="signoff"' not in trainer.get("/workbooks/demo/users/anna").text


# ------------------------------------------------------------------ review view details


def test_review_view_shows_supervisor_and_orphans(config):
    set_supervisors(config, "    supervisors:\n      demo:\n        default: boss\n"
                            "        tasks:\n          t2: kai\n")  # fmt: skip
    with open_client(config, "boss") as trainer:
        anna = second_client(trainer, "anna")
        patch(anna, f"{API}/tasks/t2", {"answers": {"a1": "Altlast"}})
        path = config.paths.workbooks / "demo.yaml"
        path.write_text(
            path.read_text().replace("              - {id: a1}\n", "              - {id: neu}\n")
        )
        trainer.app.state.registry.apply_changes({path})
        page = trainer.get("/workbooks/demo/users/anna").text
    assert "Zuständig: Chefin Boss" in page and "Zuständig: Kai Schulz" in page
    assert "Verwaiste Antworten" in page
    assert re.search(r"t2 · a1</dt>\s*<dd>Altlast</dd>", page)


def test_trainer_preview_is_read_only(config):
    with open_client(config, "boss") as trainer:
        html = trainer.get("/workbooks/demo").text
    assert "data-autosave" not in html
    assert "data-review-status disabled" in html
    assert 'data-signoff="comment"' in html and "disabled" in html


# ------------------------------------------------------------------ overview & "Meine Azubis"


def test_overview_matrix(config):
    add_user(config, "olaf", "apprentice", name="Olaf Inaktiv")
    with open_client(config, "boss") as trainer:
        anna = second_client(trainer, "anna")
        patch(anna, f"{API}/tasks/t1", {"done": True})
        patch(anna, f"{API}/tasks/t2", {"done": True})
        patch(trainer, f"{API}/users/anna/tasks/t2/review", {"status": "redo"})
        trainer.app.state.accounts.set_active("olaf", False)
        html = trainer.get("/admin/overview").text
    assert 'id="demo"' in html
    assert (
        'href="/workbooks/demo/users/anna"' in html and 'href="/workbooks/demo/users/ben"' in html
    )
    assert "Olaf Inaktiv" not in html
    anna_row = html[html.index("Anna Azubi") : html.index("Ben Azubi")]
    assert 'class="port port-link is-lit"' in anna_row  # t1 done, not reviewed
    assert 'class="port port-link is-redo"' in anna_row  # t2 redo
    assert "2 / 2" in anna_row
    assert "1.1 Aufgabe eins – erledigt, noch nicht geprüft" in anna_row


def test_meine_azubis(config):
    set_supervisors(config, "    supervisors:\n      demo:\n        default: boss\n"
                            "        tasks:\n          t2: kai\n")  # fmt: skip
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        kai = second_client(boss, "kai")
        patch(anna, f"{API}/tasks/t1", {"done": True})
        patch(anna, f"{API}/tasks/t2", {"done": True})

        boss_home = boss.get("/").text
        assert "Meine Azubis" in boss_home
        assert 'href="/workbooks/demo/users/anna"' in boss_home
        assert "nur Aufgaben 1.1" in boss_home
        assert "1 Aufgabe zu prüfen" in boss_home
        assert "Ben Azubi" not in boss_home

        kai_home = kai.get("/").text
        assert "nur Aufgaben 1.2" in kai_home
        assert "1 Aufgabe zu prüfen" in kai_home

        # After OK the check disappears; a later change by Anna re-opens it.
        patch(boss, REVIEW, {"status": "ok"})
        assert "zu prüfen" not in boss.get("/").text.split("Alle Arbeitshefte")[0]
        patch(anna, f"{API}/tasks/t1", {"answers": {"a1": "nachgebessert"}})
        assert "1 Aufgabe zu prüfen" in boss.get("/").text

        assert "Meine Azubis" not in anna.get("/").text


def test_meine_azubis_empty(config):
    with open_client(config, "kai") as kai:
        assert "Du bist noch bei keinem Azubi als Fachbetreuer eingetragen." in kai.get("/").text
