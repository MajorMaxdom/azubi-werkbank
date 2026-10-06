from __future__ import annotations

import pytest

from tests.conftest import BASE_URL, add_user, make_config, open_client, second_client
from tests.test_progress import CATALOG

JSON_HEADERS = {"Origin": BASE_URL, "X-Workbook": "1"}
API = "/api/progress/demo"


@pytest.fixture
def config(tmp_path):
    config = make_config(tmp_path)
    (config.paths.workbooks / "demo.yaml").write_text(CATALOG, encoding="utf-8")
    add_user(config, "boss", "trainer", name="Chefin Boss")
    add_user(config, "kai", "trainer", name="Kai Schulz")
    add_user(config, "anna", "apprentice", name="Anna Azubi")
    text = config.paths.users.read_text().replace(
        "  anna:\n    name: Anna Azubi\n    role: apprentice\n",
        "  anna:\n    name: Anna Azubi\n    role: apprentice\n    supervisors:\n      demo:\n"
        "        default: boss\n        tasks:\n          t2: kai\n",
    )
    config.paths.users.write_text(text, encoding="utf-8")
    return config


def test_apprentice_sees_all_tasks_with_status_and_supervisor(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        anna.patch(f"{API}/tasks/t1", json={"done": True}, headers=JSON_HEADERS)
        review = {"status": "redo", "comment": "Bitte ergänzen"}
        boss.patch(f"{API}/users/anna/tasks/t1/review", json=review, headers=JSON_HEADERS)
        html = anna.get("/my-tasks").text
        assert "Meine Aufgaben" in html
        assert 'href="/workbooks/demo#task-t1"' in html and 'href="/workbooks/demo#task-t2"' in html
        assert "Nacharbeiten" in html and "Bitte ergänzen" in html
        assert "Chefin Boss" in html and "Kai Schulz" in html
        assert "1 von 2 erledigt" in html
        assert "GEHEIM" not in html

        open_only = anna.get("/my-tasks?filter=open").text
        assert "Aufgabe eins" in open_only  # redo counts as open work
        assert "Aufgabe zwei" in open_only


def test_trainer_sees_only_own_responsibilities(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        kai = second_client(boss, "kai")
        anna.patch(f"{API}/tasks/t1", json={"done": True}, headers=JSON_HEADERS)
        anna.patch(f"{API}/tasks/t2", json={"done": True}, headers=JSON_HEADERS)

        boss_html = boss.get("/my-tasks").text
        assert "Anna Azubi · Demo" in boss_html
        assert 'href="/workbooks/demo/users/anna#task-t1"' in boss_html
        assert "task-t2" not in boss_html  # t2 belongs to Kai
        assert "erledigt – wartet auf Prüfung" in boss_html

        kai_html = kai.get("/my-tasks?filter=open").text
        assert "task-t2" in kai_html and "task-t1" not in kai_html

        boss.patch(f"{API}/users/anna/tasks/t1/review", json={"status": "ok"}, headers=JSON_HEADERS)
        assert "Hier ist gerade nichts offen." in boss.get("/my-tasks?filter=open").text
        anna.patch(f"{API}/tasks/t1", json={"answers": {"a1": "neu"}}, headers=JSON_HEADERS)
        assert "geändert – erneut prüfen" in boss.get("/my-tasks?filter=open").text


def test_my_tasks_requires_login(config):
    with open_client(config) as client:
        assert client.get("/my-tasks", follow_redirects=False).status_code == 303


def test_status_filters(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        kai = second_client(boss, "kai")
        anna.patch(f"{API}/tasks/t1", json={"done": True}, headers=JSON_HEADERS)
        anna.patch(f"{API}/tasks/t2", json={"done": True}, headers=JSON_HEADERS)
        boss.patch(f"{API}/users/anna/tasks/t1/review", json={"status": "ok"}, headers=JSON_HEADERS)
        kai.patch(
            f"{API}/users/anna/tasks/t2/review", json={"status": "redo"}, headers=JSON_HEADERS
        )

        ok = anna.get("/my-tasks?filter=ok").text
        assert "task-t1" in ok and "task-t2" not in ok
        assert '<a href="/my-tasks?filter=ok" aria-current="page">Geprüft – OK</a>' in ok
        redo = anna.get("/my-tasks?filter=redo").text
        assert "task-t2" in redo and "task-t1" not in redo

        assert "task-t1" in boss.get("/my-tasks?filter=ok").text
        assert "Keine Aufgaben mit diesem Status." in boss.get("/my-tasks?filter=redo").text
        assert "task-t2" in kai.get("/my-tasks?filter=redo").text

        # Unknown filter values fall back to "Alle".
        fallback = anna.get("/my-tasks?filter=<x>").text
        assert '<a href="/my-tasks" aria-current="page">Alle</a>' in fallback
        assert "task-t1" in fallback and "task-t2" in fallback


def test_apprentice_supervisor_filter(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        anna.patch(f"{API}/tasks/t2", json={"done": True}, headers=JSON_HEADERS)
        html = anna.get("/my-tasks").text
        # Two Fachbetreuer involved -> filter is shown.
        assert 'aria-label="Fachbetreuer"' in html
        assert 'href="/my-tasks?sup=boss">Chefin Boss</a>' in html
        assert 'href="/my-tasks?sup=kai">Kai Schulz</a>' in html
        kai_only = anna.get("/my-tasks?sup=kai").text
        assert "task-t2" in kai_only and "task-t1" not in kai_only
        assert 'href="/my-tasks?sup=kai" aria-current="page"' in kai_only
        # Combined with a status filter; links keep the other filter.
        combined = anna.get("/my-tasks?filter=open&sup=kai").text
        assert "Hier ist gerade nichts offen." in combined
        assert 'href="/my-tasks?filter=ok&amp;sup=kai"' in combined
        # Unknown Fachbetreuer values are ignored.
        assert "task-t1" in anna.get("/my-tasks?sup=ghost").text


def test_supervisor_filter_hidden_with_single_supervisor(config):
    text = config.paths.users.read_text().replace("        tasks:\n          t2: kai\n", "")
    config.paths.users.write_text(text, encoding="utf-8")
    with open_client(config, "anna") as anna:
        html = anna.get("/my-tasks").text
    assert 'aria-label="Fachbetreuer"' not in html


def test_trainer_all_scope_with_supervisor_filter(config):
    add_user(config, "ben", "apprentice", name="Ben Azubi")  # no Fachbetreuer assigned
    with open_client(config, "boss") as boss:
        mine = boss.get("/my-tasks").text
        assert '<a href="/my-tasks" aria-current="page">Meine</a>' in mine
        assert "Ben Azubi" not in mine and 'aria-label="Fachbetreuer"' not in mine

        everyone = boss.get("/my-tasks?scope=all").text
        assert '<a href="/my-tasks?scope=all" aria-current="page">Alle Fachbetreuer</a>' in everyone
        assert "Anna Azubi · Demo" in everyone and "Ben Azubi · Demo" in everyone
        assert 'href="/my-tasks?sup=kai&amp;scope=all">Kai Schulz</a>' in everyone
        assert 'href="/my-tasks?sup=-&amp;scope=all">ohne Fachbetreuer</a>' in everyone

        kai = boss.get("/my-tasks?scope=all&sup=kai").text  # Vertretung für Kai
        assert 'href="/workbooks/demo/users/anna#task-t2"' in kai
        assert "task-t1" not in kai and "Ben Azubi" not in kai

        nobody = boss.get("/my-tasks?scope=all&sup=-").text
        assert "Ben Azubi · Demo" in nobody and "Anna Azubi" not in nobody


def test_apprentice_cannot_use_all_scope(config):
    with open_client(config, "anna") as anna:
        html = anna.get("/my-tasks?scope=all").text
    assert "Alle Fachbetreuer" not in html
    assert "Anna Azubi ·" not in html  # still only her own workbooks
