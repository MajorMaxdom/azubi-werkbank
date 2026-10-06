"""Model of ``progress/<workbook-id>/<username>.json`` (see docs/PLAN.md section 4)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

AnswerValue = str | list[str]


class Lenient(BaseModel):
    # Progress files are app-managed; unknown keys from newer versions are ignored.
    model_config = ConfigDict(extra="ignore")


class Review(Lenient):
    status: Literal["ok", "redo"] | None = None
    comment: str = ""
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class Comment(Lenient):
    """One message of the question/answer thread of a task (append-only)."""

    author: str
    role: Literal["apprentice", "trainer"]
    text: str
    at: datetime


class TaskProgress(Lenient):
    answers: dict[str, AnswerValue] = {}
    done: bool = False
    done_at: datetime | None = None
    task_hash: str | None = None
    updated_at: datetime | None = None
    review: Review | None = None
    review_history: list[Review] = []  # earlier reviews, oldest first
    comments: list[Comment] = []  # question/answer thread, oldest first


class Signoff(Lenient):
    comment: str = ""
    date: str | None = None
    by: str | None = None


class Progress(Lenient):
    schema_version: Literal[1] = 1
    workbook: str
    workbook_version: str = ""
    user: str
    updated_at: datetime | None = None
    header: dict[str, str] = {}
    tasks: dict[str, TaskProgress] = {}
    signoff: Signoff = Signoff()

    def done_count(self, task_ids: set[str]) -> int:
        return sum(1 for tid, tp in self.tasks.items() if tid in task_ids and tp.done)
