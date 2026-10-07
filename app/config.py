"""Load ``config.yaml`` (see docs/PLAN.md section 7). Missing file -> defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ruamel.yaml import YAML

CONFIG_ENV = "WERKBANK_CONFIG"
LEGACY_CONFIG_ENV = "WORKBOOK_CONFIG"  # deprecated name, still read
DEFAULT_CONFIG_FILE = Path("config.yaml")
# systemd LoadCredential= names for the TLS files (see deploy/install.sh).
TLS_CREDENTIALS = ("tls.crt", "tls.key")


class Paths(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbooks: Path = Path("workbooks")
    progress: Path = Path("progress")
    users: Path = Path("users.yaml")
    data: Path = Path("data")
    locales: Path = Path("locales")


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://127.0.0.1:8000"
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(8000, ge=1, le=65535)
    paths: Paths = Paths()
    session_idle_minutes: int = Field(480, ge=1)
    login_max_attempts: int = Field(5, ge=1)
    login_lockout_minutes: int = Field(15, ge=1)
    invite_valid_hours: int = Field(72, ge=1)
    auth_mode: Literal["local"] = "local"  # reserved: "header" (Caddy forward_auth) later
    # Secure cookies with the __Host- prefix. Only disable for plain-HTTP testing.
    secure_cookies: bool = True
    # Time zone for dates and times shown in the UI, CSV and exports.
    timezone: str = "Europe/Berlin"
    # Serve HTTPS directly with these PEM files instead of running behind
    # Caddy: certificate (with chain) and private key. Both or neither.
    tls_cert: Path | None = None
    tls_key: Path | None = None

    @model_validator(mode="after")
    def _tls_pair(self) -> Config:
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ValueError("tls_cert and tls_key must be set together")
        return self

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone: {value}") from exc
        return value

    @property
    def origin(self) -> str:
        """Scheme, host and port of ``base_url`` (what browsers send as Origin)."""
        parts = urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.netloc}"

    def tls_files(self) -> tuple[Path, Path] | None:
        """Certificate and key to serve HTTPS with, or None (plain HTTP).

        Under systemd the files are handed over with LoadCredential=, so the
        service user needs no access to the originals (often root-only).
        """
        if self.tls_cert is None or self.tls_key is None:
            return None
        credentials = os.environ.get("CREDENTIALS_DIRECTORY")
        if credentials:
            cert, key = (Path(credentials) / name for name in TLS_CREDENTIALS)
            if cert.is_file() and key.is_file():
                return cert, key
        return self.tls_cert, self.tls_key

    def resolve_paths(self, base: Path) -> Config:
        """Return a copy with relative paths resolved against ``base``."""

        def resolve(value: Path) -> Path:
            return (value if value.is_absolute() else (base / value)).resolve()

        resolved = {name: resolve(value) for name, value in self.paths.model_dump().items()}
        tls = {
            name: resolve(value)
            for name in ("tls_cert", "tls_key")
            if (value := getattr(self, name)) is not None
        }
        return self.model_copy(update={"paths": Paths(**resolved), **tls})


def load_config(path: Path | None = None) -> Config:
    """Load the config file named by ``path``, $WERKBANK_CONFIG or ./config.yaml.

    Relative paths inside the config are resolved against the config file's
    directory (or the current directory when no file exists).
    """
    from_env = os.environ.get(CONFIG_ENV) or os.environ.get(LEGACY_CONFIG_ENV)
    explicit = path is not None or bool(from_env)
    if path is None:
        path = Path(from_env or DEFAULT_CONFIG_FILE)
    if path.is_file():
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        return Config.model_validate(data).resolve_paths(path.resolve().parent)
    if explicit:
        raise FileNotFoundError(f"Config file not found: {path}")
    return Config().resolve_paths(Path.cwd())
