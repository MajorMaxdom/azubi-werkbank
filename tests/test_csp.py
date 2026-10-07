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
    (config.paths.workbooks / "linux-basics.yaml").write_text(
        (ROOT / "workbooks" / "linux-basics.yaml").read_text(encoding="utf-8"), encoding="utf-8"
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
        # Question/answer thread and review history (rendered with forms and details).
        json_headers = {"Origin": BASE_URL, "X-Workbook": "1"}
        anna.post("/api/progress/demo/tasks/t1/comments", json={"text": "Frage?"},
                  headers=json_headers)  # fmt: skip
        boss.post("/api/progress/demo/users/anna/tasks/t1/comments", json={"text": "Antwort"},
                  headers=json_headers)  # fmt: skip
        for review in ({"status": "redo", "comment": "Erst"}, {"status": "ok", "comment": ""}):
            boss.patch("/api/progress/demo/users/anna/tasks/t1/review", json=review,
                       headers=json_headers)  # fmt: skip
        link = boss.app.state.accounts.create_user("neu", "Neu", "apprentice")
        result["invite"] = anna.get(link.removeprefix(BASE_URL)).text
        for path in ["/", "/workbooks/demo", "/workbooks/themed", "/workbooks/linux-basics",
                     "/workbooks/demo/users/anna", "/admin/overview", "/admin/users",
                     "/admin/users/anna", "/admin/catalogs", "/nope", "/my-tasks",
                     "/my-tasks?filter=open", "/my-tasks?filter=ok", "/my-tasks?filter=redo",
                     "/my-tasks?scope=all", "/admin/editor", "/admin/editor/demo", "/account",
                     "/admin/users/anna/delete", "/admin/assign", "/admin/editor/demo/assets",
                     "/admin/editor/demo/delete",
                     "/admin/assign?workbook=linux-basics"]:  # fmt: skip
            result[f"boss {path}"] = boss.get(path).text
        for path in ["/", "/workbooks/demo", "/workbooks/linux-basics", "/admin/users",
                     "/my-tasks", "/account"]:  # fmt: skip
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


def test_fixture_covers_thread_and_review_history(pages):
    for name in ("anna /workbooks/demo", "boss /workbooks/demo/users/anna"):
        assert "data-thread-url=" in pages[name] and "Bewertungsverlauf (1)" in pages[name]
        assert '<script src="/static/js/comments.js" defer></script>' in pages[name]
    assert ">Rückfrage</span>" not in pages["boss /my-tasks?scope=all"]  # answered
    assert ">Antwort</span>" in pages["anna /my-tasks"]


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
                   "X-Frame-Options DENY", "Referrer-Policy same-origin", "-Server",
                   "reverse_proxy 127.0.0.1:8000", "werkbank.example.de"):  # fmt: skip
        assert header in caddyfile


def test_service_unit_hardening():
    unit = (ROOT / "deploy" / "werkbank.service").read_text(encoding="utf-8")
    for line in ("User=werkbank", "NoNewPrivileges=true", "ProtectSystem=strict",
                 "ProtectHome=true", "PrivateTmp=true", "Restart=on-failure",
                 "ReadWritePaths=/var/lib/werkbank",
                 "ExecStart=/opt/werkbank/.venv/bin/werkbank serve"):  # fmt: skip
        assert line in unit


def test_config_example_matches_config_model():
    from ruamel.yaml import YAML

    from app.config import Config

    data = YAML(typ="safe").load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    config = Config.model_validate(data)
    assert config.listen_host == "127.0.0.1"
    assert config.secure_cookies is True
    assert set(data) == set(Config.model_fields)


