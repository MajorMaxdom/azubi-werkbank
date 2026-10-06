"""Bulk Fachbetreuer assignment (/admin/assign) and CSV exports
(/admin/overview.csv, /my-tasks.csv)."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime

import pytest
from ruamel.yaml import YAML

from tests.conftest import BASE_URL, PASSWORD, csrf_from, make_config, open_client, second_client

JSON_HEADERS = {"Origin": BASE_URL, "X-Workbook": "1"}
ORIGIN = {"Origin": BASE_URL}

CATALOG = """\
schema_version: 1
workbook:
  id: demo
  version: 1.0.0
  title: Demo
levels:
  u: {label: Verstehen, color: blue}
days:
  - id: day-1
    title: Eins
    modules:
      - code: M-01
        title: Grundlagen
        tasks:
          - id: t1
            title: Aufgabe eins
            level: u
            answers: [{id: a1}]
            trainer:
              expectations: [GEHEIM-ERWARTUNG]
              notes: GEHEIM-NOTIZ
          - id: t2
            title: Aufgabe zwei
            level: u
            answers: [{id: a1}]
      - code: LB-02
        title: Begriffe
        tasks:
          - id: t3
            title: "-Aufgabe drei"
            level: u
            answers: [{id: a1}]
"""

OTHER = CATALOG.replace("id: demo", "id: other").replace("title: Demo", "title: Anderes")

USERS = """\
# Nutzerliste – von Hand gepflegt
users:
  boss:
    name: Chefin Boss
    role: trainer
  kai:
    name: Kai Schulz   # Netzwerk
    role: trainer
  anna:
    name: Anna Azubi
    role: apprentice
    supervisors:
      demo:
        default: boss
        tasks:
          t1: kai   # Sonderfall
  ben:
    name: Ben Berg
    role: apprentice
  cora:
    name: Cora Clever
    role: apprentice
    supervisors:
      other:
        default: kai
  dave:
    name: Dave Dorn
    role: apprentice
    workbooks: [other]
