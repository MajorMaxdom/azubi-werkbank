from __future__ import annotations

import json
import re
import stat
import time
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from app.auth import (
    AccountService,
    CredentialStore,
    IpRateLimiter,
    UserDirectory,
    safe_next,
    suggest_username,
    utcnow,
)
from app.models.users import User
from cli import app as cli_app
from tests.conftest import (
    BASE_URL,
    ORIGIN,
    PASSWORD,
    add_user,
    csrf_from,
    login,
    make_config,
    open_client,
    replace_cookie,
    second_client,
)
from tests.test_loader import VALID_YAML


@pytest.fixture
def config(tmp_path):
    config = make_config(tmp_path)
    add_user(config, "boss", "trainer", name="Chefin Boss")
    add_user(config, "azubi", "apprentice", name="Max Azubi")
    (config.paths.workbooks / "demo.yaml").write_text(VALID_YAML, encoding="utf-8")
    return config


def accounts_for(config) -> AccountService:
    directory = UserDirectory(config.paths.users)
    directory.load()
    return AccountService(
        config, directory, CredentialStore(config.paths.data / "credentials.json")
    )


def credentials(config) -> dict:
    return json.loads((config.paths.data / "credentials.json").read_text())["users"]


def post_form(client, url: str, data: dict | None = None, page: str = "/admin/users", **kw):
    token = csrf_from(client.get(page).text)
    return client.post(url, data={**(data or {}), "csrf_token": token}, headers=ORIGIN, **kw)


# ------------------------------------------------------------------ login / logout


def test_pages_require_login(config):
    with open_client(config) as client:
        for path in ["/", "/workbooks/demo", "/admin/catalogs", "/admin/users"]:
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/login?next=")
        assert client.get("/workbooks/demo/theme.css", follow_redirects=False).status_code == 303
        assert client.get("/events").status_code == 401
        assert client.get("/static/css/tokens.css").status_code == 200


def test_login_success_sets_secure_cookie_and_redirects(config):
    with open_client(config) as client:
        response = login(client, "azubi")
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-session=")
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            assert flag.lower() in cookie.lower()
        page = client.get("/").text
        assert "Max Azubi" in page and "Abmelden" in page


def test_login_redirects_to_next(config):
    with open_client(config) as client:
        token = csrf_from(client.get("/login?next=/workbooks/demo").text)
        response = client.post(
            "/login",
            data={"username": "azubi", "password": PASSWORD, "csrf_token": token,
                  "next": "/workbooks/demo"},
            headers=ORIGIN,
            follow_redirects=False,
        )  # fmt: skip
    assert response.headers["location"] == "/workbooks/demo"


@pytest.mark.parametrize(
    ("target", "expected"),
    [("/x", "/x"), ("//evil.com", "/"), ("https://evil.com", "/"), ("/\\evil", "/"), ("", "/"),
     ("/a\nb", "/")],
)  # fmt: skip
def test_safe_next(target, expected):
    assert safe_next(target) == expected


def test_login_failure_same_message_for_unknown_user(config, caplog):
    with open_client(config) as client:
        wrong = login(client, "azubi", "falsches-passwort-123")
        unknown = login(client, "niemand", "falsches-passwort-123")
        invalid = login(client, "../../etc", "x")
    assert wrong.status_code == unknown.status_code == invalid.status_code == 401
    message = "Anmeldung fehlgeschlagen. Bitte Benutzername und Passwort prüfen."
    for response in (wrong, unknown, invalid):
        assert message in response.text
    lines = [r.getMessage() for r in caplog.records if "login_failed" in r.getMessage()]
    assert "auth.login_failed user=azubi ip=testclient" in lines
    assert "auth.login_failed user=- ip=testclient" in lines  # invalid name not logged verbatim
    assert not any(PASSWORD in r.getMessage() or "falsches" in r.getMessage()
                   for r in caplog.records)  # fmt: skip


def test_login_is_case_insensitive_for_username(config):
    with open_client(config) as client:
        assert login(client, "  AZUBI ").status_code == 303


def test_lockout_after_max_attempts(config):
    with open_client(config) as client:
        for _ in range(5):
            assert login(client, "azubi", "falsch-falsch-falsch").status_code == 401
        assert credentials(config)["azubi"]["locked_until"] is not None
        # Correct password is rejected while locked, with the same message.
        locked = login(client, "azubi")
        assert locked.status_code == 401
        assert "Anmeldung fehlgeschlagen" in locked.text


