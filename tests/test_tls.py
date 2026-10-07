"""Own-certificate mode: TLS config, security headers, installer checks."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Config, load_config
from app.security import CONTENT_SECURITY_POLICY
from tests.conftest import ROOT, make_config, open_client

# ------------------------------------------------------------------ config


def test_tls_cert_and_key_must_be_set_together(tmp_path):
    with pytest.raises(ValidationError, match="tls_cert and tls_key"):
        Config(tls_cert=tmp_path / "cert.pem")
    assert Config().tls_files() is None


def test_tls_paths_resolve_relative_to_the_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("tls_cert: certs/full.pem\ntls_key: /etc/ssl/key.pem\n")
    config = load_config(config_file)
    assert config.tls_files() == (tmp_path / "certs" / "full.pem", Path("/etc/ssl/key.pem"))


def test_tls_files_prefer_systemd_credentials(tmp_path, monkeypatch):
    config = Config(tls_cert=Path("/etc/ssl/full.pem"), tls_key=Path("/etc/ssl/key.pem"))
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    assert config.tls_files() == (Path("/etc/ssl/full.pem"), Path("/etc/ssl/key.pem"))
    (credentials / "tls.crt").write_text("cert")
    (credentials / "tls.key").write_text("key")
    assert config.tls_files() == (credentials / "tls.crt", credentials / "tls.key")


# ------------------------------------------------------------------ security headers


def test_security_headers_on_every_response(tmp_path):
    with open_client(make_config(tmp_path)) as client:
        for path in ("/login", "/static/css/tokens.css", "/does-not-exist"):
            headers = client.get(path, follow_redirects=False).headers
            assert headers["content-security-policy"] == CONTENT_SECURITY_POLICY, path
            assert headers["x-frame-options"] == "DENY"
            assert headers["x-content-type-options"] == "nosniff"
            assert headers["referrer-policy"] == "same-origin"
            assert "server" not in headers


def test_hsts_only_for_https(tmp_path):
    https = make_config(tmp_path)  # https://testserver
    with open_client(https) as client:
        assert client.get("/login").headers["strict-transport-security"] == "max-age=31536000"
    plain = https.model_copy(update={"base_url": "http://127.0.0.1:8000"})
    with open_client(plain) as client:
        assert "strict-transport-security" not in client.get("/login").headers


def test_caddy_site_sends_the_same_csp():
    assert (
        f'Content-Security-Policy "{CONTENT_SECURITY_POLICY}"'
        in (ROOT / "deploy" / "Caddyfile").read_text()
    )


# ------------------------------------------------------------------ installer


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    """Self-signed wildcard certificate for *.werkbank.test plus a foreign key."""
    if shutil.which("openssl") is None or shutil.which("bash") is None:
        pytest.skip("openssl and bash needed")
    d = tmp_path_factory.mktemp("cert")
    for name, cn in (("wild", "*.werkbank.test"), ("other", "other.example")):
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-nodes", "-days", "2", "-subj", f"/CN={cn}",
             "-addext", f"subjectAltName=DNS:{cn}",
             "-keyout", str(d / f"{name}.key"), "-out", str(d / f"{name}.pem")],
            check=True, capture_output=True,
        )  # fmt: skip
    return d


def certificate_problem(cert: Path, key: Path, domain: str) -> str:
    script = ROOT / "deploy" / "install.sh"
    return subprocess.run(
        ["bash", "-c", f'source "{script}"; certificate_problem "$1" "$2" "$3"', "-",
         str(cert), str(key), domain],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def test_installer_accepts_matching_wildcard_certificate(certificate):
    cert, key = certificate / "wild.pem", certificate / "wild.key"
    assert certificate_problem(cert, key, "azubi.werkbank.test") == ""


@pytest.mark.parametrize(
    ("cert", "key", "domain", "problem"),
    [
        ("wild.pem", "wild.key", "a.b.werkbank.test", "not valid for a.b.werkbank.test"),
        ("wild.pem", "wild.key", "werkbank.example.org", "it covers: DNS:*.werkbank.test"),
        ("wild.pem", "other.key", "azubi.werkbank.test", "does not belong"),
        ("wild.key", "wild.key", "azubi.werkbank.test", "not a PEM certificate"),
        ("missing.pem", "wild.key", "azubi.werkbank.test", "not found"),
    ],
)
def test_installer_rejects_unsuitable_certificates(certificate, cert, key, domain, problem):
    assert problem in certificate_problem(certificate / cert, certificate / key, domain)


def test_installer_https_mode_without_questions():
    script = ROOT / "deploy" / "install.sh"

    def mode(args: str) -> str:
        return subprocess.run(
            ["bash", "-c", f'source "{script}"; CONFIG_FILE=/nonexistent; ASSUME_YES=1; {args}; '
                           'choose_https_mode; echo "$HTTPS_MODE $WITH_CADDY"'],
            check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        ).stdout.strip()  # fmt: skip

    assert mode("true") == "none 0"
    assert mode("DOMAIN=w.example.org") == "caddy 1"
    assert mode("DOMAIN=w.example.org; TLS_CERT=/c.pem") == "cert 0"
