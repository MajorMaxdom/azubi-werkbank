"""Question/answer thread per task ("Rückfragen") and the review history."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import BASE_URL, add_user, make_config, open_client, second_client
from tests.test_progress import CATALOG

API = "/api/progress/demo"
JSON_HEADERS = {"Origin": BASE_URL, "X-Workbook": "1"}
OWN = f"{API}/tasks/t1/comments"  # apprentice: own thread
ANNA = f"{API}/users/anna/tasks/t1/comments"  # Fachbetreuer: anna's thread
REVIEW = f"{API}/users/anna/tasks/t1/review"


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
    text = config.paths.users.read_text().replace(
        "  anna:\n    name: Anna Azubi\n    role: apprentice\n",
        "  anna:\n    name: Anna Azubi\n    role: apprentice\n    supervisors:\n      demo:\n"
        "        default: boss\n",
    )
    config.paths.users.write_text(text, encoding="utf-8")
    return config


def post(client, url, body):
    return client.post(url, json=body, headers=JSON_HEADERS)


def patch(client, url, body):
    return client.patch(url, json=body, headers=JSON_HEADERS)


def stored(config, user="anna") -> dict:
    return json.loads((config.paths.progress / "demo" / f"{user}.json").read_text())


def task_html(html: str, task_id: str) -> str:
    match = re.search(rf'<article class="task" id="task-{task_id}".*?</article>', html, re.S)
    assert match, f"task {task_id} not rendered"
    return match.group(0)


# ------------------------------------------------------------------ thread


def test_thread_in_both_directions(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        response = post(anna, OWN, {"text": "  Wie ist Aufgabe 1 gemeint?\nZeile 2  "})
        assert response.status_code == 200
        data = response.json()
        assert data["comment"]["author"] == "anna"
        assert data["comment"]["role"] == "apprentice"
        assert data["comment"]["text"] == "Wie ist Aufgabe 1 gemeint?\nZeile 2"  # trimmed
        assert data["author_name"] == "Anna Azubi"
        assert re.fullmatch(r"\d\d\.\d\d\.\d{4} \d\d:\d\d", data["at_text"])

        reply = post(boss, ANNA, {"text": "Lies die Aufgabenstellung noch einmal."})
        assert reply.status_code == 200
        assert reply.json()["comment"]["role"] == "trainer"
        assert reply.json()["comment"]["author"] == "boss"
        assert reply.json()["author_name"] == "Chefin Boss"

        comments = stored(config)["tasks"]["t1"]["comments"]
        assert [(c["author"], c["role"]) for c in comments] == [
            ("anna", "apprentice"),
            ("boss", "trainer"),
        ]
        # Posting a message is not a change of the answers (no re-check needed).
        assert stored(config)["tasks"]["t1"]["updated_at"] is None

        own = task_html(anna.get("/workbooks/demo").text, "t1")
        assert "Wie ist Aufgabe 1 gemeint?\nZeile 2" in own
        assert "Lies die Aufgabenstellung noch einmal." in own
        assert "Chefin Boss" in own and "Anna Azubi" in own
        assert f'data-thread-url="{OWN}"' in own
        assert 'class="thread has-comments"' in own

        review = task_html(boss.get("/workbooks/demo/users/anna").text, "t1")
        trainer_area = review[review.index('<div class="trainer">') :]
        assert "Wie ist Aufgabe 1 gemeint?" in trainer_area  # thread inside trainer area
        assert f'data-thread-url="{ANNA}"' in trainer_area

        # Every task has a form, but only the answered thread is listed.
        assert f'data-thread-url="{API}/tasks/t2/comments"' in anna.get("/workbooks/demo").text


def test_messages_are_escaped(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        assert post(anna, OWN, {"text": "<script>alert(1)</script>"}).status_code == 200
        assert post(boss, ANNA, {"text": "<img src=x onerror=alert(2)>"}).status_code == 200
        for html in (
            anna.get("/workbooks/demo").text,
            boss.get("/workbooks/demo/users/anna").text,
            anna.get("/workbooks/demo/export").text,
        ):
            assert "<script>alert(1)" not in html and "<img src=x" not in html
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
            assert "&lt;img src=x onerror=alert(2)&gt;" in html


@pytest.mark.parametrize(
    "body",
    [
        {"text": ""},
        {"text": "   \n "},
        {"text": "x" * 5001},
        {},
        {"text": 5},
        {"text": "ok", "author": "boss"},
        ["text"],
        None,
    ],
)
def test_invalid_messages_are_rejected(config, body):
    with open_client(config, "anna") as anna:
        assert anna.post(OWN, json=body, headers=JSON_HEADERS).status_code == 422
    assert not (config.paths.progress / "demo" / "anna.json").exists()


def test_size_limits_and_unknown_task(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        assert post(anna, OWN, {"text": "x" * 5000}).status_code == 200
        assert post(anna, f"{API}/tasks/nope/comments", {"text": "Hallo"}).status_code == 422
        unknown = post(boss, f"{API}/users/anna/tasks/nope/comments", {"text": "Hi"})
        assert unknown.status_code == 422
        huge = anna.post(
            OWN,
            content=json.dumps({"text": "x" * 1_100_000}),
            headers={**JSON_HEADERS, "Content-Type": "application/json"},
        )
        assert huge.status_code == 413


def test_access_rules(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        ben = second_client(boss, "ben")
        post(anna, OWN, {"text": "Frage von Anna"})
        # Apprentices cannot use the Fachbetreuer URL, not even for themselves.
        assert post(anna, f"{API}/users/ben/tasks/t1/comments", {"text": "x"}).status_code == 403
        assert post(anna, ANNA, {"text": "x"}).status_code == 403
        # Fachbetreuer cannot use the apprentice URL.
        assert post(boss, OWN, {"text": "x"}).status_code == 403
        # Unknown or non-apprentice targets.
        assert post(boss, f"{API}/users/ghost/tasks/t1/comments", {"text": "x"}).status_code == 404
        assert post(boss, f"{API}/users/kai/tasks/t1/comments", {"text": "x"}).status_code == 404
        assert post(boss, f"{API}/users/..%2Fx/tasks/t1/comments", {"text": "x"}).status_code in (
            404,
            422,
        )
        # Ben's own thread is separate; he never sees Anna's messages.
        assert post(ben, OWN, {"text": "Frage von Ben"}).status_code == 200
        assert "Frage von Anna" not in ben.get("/workbooks/demo").text
        assert "Frage von Ben" not in anna.get("/workbooks/demo").text
        assert [c["text"] for c in stored(config)["tasks"]["t1"]["comments"]] == ["Frage von Anna"]
        # JSON API CSRF rules.
        assert anna.post(OWN, json={"text": "x"}, headers={"Origin": BASE_URL}).status_code == 403
        evil = {"Origin": "https://evil.example", "X-Workbook": "1"}
        assert anna.post(OWN, json={"text": "x"}, headers=evil).status_code == 403
        assert anna.get(OWN, headers=JSON_HEADERS).status_code == 405
    with open_client(config) as anonymous:
        assert post(anonymous, OWN, {"text": "x"}).status_code == 401


def test_trainer_preview_has_no_thread_form(config):
    with open_client(config, "boss") as boss:
        html = boss.get("/workbooks/demo").text
    assert "data-thread" not in html
    assert "/static/js/comments.js" in html


# ------------------------------------------------------------------ open questions


def test_open_question_badge_and_filter(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        assert "Hier ist gerade nichts offen." in boss.get("/my-tasks?filter=open").text
        assert "zu prüfen" not in boss.get("/").text

        post(anna, OWN, {"text": "Ich komme nicht weiter."})  # t1 is not even done
        open_rows = boss.get("/my-tasks?filter=open").text
        assert 'href="/workbooks/demo/users/anna#task-t1"' in open_rows
        assert "task-t2" not in open_rows
        assert 'class="state state-question"' in open_rows and ">Rückfrage</span>" in open_rows
        assert "1 Aufgabe zu prüfen" in boss.get("/").text
        assert ">Rückfrage</span>" in boss.get("/my-tasks?scope=all").text
        # The apprentice gets no badge for her own question.
        assert "state-question" not in anna.get("/my-tasks").text
        assert "state-answer" not in anna.get("/my-tasks").text

        # A done task waiting for review AND an open question counts once.
        patch(anna, f"{API}/tasks/t1", {"done": True})
        assert "1 Aufgabe zu prüfen" in boss.get("/").text

        patch(boss, REVIEW, {"status": "ok"})
        post(boss, ANNA, {"text": "Siehe Hinweis im Heft."})
        assert "Hier ist gerade nichts offen." in boss.get("/my-tasks?filter=open").text
        assert "zu prüfen" not in boss.get("/").text
        assert "state-question" not in boss.get("/my-tasks").text

        mine = anna.get("/my-tasks").text
        assert 'class="state state-answer"' in mine and ">Antwort</span>" in mine

        post(anna, OWN, {"text": "Danke!"})
        assert "state-answer" not in anna.get("/my-tasks").text
        assert "task-t1" in boss.get("/my-tasks?filter=open").text


def test_open_question_counts_on_index_only_for_responsible(config):
    with open_client(config, "kai") as kai:
        anna = second_client(kai, "anna")
        post(anna, OWN, {"text": "Frage"})
        # Kai is not anna's Fachbetreuer: nothing on his start page, but visible in "Alle".
        assert "Anna Azubi" not in kai.get("/").text.split("Alle Arbeitshefte")[0]
        assert "Rückfrage" in kai.get("/my-tasks?scope=all&filter=open").text


# ------------------------------------------------------------------ review history


def test_history_only_on_real_changes(config):
    with open_client(config, "boss") as boss:
        kai = second_client(boss, "kai")
        anna = second_client(boss, "anna")
        patch(anna, f"{API}/tasks/t1", {"done": True})

        def history():
            return stored(config)["tasks"]["t1"]["review_history"]

        patch(boss, REVIEW, {"status": "redo", "comment": "Mehr"})
        assert history() == []
        patch(boss, REVIEW, {"status": "redo", "comment": "Mehr"})  # unchanged
        assert history() == []
        # Autosave while typing: same Fachbetreuer, same status -> same review.
        patch(boss, REVIEW, {"status": "redo", "comment": "Mehr Details"})
        assert history() == []
        assert stored(config)["tasks"]["t1"]["review"]["comment"] == "Mehr Details"

        patch(boss, REVIEW, {"status": "ok", "comment": "Mehr Details"})  # status changed
        assert [(h["status"], h["comment"]) for h in history()] == [("redo", "Mehr Details")]
        assert history()[0]["reviewed_by"] == "boss"

        patch(kai, REVIEW, {"status": "ok", "comment": "Passt"})  # another Fachbetreuer
        assert [h["comment"] for h in history()] == ["Mehr Details", "Mehr Details"]
        assert stored(config)["tasks"]["t1"]["review"]["reviewed_by"] == "kai"

        # An older review is kept even when only the comment changes.
        path = config.paths.progress / "demo" / "anna.json"
        data = json.loads(path.read_text())
        old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        data["tasks"]["t1"]["review"]["reviewed_at"] = old
        path.write_text(json.dumps(data))
        patch(kai, REVIEW, {"status": "ok", "comment": "Passt jetzt"})
        assert [h["comment"] for h in history()][-1] == "Passt"

        patch(kai, REVIEW, {"status": None, "comment": ""})  # cleared
        assert stored(config)["tasks"]["t1"]["review"] is None
        assert [h["comment"] for h in history()][-1] == "Passt jetzt"
        assert len(history()) == 4
        patch(kai, REVIEW, {"status": None, "comment": ""})  # nothing to keep
        assert len(history()) == 4


def test_history_shown_everywhere_without_trainer_content(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        patch(anna, f"{API}/tasks/t1", {"done": True})
        patch(boss, REVIEW, {"status": "redo", "comment": "Erste <b>Runde</b>"})
        patch(boss, REVIEW, {"status": "ok", "comment": "Jetzt gut"})
        post(anna, OWN, {"text": "Frage im Export"})
        post(boss, ANNA, {"text": "Antwort im Export"})

        review = task_html(boss.get("/workbooks/demo/users/anna").text, "t1")
        own = task_html(anna.get("/workbooks/demo").text, "t1")
        apprentice_export = anna.get("/workbooks/demo/export").text
        trainer_export = boss.get("/workbooks/demo/export?user=anna").text
        for html in (review, own, apprentice_export, trainer_export):
            assert "Bewertungsverlauf (1)" in html
            assert "Erste &lt;b&gt;Runde&lt;/b&gt;" in html
            assert "Chefin Boss am" in html
            assert "Frage im Export" in html and "Antwort im Export" in html

        # Review view: history inside the trainer area, below the review.
        trainer_area = review[review.index('<div class="trainer">') :]
        assert trainer_area.index("Zuletzt bewertet") < trainer_area.index("Bewertungsverlauf")
        # Apprentice view: below the review note.
        assert own.index("review-note") < own.index("Bewertungsverlauf")
        assert '<details class="review-history">' in own  # collapsed on screen

        for html in (apprentice_export, trainer_export):
            assert '<details class="review-history" open>' in html  # expanded in the export
            assert "data-thread-url" not in html and '<textarea id="thread-' not in html
            assert "<script" not in html

        apprentice_pages = (
            anna.get("/workbooks/demo").text,
            apprentice_export,
            anna.get("/my-tasks").text,
        )
        for html in apprentice_pages:
            assert "GEHEIM" not in html and "NOTIZ-X" not in html
        assert "GEHEIM" in trainer_export and "NOTIZ-X" in review


def test_history_newest_first(config):
    with open_client(config, "boss") as boss:
        kai = second_client(boss, "kai")
        patch(boss, REVIEW, {"status": "redo", "comment": "eins"})
        patch(kai, REVIEW, {"status": "redo", "comment": "zwei"})
        patch(boss, REVIEW, {"status": "ok", "comment": "drei"})
        html = task_html(boss.get("/workbooks/demo/users/anna").text, "t1")
    history = html[html.index("review-history") :]
    assert "Bewertungsverlauf (2)" in history
    assert history.index("zwei") < history.index("eins")


def test_old_progress_files_still_load(config):
    path = config.paths.progress / "demo" / "anna.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workbook": "demo",
                "user": "anna",
                "tasks": {
                    "t1": {
                        "done": True,
                        "review": {"status": "ok", "comment": "alt", "reviewed_by": "boss",
                                   "reviewed_at": "2026-10-01T10:00:00Z"},
                    }
                },
            }
        )
    )  # fmt: skip
    with open_client(config, "anna") as anna:
        html = anna.get("/workbooks/demo").text
        assert "alt" in html and "Bewertungsverlauf" not in html
        assert post(anna, OWN, {"text": "Neu"}).status_code == 200
    data = json.loads(path.read_text())
    assert data["tasks"]["t1"]["review"]["comment"] == "alt"
    assert data["tasks"]["t1"]["review_history"] == []
    assert [c["text"] for c in data["tasks"]["t1"]["comments"]] == ["Neu"]