"""


@pytest.fixture
def config(tmp_path):
    from app.auth import CredentialStore, hash_password

    config = make_config(tmp_path)
    (config.paths.workbooks / "demo.yaml").write_text(CATALOG, encoding="utf-8")
    (config.paths.workbooks / "other.yaml").write_text(OTHER, encoding="utf-8")
    config.paths.users.write_text(USERS, encoding="utf-8")
    store = CredentialStore(config.paths.data / "credentials.json")
    for name in ("boss", "kai", "anna", "ben", "cora", "dave"):

        def change(cred):
            cred.password_hash = hash_password(PASSWORD)

        store.update(name, change)
    return config


def users_data(config) -> dict:
    return YAML(typ="safe").load(config.paths.users.read_text(encoding="utf-8"))["users"]


def post_assign(client, data: dict, headers=ORIGIN):
    token = csrf_from(client.get("/admin/assign?workbook=demo").text)
    return client.post("/admin/assign", data={"csrf_token": token, **data}, headers=headers)


def parse_csv(response) -> list[list[str]]:
    body = response.content
    assert body.startswith(b"\xef\xbb\xbf")
    text = body.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=";"))


HEADER = ["Azubi", "Benutzername", "Heft", "Modul", "Nr.", "Aufgabe", "Status", "Erledigt am",
          "Bewertet von", "Bewertet am", "Kommentar", "Fachbetreuer"]  # fmt: skip


# --------------------------------------------------------------------------- bulk assignment


def test_assign_page_lists_allowed_apprentices_and_modules(config):
    with open_client(config, "boss") as boss:
        assert 'href="/admin/assign"' in boss.get("/admin/users").text
        html = boss.get("/admin/assign?workbook=demo").text
        assert "Fachbetreuer zuordnen" in html
        for name in ("Anna Azubi", "Ben Berg", "Cora Clever"):
            assert name in html
        assert "Dave Dorn" not in html  # may not open demo
        assert 'name="module:M-01"' in html and 'name="module:LB-02"' in html
        assert "— unverändert —" in html and "wie Heft" in html and "niemand" in html
        assert "(+ 1 Aufgabe abweichend)" in html  # anna's override
        default_page = boss.get("/admin/assign").text  # first workbook preselected
        assert 'name="module:M-01"' in default_page
        unknown = boss.get("/admin/assign?workbook=nope")
        assert unknown.status_code == 404 and "Dieses Heft gibt es nicht." in unknown.text


def test_bulk_default_and_module_override_in_one_write(config, monkeypatch):
    from app.auth import UserDirectory

    with open_client(config, "boss") as boss:
        writes = []
        original = UserDirectory._modify

        def counting(self, change):
            writes.append(1)
            return original(self, change)

        monkeypatch.setattr(UserDirectory, "_modify", counting)
        response = post_assign(
            boss,
            {"workbook": "demo", "users": ["ben", "cora"], "default": "boss",
             "module:LB-02": "kai", "module:M-01": ""},
        )  # fmt: skip
        assert response.status_code == 200
        assert "2 Azubis aktualisiert." in response.text
        assert len(writes) == 1
        users = users_data(config)
        for name in ("ben", "cora"):
            assert users[name]["supervisors"]["demo"] == {"default": "boss", "tasks": {"t3": "kai"}}
        assert users["cora"]["supervisors"]["other"] == {"default": "kai"}  # untouched
        assert users["anna"]["supervisors"]["demo"] == {"default": "boss", "tasks": {"t1": "kai"}}
        # The app sees the change immediately.
        assert boss.app.state.users.get("ben").supervisor_for("demo", "t3") == "kai"


def test_comments_survive_bulk_assignment(config):
    with open_client(config, "boss") as boss:
        response = post_assign(boss, {"workbook": "demo", "users": ["anna"], "module:LB-02": "kai"})
        assert "1 Azubi aktualisiert." in response.text
        text = config.paths.users.read_text(encoding="utf-8")
        assert "# Nutzerliste – von Hand gepflegt" in text
        assert "# Netzwerk" in text and "# Sonderfall" in text
        assert users_data(config)["anna"]["supervisors"]["demo"] == {
            "default": "boss",
            "tasks": {"t1": "kai", "t3": "kai"},
        }


def test_same_as_workbook_removes_overrides_and_unchanged_keeps(config):
    with open_client(config, "boss") as boss:
        post_assign(boss, {"workbook": "demo", "users": ["anna"], "module:LB-02": "kai"})
        response = post_assign(
            boss,
            {"workbook": "demo", "users": ["anna"], "default": "", "module:M-01": "=",
             "module:LB-02": ""},
        )  # fmt: skip
        assert response.status_code == 200
        demo = users_data(config)["anna"]["supervisors"]["demo"]
        assert demo == {"default": "boss", "tasks": {"t3": "kai"}}  # t1 gone, t3 + default kept


def test_nobody_and_overrides_equal_to_default_are_dropped(config):
    with open_client(config, "boss") as boss:
        post_assign(boss, {"workbook": "demo", "users": ["anna"], "default": "kai"})
        # t1: kai equals the new default -> dropped
        assert users_data(config)["anna"]["supervisors"]["demo"] == {"default": "kai"}
        post_assign(boss, {"workbook": "demo", "users": ["anna", "cora"], "default": "-"})
        users = users_data(config)
        assert "supervisors" not in users["anna"]
        assert "demo" not in users["cora"]["supervisors"]
        assert users["cora"]["supervisors"]["other"] == {"default": "kai"}


@pytest.mark.parametrize(
    "data, message",
    [
        ({"workbook": "demo", "users": ["ben"], "default": "ghost"}, "Fachbetreuer ist unbekannt"),
        ({"workbook": "demo", "users": ["ben"], "default": "anna"}, "Fachbetreuer ist unbekannt"),
        ({"workbook": "demo", "users": ["ben"], "module:M-01": "-"}, "Fachbetreuer ist unbekannt"),
        ({"workbook": "demo", "users": ["ben", "ghost"], "default": "kai"}, "existiert nicht"),
        ({"workbook": "demo", "users": ["ben", "kai"], "default": "kai"}, "existiert nicht"),
        ({"workbook": "demo", "users": ["dave"], "default": "kai"}, "existiert nicht"),
        ({"workbook": "demo", "users": ["../x"], "default": "kai"}, "existiert nicht"),
        ({"workbook": "nope", "users": ["ben"], "default": "kai"}, "Dieses Heft gibt es nicht."),
        ({"workbook": "demo", "default": "kai"}, "mindestens einen Azubi"),
        ({"workbook": "demo", "users": ["ben"], "default": ""}, "keine Änderung"),
    ],
)
def test_invalid_input_is_rejected_without_writing(config, data, message):
    with open_client(config, "boss") as boss:
        before = config.paths.users.read_text(encoding="utf-8")
        response = post_assign(boss, data)
        assert response.status_code == 400
        assert message in response.text
        assert config.paths.users.read_text(encoding="utf-8") == before


def test_assign_requires_trainer_csrf_and_origin(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        assert anna.get("/admin/assign").status_code == 403
        token = csrf_from(anna.get("/my-tasks").text)
        denied = anna.post(
            "/admin/assign",
            data={"csrf_token": token, "workbook": "demo", "users": ["anna"], "default": "kai"},
            headers=ORIGIN,
        )
        assert denied.status_code == 403
        before = config.paths.users.read_text(encoding="utf-8")
        data = {"workbook": "demo", "users": ["ben"], "default": "kai"}
        assert boss.post("/admin/assign", data=data, headers=ORIGIN).status_code == 403
        assert post_assign(boss, data, headers={}).status_code == 403
        assert (
            post_assign(boss, data, headers={"Origin": "https://evil.example"}).status_code == 403
        )
        assert config.paths.users.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- csv


def test_overview_csv_format_and_content(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        anna.patch("/api/progress/demo/tasks/t1", json={"done": True}, headers=JSON_HEADERS)
        review = {"status": "redo", "comment": '=HYPERLINK("http://evil","x")'}
        boss.patch(
            "/api/progress/demo/users/anna/tasks/t1/review", json=review, headers=JSON_HEADERS
        )
        overview = boss.get("/admin/overview").text
        assert 'href="/admin/overview.csv"' in overview
        assert 'href="/admin/overview.csv?workbook=demo"' in overview

        response = boss.get("/admin/overview.csv?workbook=demo")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert response.headers["content-disposition"] == (
            f'attachment; filename="uebersicht_demo_{today}.csv"'
        )
        assert b"\r\n" in response.content and b";" in response.content
        assert b"\n" not in response.content.replace(b"\r\n", b"")
        rows = parse_csv(response)
        assert rows[0] == HEADER
        body = rows[1:]
        # anna, ben, cora (active, may open demo) x 3 tasks; dave not.
        assert len(body) == 9
        assert {r[1] for r in body} == {"anna", "ben", "cora"}
        assert {r[2] for r in body} == {"Demo"}
        t1 = next(r for r in body if r[1] == "anna" and r[4] == "1.1")
        date_de = datetime.now().astimezone().strftime("%d.%m.%Y")
        assert t1[0] == "Anna Azubi" and t1[3] == "M-01" and t1[5] == "Aufgabe eins"
        assert t1[6] == "Nacharbeiten"
        assert t1[7] == date_de and t1[9] == date_de
        assert re.fullmatch(r"\d\d\.\d\d\.\d{4}", t1[7])
        assert t1[8] == "Chefin Boss"
        assert t1[10] == '\'=HYPERLINK("http://evil","x")'
        assert t1[11] == "Kai Schulz"
        t3 = next(r for r in body if r[1] == "anna" and r[3] == "LB-02")
        assert t3[5] == "'-Aufgabe drei"  # formula escaping also for catalog text
        assert t3[11] == "Chefin Boss" and t3[7] == "" and t3[6] == "offen"
        assert "GEHEIM" not in response.content.decode("utf-8")


def test_overview_csv_all_workbooks_and_unknown(config):
    with open_client(config, "boss") as boss:
        response = boss.get("/admin/overview.csv")
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert f'filename="uebersicht_alle_{today}.csv"' in response.headers["content-disposition"]
        rows = parse_csv(response)[1:]
        assert {r[2] for r in rows} == {"Demo", "Anderes"}
        assert len(rows) == 9 + 12  # other: anna, ben, cora, dave
        assert boss.get("/admin/overview.csv?workbook=nope").status_code == 404
        assert boss.get("/admin/overview.csv?workbook=../x").status_code == 404


def test_overview_csv_forbidden_for_apprentices(config):
    with open_client(config, "anna") as anna:
        assert anna.get("/admin/overview.csv").status_code == 403
        assert anna.get("/admin/overview.csv?workbook=demo").status_code == 403


def test_my_tasks_csv_matches_view_and_filters(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        anna.patch("/api/progress/demo/tasks/t1", json={"done": True}, headers=JSON_HEADERS)
        boss.patch(
            "/api/progress/demo/users/anna/tasks/t1/review",
            json={"status": "ok", "comment": "+gut"},
            headers=JSON_HEADERS,
        )
        html = anna.get("/my-tasks?filter=ok").text
        assert 'href="/my-tasks.csv?filter=ok"' in html

        response = anna.get("/my-tasks.csv")
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert response.headers["content-disposition"] == (
            f'attachment; filename="meine_aufgaben_{today}.csv"'
        )
        rows = parse_csv(response)
        assert rows[0] == HEADER
        assert len(rows) == 1 + 6  # demo + other, three tasks each
        assert {r[1] for r in rows[1:]} == {"anna"}
        text = response.content.decode("utf-8")
        assert "GEHEIM" not in text

        ok_rows = parse_csv(anna.get("/my-tasks.csv?filter=ok"))[1:]
        assert len(ok_rows) == 1
        assert ok_rows[0][5] == "Aufgabe eins" and ok_rows[0][6] == "geprüft – OK"
        assert ok_rows[0][10] == "'+gut"

        # Fachbetreuer filter: only tasks of kai (anna: t1 in demo).
        kai_rows = parse_csv(anna.get("/my-tasks.csv?sup=kai"))[1:]
        assert [(r[2], r[4]) for r in kai_rows] == [("Demo", "1.1")]
        none_rows = parse_csv(anna.get("/my-tasks.csv?sup=-"))[1:]
        assert {r[2] for r in none_rows} == {"Anderes"}

        # Trainer: own responsibilities vs. scope=all.
        mine = parse_csv(boss.get("/my-tasks.csv"))[1:]
        assert [(r[1], r[4]) for r in mine] == [("anna", "1.2"), ("anna", "1.3")]
        everyone = parse_csv(boss.get("/my-tasks.csv?scope=all"))[1:]
        assert len(everyone) == 9 + 12  # demo: anna, ben, cora; other: all four
        assert 'href="/my-tasks.csv?scope=all"' in boss.get("/my-tasks?scope=all").text


def test_my_tasks_csv_requires_login(config):
    with open_client(config) as client:
        assert client.get("/my-tasks.csv", follow_redirects=False).status_code == 303


def test_safe_cell():
    from app.csv_export import safe_cell

    for value in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
        assert safe_cell(value) == "'" + value
    assert safe_cell("Normal") == "Normal"
    assert safe_cell(None) == ""
    assert safe_cell("a=b") == "a=b"
