from __future__ import annotations

import contextlib
import copy
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent

MINIMAL: dict[str, Any] = {
    "schema_version": 1,
    "workbook": {"id": "demo", "version": "1.0.0", "title": "Demo"},
    "levels": {"understand": {"label": "Verstehen", "color": "blue"}},
    "days": [
        {
            "id": "day-1",
            "title": "Tag eins",
            "modules": [
                {
                    "code": "M-01",
                    "title": "Modul",
                    "tasks": [
                        {
                            "id": "t1",
                            "title": "Aufgabe",
                            "level": "understand",
                            "duration": "45m",
                            "requirement": "Tu etwas.",
                            "answers": [{"id": "a1", "label": "Antwort"}],
                            "trainer": {
                                "expectations": ["GEHEIM-ERWARTUNG"],
                                "notes": "GEHEIM-NOTIZ",
                            },
                        }
                    ],
                }
            ],
        }
    ],
}


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    """argon2 with production parameters is deliberately slow; tests use cheap ones."""
    from argon2 import PasswordHasher

    import app.auth

    monkeypatch.setattr(
        app.auth, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    monkeypatch.setattr(app.auth, "_dummy_hash", None)


@pytest.fixture
def catalog_data() -> dict[str, Any]:
    """A fresh, valid minimal catalog as plain data (safe to mutate)."""
    return copy.deepcopy(MINIMAL)


def task_of(data: dict[str, Any], day: int = 0, module: int = 0, task: int = 0) -> dict[str, Any]:
    return data["days"][day]["modules"][module]["tasks"][task]


# ------------------------------------------------------------------ server helpers

BASE_URL = "https://testserver"
ORIGIN = {"Origin": BASE_URL}
PASSWORD = "korrekt-pferd-batterie"


def make_config(tmp_path: Path, **overrides: Any):
    from app.config import Config, Paths

    workbooks = tmp_path / "workbooks"
    workbooks.mkdir(exist_ok=True)
    return Config(
        base_url=BASE_URL,
        paths=Paths(
            workbooks=workbooks,
            progress=tmp_path / "progress",
            users=tmp_path / "users.yaml",
            data=tmp_path / "data",
            locales=ROOT / "locales",
        ),
        **overrides,
    )


def add_user(
    config,
    username: str,
    role: str = "apprentice",
    *,
    name: str | None = None,
    password: str | None = PASSWORD,
    workbooks: list[str] | None = None,
) -> None:
    """Create a user directly in users.yaml + credentials (bypassing the UI)."""
    from app.auth import CredentialStore, UserDirectory, hash_password

    directory = UserDirectory(config.paths.users)
    directory.load()
    directory.add(username, name or username.title(), role, workbooks)
    if password is not None:
        store = CredentialStore(config.paths.data / "credentials.json")

        def change(cred):
            cred.password_hash = hash_password(password)

        store.update(username, change)


def csrf_from(html: str) -> str:
    import re

    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html)
    assert match, "no csrf token in page"
    return match.group(1)


def login(client, username: str, password: str = PASSWORD, *, follow: bool = False):
    token = csrf_from(client.get("/login").text)
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token, "next": "/"},
        headers=ORIGIN,
        follow_redirects=follow,
    )


@contextlib.contextmanager
def open_client(config, user: str | None = None, *, watch: bool = False):
    """TestClient for a fresh app; logs in ``user`` (must exist) if given."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(config, watch=watch), base_url=BASE_URL) as client:
        if user is not None:
            response = login(client, user)
            assert response.status_code == 303, response.text
        yield client


def replace_cookie(client, name: str, value: str) -> None:
    """Overwrite a cookie the server set (keeps its cookie-jar domain)."""
    domain = next(c.domain for c in client.cookies.jar if c.name == name)
    client.cookies.delete(name)
    client.cookies.set(name, value, domain=domain, path="/")


def second_client(client, user: str | None = None):
    """Another browser on the SAME running app (shared registry and user list)."""
    from fastapi.testclient import TestClient

    other = TestClient(client.app, base_url=BASE_URL)
    if user is not None:
        assert login(other, user).status_code == 303
    return other
