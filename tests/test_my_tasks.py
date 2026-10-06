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
