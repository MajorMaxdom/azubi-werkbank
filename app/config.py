"""Load ``config.yaml`` (see docs/PLAN.md section 7). Missing file -> defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

CONFIG_ENV = "WORKBOOK_CONFIG"
DEFAULT_CONFIG_FILE = Path("config.yaml")


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
    auth_mode: Literal["local"] = "local"

    def resolve_paths(self, base: Path) -> Config:
        """Return a copy with relative paths resolved against ``base``."""
        resolved = {
            name: (value if value.is_absolute() else (base / value)).resolve()
            for name, value in self.paths.model_dump().items()
        }
        return self.model_copy(update={"paths": Paths(**resolved)})


def load_config(path: Path | None = None) -> Config:
    """Load the config file named by ``path``, $WORKBOOK_CONFIG or ./config.yaml.

    Relative paths inside the config are resolved against the config file's
    directory (or the current directory when no file exists).
    """
    explicit = path is not None or bool(os.environ.get(CONFIG_ENV))
    if path is None:
        path = Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_FILE)
    if path.is_file():
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        return Config.model_validate(data).resolve_paths(path.resolve().parent)
    if explicit:
        raise FileNotFoundError(f"Config file not found: {path}")
    return Config().resolve_paths(Path.cwd())
