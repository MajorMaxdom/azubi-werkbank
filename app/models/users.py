"""Models for ``users.yaml`` (human-editable) and ``data/credentials.json`` (app-managed)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.catalog import Id

Role = Literal["trainer", "apprentice"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Supervision(Strict):
    """Fachbetreuer for one workbook of an apprentice."""

    default: Id | None = None
    tasks: dict[Id, Id] = {}


class User(Strict):
    name: str = Field(min_length=1)
    role: Role
    workbooks: list[Id] | None = None  # None = all workbooks
    active: bool = True
    supervisors: dict[Id, Supervision] = {}

    def may_open(self, workbook_id: str) -> bool:
        return self.role == "trainer" or self.workbooks is None or workbook_id in self.workbooks

    def supervisor_for(self, workbook_id: str, task_id: str) -> str | None:
        supervision = self.supervisors.get(workbook_id)
        if supervision is None:
            return None
        return supervision.tasks.get(task_id, supervision.default)


class UsersFile(Strict):
    users: dict[Id, User] = {}

    @field_validator("users", mode="before")
    @classmethod
    def _none_is_empty(cls, value):
        return {} if value is None else value


class Invite(Strict):
    token_hash: str
    expires_at: datetime


class Credential(Strict):
    password_hash: str | None = None
    session_version: int = 1
    invite: Invite | None = None
    failed_attempts: int = 0
    locked_until: datetime | None = None


class CredentialsFile(Strict):
    users: dict[Id, Credential] = {}