def test_lockout_expires(config):
    store = CredentialStore(config.paths.data / "credentials.json")

    def expired(cred):
        cred.locked_until = utcnow() - timedelta(seconds=1)
        cred.failed_attempts = 0

    store.update("azubi", expired)
    with open_client(config) as client:
        assert login(client, "azubi").status_code == 303
    assert credentials(config)["azubi"]["locked_until"] is None


def test_ip_rate_limit(config):
    with open_client(config) as client:
        limiter: IpRateLimiter = client.app.state.login.limiter
        for _ in range(20):
            limiter.record_failure("testclient")
        assert login(client, "azubi").status_code == 401


def test_ip_rate_limiter_window():
    limiter = IpRateLimiter(limit=2, window=0.2)
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    assert limiter.blocked("1.2.3.4")
    assert not limiter.blocked("5.6.7.8")
    time.sleep(0.25)
    assert not limiter.blocked("1.2.3.4")


def test_x_forwarded_for_only_from_loopback():
    from starlette.requests import Request

    from app.auth import client_ip

    def req(peer, xff=None):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": (peer, 1)})

    assert client_ip(req("127.0.0.1", "203.0.113.9")) == "203.0.113.9"
    assert client_ip(req("127.0.0.1", "10.0.0.1, 203.0.113.9")) == "203.0.113.9"
    assert client_ip(req("100.64.0.9", "203.0.113.9")) == "100.64.0.9"  # spoofed, ignored
    assert client_ip(req("127.0.0.1", "nonsense")) == "127.0.0.1"


def test_logout_requires_post_and_clears_session(config):
    with open_client(config, "azubi") as client:
        assert client.get("/logout").status_code == 405
        response = post_form(client, "/logout", page="/", follow_redirects=False)
        assert response.status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 303


def test_tampered_session_cookie_rejected(config):
    with open_client(config, "azubi") as client:
        cookie = client.cookies.get("__Host-session")
        replace_cookie(client, "__Host-session", cookie[:-2] + "xx")
        assert client.get("/", follow_redirects=False).status_code == 303


def test_idle_timeout(config):
    with open_client(config, "azubi") as client:
        sessions = client.app.state.sessions
        payload = sessions.decode(client.cookies.get("__Host-session"))
        payload["last_seen"] -= 8 * 3600 + 1
        replace_cookie(client, "__Host-session", sessions._serializer.dumps(payload))
        assert client.get("/", follow_redirects=False).status_code == 303


def test_last_seen_refreshed_after_five_minutes(config):
    with open_client(config, "azubi") as client:
        sessions = client.app.state.sessions
        payload = sessions.decode(client.cookies.get("__Host-session"))
        assert client.get("/").headers.get("set-cookie") is None  # fresh: no refresh
        payload["last_seen"] -= 301
        replace_cookie(client, "__Host-session", sessions._serializer.dumps(payload))
        response = client.get("/")
        assert "__Host-session=" in response.headers["set-cookie"]


# ------------------------------------------------------------------ CSRF / Origin


def test_form_post_requires_origin_and_csrf(config):
    with open_client(config, "boss") as client:
        token = csrf_from(client.get("/admin/users").text)
        data = {"name": "Neu Ling", "role": "apprentice", "csrf_token": token}
        assert client.post("/admin/users", data=data).status_code == 403  # no Origin
        evil = {"Origin": "https://evil.example"}
        assert client.post("/admin/users", data=data, headers=evil).status_code == 403
        bad = {**data, "csrf_token": "0" * 64}
        assert client.post("/admin/users", data=bad, headers=ORIGIN).status_code == 403
        assert client.post("/admin/users", data=data, headers=ORIGIN).status_code == 200


def test_login_form_requires_csrf(config):
    with open_client(config) as client:
        client.get("/login")
        response = client.post(
            "/login", data={"username": "azubi", "password": PASSWORD}, headers=ORIGIN
        )
        assert response.status_code == 403
        assert "Sitzung ist abgelaufen" in response.text


# ------------------------------------------------------------------ invites