def test_install_script_syntax_and_caddy_site(tmp_path):
    """deploy/install.sh parses and generates the Caddy site from deploy/Caddyfile."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    script = ROOT / "deploy" / "install.sh"
    subprocess.run([bash, "-n", str(script)], check=True)
    out = subprocess.run(
        [bash, "-c", f'source "{script}"; caddyfile_content werkbank.firma.de 8123 1'],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    assert "werkbank.firma.de {\n\ttls internal" in out
    assert "reverse_proxy 127.0.0.1:8123" in out
    assert "Content-Security-Policy" in out
    assert "example.de" not in out


def test_uninstall_script_removes_only_the_werkbank_caddy_import(tmp_path):
    """deploy/uninstall.sh parses, drops its import line and keeps other sites."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    script = ROOT / "deploy" / "uninstall.sh"
    subprocess.run([bash, "-n", str(script)], check=True)
    site = tmp_path / "werkbank.caddy"
    site.write_text(":9998 {\n\treverse_proxy 127.0.0.1:8013\n}\n")
    main = tmp_path / "Caddyfile"
    other = ':9999 {\n\trespond "other"\n}\n'
    main.write_text(f"{other}\n# Azubi-Werkbank (added by deploy/install.sh)\nimport {site}\n")
    env = {"PATH": "/usr/bin:/bin", "CADDY_DIR": str(tmp_path),
           "CADDY_BIN": "no-such-caddy", "CADDY_RELOAD": "0"}  # fmt: skip
    subprocess.run(
        [bash, "-c", f'source "{script}"; remove_caddy_site'],
        check=True, capture_output=True, env=env,
    )  # fmt: skip
    assert main.read_text() == other + "\n"
    assert not site.exists()
    assert list(tmp_path.glob("Caddyfile.bak-werkbank-uninstall-*"))

    refused = subprocess.run(
        [bash, "-c", f'source "{script}"; safe_data_dir /var/lib || safe_data_dir "{tmp_path}"'],
        env=env,
    )  # fmt: skip
    assert refused.returncode != 0


def test_install_script_rejects_invalid_port(tmp_path):
    """deploy/install.sh refuses privileged ports and keeps the configured one."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    script = ROOT / "deploy" / "install.sh"
    config = tmp_path / "config.yaml"

    def choose(port: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [bash, "-c", f'source "{script}"; CONFIG_FILE="{config}"; ASSUME_YES=1; '
                         f'PORT={port}; choose_port; echo "PORT=$PORT"'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )  # fmt: skip

    bad = choose("80")
    assert bad.returncode != 0 and "Invalid port" in bad.stderr
    config.write_text("listen_port: 8123\n")
    assert choose('""').stdout.strip() == "PORT=8123"


def _caddy_env(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "Caddyfile").write_text(':9999 {\n\trespond "other"\n}\n')
    log = tmp_path / "log" / "werkbank.log"
    # Fake systemctl first in PATH ("timeout" execs binaries, not shell
    # functions): tests must never reach the real Caddy service.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "systemctl"
    fake.write_text('#!/bin/sh\n[ "$1" = is-active ] && exit 0\necho "$@" >> "$0.log"\nexit 1\n')
    fake.chmod(0o755)
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin", "CADDY_DIR": str(etc),
           "CADDY_BIN": "true", "CADDY_LOG": str(log)}  # fmt: skip
    return etc, env


def test_install_script_takes_import_back_when_reload_fails(tmp_path):
    """A failed Caddy reload must not leave the import in the main Caddyfile."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    script = ROOT / "deploy" / "install.sh"
    etc, env = _caddy_env(tmp_path)
    original = (etc / "Caddyfile").read_text()
    run = subprocess.run(
        [bash, "-c", f'source "{script}"; id() {{ return 1; }}; DOMAIN=wb.example.org PORT=8014 '
                     "TLS_INTERNAL=0; install_caddy_site; reload_caddy"],
        capture_output=True, text=True, env=env,
    )  # fmt: skip
    assert run.returncode != 0
    assert "journalctl -u caddy" in run.stderr
    assert (etc / "Caddyfile").read_text() == original
    assert not (etc / "werkbank.caddy").exists()
    assert (tmp_path / "bin" / "systemctl.log").read_text() == "reload caddy\n"
    assert "restart" not in script.read_text().split("reload_caddy() {")[1].split("\n}")[0]


def test_install_script_hands_the_access_log_to_caddy(tmp_path):
    """caddy validate runs as root; the log file must still belong to caddy."""
    import os
    import pwd
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None or os.geteuid() != 0:
        pytest.skip("needs bash and root")
    try:
        caddy = pwd.getpwnam("caddy")
    except KeyError:
        pytest.skip("no caddy user")
    script = ROOT / "deploy" / "install.sh"
    _, env = _caddy_env(tmp_path)
    log = tmp_path / "log" / "werkbank.log"
    log.parent.mkdir()
    log.touch(mode=0o600)  # as left behind by "caddy validate" running as root
    subprocess.run([bash, "-c", f'source "{script}"; prepare_caddy_log'], check=True, env=env)
    assert (log.stat().st_uid, log.stat().st_gid) == (caddy.pw_uid, caddy.pw_gid)
