"""Every server-rendered page must work under the Caddy CSP:
default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'.
So: no inline scripts, no inline styles, no event-handler attributes, no
javascript: URLs, and every script/stylesheet comes from this origin."""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from tests.conftest import BASE_URL, ROOT, add_user, make_config, open_client, second_client
from tests.test_progress import CATALOG

CSP_LINE = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class CspAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for name, value in attrs:
            if name.startswith("on"):
                self.problems.append(f"event handler {name} on <{tag}>")
            if name == "style":
                self.problems.append(f"inline style on <{tag}>")
            if name in ("href", "src", "action", "formaction") and value:
                if value.strip().lower().startswith("javascript:"):
                    self.problems.append(f"javascript: URL in <{tag} {name}>")
                external = re.match(r"^(https?:)?//", value) and not value.startswith(BASE_URL)
                if external and not (tag == "a" and name == "href"):  # plain links are fine
                    self.problems.append(f"external {name} on <{tag}>: {value}")
        if tag == "style":
            self.problems.append("inline <style> block")
        if tag == "script":
            src = a.get("src")
            if not src or not src.startswith("/static/js/"):
                self.problems.append(f"script not from /static/js/: {src!r}")
            self._in_script = True
        if tag == "link" and a.get("rel") == "stylesheet":
            href = a.get("href", "")
            if not (
                href.startswith("/static/css/")
                or re.match(r"^/workbooks/[a-z0-9-]+/theme\.css", href)
            ):
                self.problems.append(f"stylesheet from elsewhere: {href}")
        if tag == "form" and a.get("action", "/").startswith(("http:", "https:", "//")):
            self.problems.append("form posts to another origin")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.problems.append("inline script content")


def audit(html: str) -> list[str]:
    parser = CspAudit()
    parser.feed(html)
    return parser.problems


@pytest.fixture
def pages(tmp_path):
    config = make_config(tmp_path)
    (config.paths.workbooks / "demo.yaml").write_text(CATALOG, encoding="utf-8")
    (config.paths.workbooks / "network-security.yaml").write_text(
        (ROOT / "workbooks" / "network-security.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    themed = CATALOG.replace("id: demo", "id: themed").replace(
        "  title: Demo\n", '  title: Themed\n  stylesheet:\n    accent: "#0F4C3A"\n'
    )
    (config.paths.workbooks / "themed.yaml").write_text(themed, encoding="utf-8")
    add_user(config, "boss", "trainer")
    add_user(config, "anna", "apprentice")
    result: dict[str, str] = {}
    with open_client(config) as anonymous:
        result["login"] = anonymous.get("/login").text
        result["invite-invalid"] = anonymous.get("/invite/nope").text
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        anna.patch(
            "/api/progress/demo/tasks/t1",
            json={"answers": {"a1": "<script>alert(1)</script>"}, "done": True},
            headers={"Origin": BASE_URL, "X-Workbook": "1"},
        )
        link = boss.app.state.accounts.create_user("neu", "Neu", "apprentice")
        result["invite"] = anna.get(link.removeprefix(BASE_URL)).text
        for path in ["/", "/workbooks/demo", "/workbooks/themed", "/workbooks/network-security",
                     "/workbooks/demo/users/anna", "/admin/overview", "/admin/users",
                     "/admin/users/anna", "/admin/catalogs", "/nope", "/my-tasks",
                     "/my-tasks?filter=open", "/my-tasks?filter=ok", "/my-tasks?filter=redo", "/admin/editor", "/admin/editor/demo"]:  # fmt: skip
            result[f"boss {path}"] = boss.get(path).text
        for path in ["/", "/workbooks/demo", "/workbooks/network-security", "/admin/users",
                     "/my-tasks"]:  # fmt: skip
            result[f"anna {path}"] = anna.get(path).text
    return result


def test_all_pages_are_csp_compatible(pages):
    assert len(pages) >= 15
    problems = {name: audit(html) for name, html in pages.items()}
    assert {k: v for k, v in problems.items() if v} == {}


def test_user_input_cannot_inject_script(pages):
    for name in ("anna /workbooks/demo", "boss /workbooks/demo/users/anna"):
        assert "<script>alert(1)" not in pages[name]
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in pages[name]


def test_theme_is_linked_not_inlined(pages):
    html = pages["boss /workbooks/themed"]
    assert re.search(
        r'<link rel="stylesheet" href="/workbooks/themed/theme\.css\?v=[0-9a-f]{16}">', html
    )
    assert "#0F4C3A" not in html


def test_caddyfile_has_the_planned_headers():
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert f'Content-Security-Policy "{CSP_LINE}"' in caddyfile
    for header in ('Strict-Transport-Security "max-age=31536000"', "X-Content-Type-Options nosniff",
                   "X-Frame-Options DENY", "Referrer-Policy no-referrer", "-Server",
                   "reverse_proxy 127.0.0.1:8000"):  # fmt: skip
        assert header in caddyfile


def test_service_unit_hardening():
    unit = (ROOT / "deploy" / "workbook.service").read_text(encoding="utf-8")
    for line in ("User=workbook", "NoNewPrivileges=true", "ProtectSystem=strict",
                 "ProtectHome=true", "PrivateTmp=true", "Restart=on-failure",
                 "ReadWritePaths=/var/lib/workbook",
                 "ExecStart=/opt/workbook/.venv/bin/workbook serve"):  # fmt: skip
        assert line in unit


def test_config_example_matches_config_model():
    from ruamel.yaml import YAML

    from app.config import Config

    data = YAML(typ="safe").load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    config = Config.model_validate(data)
    assert config.listen_host == "127.0.0.1"
    assert config.secure_cookies is True
    assert set(data) == set(Config.model_fields)