def test_invite_flow_single_use(config):
    with open_client(config, "boss") as client:
        response = post_form(client, "/admin/users", {"name": "Erika Müßig", "role": "apprentice"})
        assert response.status_code == 200
        link = re.search(r'value="(https://testserver/invite/[^"]+)"', response.text).group(1)
        assert "emuessig" in response.text
    path = link.removeprefix(BASE_URL)
    with open_client(config) as client:
        page = client.get(path)
        assert page.status_code == 200 and "Erika Müßig" in page.text
        token = csrf_from(page.text)
        short = client.post(
            path, data={"password": "kurz", "password_repeat": "kurz", "csrf_token": token},
            headers=ORIGIN,
        )  # fmt: skip
        assert short.status_code == 400 and "mindestens 12 Zeichen" in short.text
        mismatch = client.post(
            path,
            data={"password": PASSWORD, "password_repeat": PASSWORD + "x", "csrf_token": token},
            headers=ORIGIN,
        )
        assert mismatch.status_code == 400 and "stimmen nicht überein" in mismatch.text
        ok = client.post(
            path,
            data={"password": PASSWORD, "password_repeat": PASSWORD, "csrf_token": token},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert "Erika Müßig" in client.get("/").text  # logged in
        # Single use.
        assert client.get(path).status_code == 404
    assert credentials(config)["emuessig"]["invite"] is None
    with open_client(config) as client:
        assert login(client, "emuessig").status_code == 303


def test_invite_expiry(config):
    accounts = accounts_for(config)
    link = accounts.create_user("neu", "Neu", "apprentice")
    store = accounts.store

    def expire(cred):
        cred.invite.expires_at = utcnow() - timedelta(minutes=1)

    store.update("neu", expire)
    with open_client(config) as client:
        response = client.get(link.removeprefix(BASE_URL))
        assert response.status_code == 404
        assert "ungültig oder abgelaufen" in response.text
    assert accounts.status("neu") == "invite_expired"


def test_invite_token_only_stored_as_hash(config):
    link = accounts_for(config).create_user("neu", "Neu", "apprentice")
    token = link.rsplit("/", 1)[1]
    raw = (config.paths.data / "credentials.json").read_text()
    assert token not in raw
    assert len(token) >= 43  # 32 random bytes, URL-safe


def test_unknown_invite_token(config):
    with open_client(config) as client:
        assert client.get("/invite/abc").status_code == 404
        assert client.get("/invite/" + "a" * 500).status_code == 404


# ------------------------------------------------------------------ reset / deactivate


def test_reset_invalidates_sessions(config):
    with open_client(config, "boss") as trainer:
        apprentice = second_client(trainer, "azubi")
        assert apprentice.get("/", follow_redirects=False).status_code == 200
        response = post_form(trainer, "/admin/users/azubi/reset")
        assert response.status_code == 200 and "/invite/" in response.text
        assert apprentice.get("/", follow_redirects=False).status_code == 303
        assert login(apprentice, "azubi").status_code == 401  # password removed
    assert credentials(config)["azubi"]["password_hash"] is None


def test_deactivate_and_activate(config):
    with open_client(config, "boss") as trainer:
        apprentice = second_client(trainer, "azubi")
        response = post_form(trainer, "/admin/users/azubi/deactivate", follow_redirects=False)
        assert response.status_code == 303
        assert apprentice.get("/", follow_redirects=False).status_code == 303
        assert login(apprentice, "azubi").status_code == 401
        assert "active: false" in config.paths.users.read_text()
        assert "Deaktiviert" in trainer.get("/admin/users").text

        post_form(trainer, "/admin/users/azubi/activate")
        assert "active: false" not in config.paths.users.read_text()
        assert login(apprentice, "azubi").status_code == 303


def test_trainer_cannot_deactivate_self(config):
    with open_client(config, "boss") as trainer:
        response = post_form(trainer, "/admin/users/boss/deactivate")
        assert response.status_code == 400
        assert "eigenes Konto" in response.text
    assert "active: false" not in config.paths.users.read_text()


def test_user_removed_from_users_yaml_loses_session(config):
    with open_client(config, "azubi") as client:
        config.paths.users.write_text(
            "users:\n  boss:\n    name: Chefin Boss\n    role: trainer\n", encoding="utf-8"
        )
        client.app.state.users.load()
        assert client.get("/", follow_redirects=False).status_code == 303


# ------------------------------------------------------------------ roles & access


def test_apprentice_gets_403_on_admin(config):
    with open_client(config, "azubi") as client:
        assert client.get("/admin/users").status_code == 403
        assert client.get("/admin/catalogs").status_code == 403
        assert "Berechtigung" in client.get("/admin/users").text
        response = client.post("/admin/users/boss/reset", headers=ORIGIN)
        assert response.status_code == 403


def test_apprentice_workbook_restriction(config):
    (config.paths.workbooks / "other.yaml").write_text(
        VALID_YAML.replace("id: demo", "id: other"), encoding="utf-8"
    )
    add_user(config, "limited", "apprentice", workbooks=["demo"])
    with open_client(config, "limited") as client:
        index = client.get("/").text
        assert 'href="/workbooks/demo"' in index and 'href="/workbooks/other"' not in index
        assert client.get("/workbooks/demo").status_code == 200
        assert client.get("/workbooks/other").status_code == 404
        assert client.get("/workbooks/other/theme.css").status_code == 404
    with open_client(config, "boss") as client:
        assert client.get("/workbooks/other").status_code == 200


def test_trainer_sees_trainer_area_apprentice_does_not(config):
    with open_client(config, "boss") as client:
        assert "GEHEIM" not in client.get("/workbooks/demo").text  # VALID_YAML has no trainer
    text = VALID_YAML.replace(
        "            duration: 45m\n",
        "            duration: 45m\n            trainer:\n              expectations: [GEHEIM-X]\n",
    )
    (config.paths.workbooks / "demo.yaml").write_text(text, encoding="utf-8")
    with open_client(config, "boss") as client:
        assert "GEHEIM-X" in client.get("/workbooks/demo").text
    with open_client(config, "azubi") as client:
        html = client.get("/workbooks/demo").text
        assert "GEHEIM-X" not in html and "Bereich Fachbetreuer" not in html


@pytest.mark.parametrize("name", ["..", "..%2F..%2Fetc", "BOSS", "a_b", "%2e%2e"])
def test_username_path_traversal_rejected(config, name):
    with open_client(config, "boss") as client:
        response = post_form(client, f"/admin/users/{name}/reset")
        assert response.status_code in (404, 405)
    assert not (config.paths.data / f"{name}.json").exists()


def test_assets_require_login(config):
    assets = config.paths.workbooks / "assets"
    assets.mkdir()
    (assets / "a.png").write_bytes(b"\x89PNG")
    with open_client(config) as client:
        assert client.get("/assets/a.png", follow_redirects=False).status_code == 303
    with open_client(config, "azubi") as client:
        assert client.get("/assets/a.png").status_code == 200


# ------------------------------------------------------------------ users.yaml


def test_users_yaml_round_trip_keeps_comments(config):
    original = (
        "# Team list — maintained by hand\n"
        "users:\n"
        "  boss:  # the boss\n"
        "    name: Chefin Boss\n"
        "    role: trainer\n"
        "  # apprentices below\n"
        "  azubi:\n"
        "    name: Max Azubi\n"
        "    role: apprentice\n"
        "    workbooks: [demo]   # only demo\n"
    )
    config.paths.users.write_text(original, encoding="utf-8")
    accounts = accounts_for(config)
    accounts.create_user("neu", "Neu Ling", "apprentice", ["demo"])
    accounts.set_active("azubi", False)
    text = config.paths.users.read_text()
    for fragment in ("# Team list — maintained by hand", "# the boss", "# apprentices below",
                     "workbooks: [demo]   # only demo"):  # fmt: skip
        assert fragment in text
    assert "  neu:\n    name: Neu Ling\n    role: apprentice\n    workbooks: [demo]\n" in text
    assert "active: false" in text


def test_users_yaml_created_with_private_mode(tmp_path):
    config = make_config(tmp_path)
    accounts_for(config).create_user("first", "Erste", "trainer")
    assert stat.S_IMODE(config.paths.users.stat().st_mode) == 0o600
    assert stat.S_IMODE((config.paths.data / "credentials.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(config.paths.data.stat().st_mode) == 0o700


def test_secret_key_created_once_with_private_mode(config):
    with open_client(config):
        pass
    key = config.paths.data / "secret.key"
    first = key.read_bytes()
    assert len(first) == 64
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    with open_client(config):
        pass
    assert key.read_bytes() == first


def test_invalid_users_yaml_keeps_last_valid(config):
    directory = UserDirectory(config.paths.users)
    directory.load()
    assert directory.get("azubi") is not None
    config.paths.users.write_text("users:\n  azubi:\n    role: chef\n", encoding="utf-8")
    directory.load()
    assert directory.error and "role" in directory.error
    assert directory.get("azubi") is not None


def test_users_yaml_hot_reload(config):
    with open_client(config, "boss", watch=True) as client:
        time.sleep(0.3)
        text = (
            config.paths.users.read_text() + "  spaet:\n    name: Spät Dran\n    role: apprentice\n"
        )
        config.paths.users.write_text(text, encoding="utf-8")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "Spät Dran" not in client.get("/admin/users").text:
            time.sleep(0.05)
        assert "Spät Dran" in client.get("/admin/users").text


# ------------------------------------------------------------------ admin users page


def test_admin_users_lists_status(config):
    accounts_for(config).create_user("neu", "Neu Ling", "apprentice")
    with open_client(config, "boss") as client:
        html = client.get("/admin/users").text
    assert "Eingeladen, noch kein Passwort" in html
    assert "Aktiv" in html
    assert "Neu Ling" in html


def test_create_user_validation(config):
    with open_client(config, "boss") as client:
        assert "Bitte einen Namen" in post_form(client, "/admin/users", {"role": "apprentice"}).text
        dup = post_form(client, "/admin/users", {"name": "X", "username": "azubi"})
        assert dup.status_code == 400 and "gibt es bereits" in dup.text
        bad = post_form(client, "/admin/users", {"name": "X", "username": "../x"})
        assert bad.status_code == 400
        role = post_form(client, "/admin/users", {"name": "X", "role": "admin"})
        assert role.status_code == 400


def test_create_user_with_workbooks(config):
    with open_client(config, "boss") as client:
        post_form(
            client, "/admin/users", {"name": "Lisa Lern", "role": "apprentice", "workbooks": "demo"}
        )
    assert "workbooks: [demo]" in config.paths.users.read_text()


def test_supervisor_warnings_and_resolution(config):
    text = config.paths.users.read_text() + (
        "  lern:\n"
        "    name: Lern\n"
        "    role: apprentice\n"
        "    supervisors:\n"
        "      demo:\n"
        "        default: boss\n"
        "        tasks:\n"
        "          t1: azubi\n"
        "          nope: boss\n"
        "      missing:\n"
        "        default: ghost\n"
    )
    config.paths.users.write_text(text, encoding="utf-8")
    with open_client(config, "boss") as client:
        html = client.get("/admin/users").text
    assert "Unbekannte Aufgabe „nope“ im Heft „demo“." in html
    assert "„azubi“ ist kein Fachbetreuer." in html
    assert "Unbekanntes Heft „missing“." in html
    assert "„ghost“ ist kein Fachbetreuer." in html
    assert "demo: Fachbetreuer boss" in html

    user = User.model_validate(
        {"name": "x", "role": "apprentice",
         "supervisors": {"demo": {"default": "boss", "tasks": {"t1": "kschulz"}}}}
    )  # fmt: skip
    assert user.supervisor_for("demo", "t1") == "kschulz"
    assert user.supervisor_for("demo", "t2") == "boss"
    assert user.supervisor_for("other", "t1") is None


# ------------------------------------------------------------------ username suggestion


@pytest.mark.parametrize(
    ("name", "existing", "expected"),
    [
        ("Max Müller", [], "mmueller"),
        ("Jürgen Groß", [], "jgross"),
        ("Anna Maria Öztürk", [], "aoeztuerk"),
        ("Élodie Dupré", [], "edupre"),
        ("Max Müller", ["mmueller"], "mmueller-2"),
        ("Max Müller", ["mmueller", "mmueller-2"], "mmueller-3"),
        ("Cher", [], "cher"),
        ("  ", [], "user"),
        ("李 王", [], "user"),
    ],
)
def test_suggest_username(name, existing, expected):
    assert suggest_username(name, existing) == expected


# ------------------------------------------------------------------ CLI


def test_cli_user_commands(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"base_url: {BASE_URL}\npaths:\n  workbooks: {config.paths.workbooks}\n"
        f"  users: {config.paths.users}\n  data: {config.paths.data}\n"
    )
    runner = CliRunner()
    added = runner.invoke(
        cli_app,
        ["user", "add", "mmustermann", "--name", "Max Mustermann", "--role", "trainer", "-c", str(cfg)],
    )
    assert added.exit_code == 0, added.output
    assert f"{BASE_URL}/invite/" in added.output

    dup = runner.invoke(
        cli_app, ["user", "add", "mmustermann", "--name", "J", "--role", "trainer", "-c", str(cfg)]
    )
    assert dup.exit_code == 1

    bad = runner.invoke(
        cli_app, ["user", "add", "../x", "--name", "J", "--role", "trainer", "-c", str(cfg)]
    )
    assert bad.exit_code == 2
    role = runner.invoke(
        cli_app, ["user", "add", "x", "--name", "J", "--role", "boss", "-c", str(cfg)]
    )
    assert role.exit_code == 2

    runner.invoke(
        cli_app,
        ["user", "add", "mm", "--name", "Max", "--role", "apprentice", "--workbook", "demo",
         "-c", str(cfg)],
    )  # fmt: skip
    listing = runner.invoke(cli_app, ["user", "list", "-c", str(cfg)])
    assert listing.exit_code == 0
    assert re.search(r"mmustermann\s+trainer\s+invited\s+all", listing.output)
    assert re.search(r"mm\s+apprentice\s+invited\s+demo", listing.output)

    reset = runner.invoke(cli_app, ["user", "reset", "mmustermann", "-c", str(cfg)])
    assert reset.exit_code == 0 and "/invite/" in reset.output
    assert added.output.strip().splitlines()[-1] != reset.output.strip()
    assert runner.invoke(cli_app, ["user", "reset", "ghost", "-c", str(cfg)]).exit_code == 1


# ------------------------------------------------------------------ edit apprentice


def test_edit_apprentice_assignment(config):
    add_user(config, "kschulz", "trainer", name="Kai Schulz")
    (config.paths.workbooks / "other.yaml").write_text(
        VALID_YAML.replace("id: demo", "id: other"), encoding="utf-8"
    )
    original = config.paths.users.read_text()
    config.paths.users.write_text("# keep me\n" + original, encoding="utf-8")
    with open_client(config, "boss") as client:
        page = client.get("/admin/users/azubi")
        assert page.status_code == 200
        assert 'name="supervisor:demo"' in page.text and 'name="task:demo:t1"' in page.text
        response = post_form(
            client,
            "/admin/users/azubi",
            {
                "name": "Max Azubi-Neu",
                "workbooks": ["demo"],
                "supervisor:demo": "boss",
                "task:demo:t1": "kschulz",
                "supervisor:other": "kschulz",  # not allowed -> ignored
            },
            page="/admin/users/azubi",
        )
        assert response.status_code == 200 and "Gespeichert." in response.text
        user = client.app.state.users.get("azubi")
    assert user.name == "Max Azubi-Neu"
    assert user.workbooks == ["demo"]
    assert user.supervisor_for("demo", "t1") == "kschulz"
    assert "other" not in user.supervisors
    text = config.paths.users.read_text()
    assert text.startswith("# keep me\n")
    assert (
        "supervisors:\n      demo:\n        default: boss\n        tasks:\n          t1: kschulz"
        in text
    )


def test_edit_apprentice_override_equal_to_default_is_dropped(config):
    with open_client(config, "boss") as client:
        post_form(
            client,
            "/admin/users/azubi",
            {"name": "Max", "supervisor:demo": "boss", "task:demo:t1": "boss"},
            page="/admin/users/azubi",
        )
        user = client.app.state.users.get("azubi")
    assert user.workbooks is None  # no selection = all workbooks
    assert user.supervisors["demo"].default == "boss"
    assert user.supervisors["demo"].tasks == {}


def test_edit_apprentice_clear_assignment(config):
    with open_client(config, "boss") as client:
        post_form(client, "/admin/users/azubi", {"name": "Max", "supervisor:demo": "boss"},
                  page="/admin/users/azubi")  # fmt: skip
        post_form(client, "/admin/users/azubi", {"name": "Max", "supervisor:demo": ""},
                  page="/admin/users/azubi")  # fmt: skip
    assert "supervisors" not in config.paths.users.read_text()


def test_edit_apprentice_rejects_non_trainer_supervisor(config):
    with open_client(config, "boss") as client:
        response = post_form(
            client, "/admin/users/azubi", {"name": "Max", "supervisor:demo": "azubi"},
            page="/admin/users/azubi",
        )  # fmt: skip
        assert response.status_code == 400
        assert "Rolle Fachbetreuer" in response.text
        assert client.app.state.users.get("azubi").supervisors == {}


def test_edit_page_only_for_apprentices_and_trainers(config):
    with open_client(config, "boss") as client:
        assert client.get("/admin/users/boss").status_code == 404
        assert client.get("/admin/users/ghost").status_code == 404
        assert client.get("/admin/users/..").status_code == 404
    with open_client(config, "azubi") as client:
        assert client.get("/admin/users/azubi").status_code == 403
        response = client.post("/admin/users/azubi", data={"name": "Hack"}, headers=ORIGIN)
        assert response.status_code == 403
