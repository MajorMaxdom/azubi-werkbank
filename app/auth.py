"""Authentication: users.yaml, credentials, passwords, invites, sessions,
rate limiting, CSRF/Origin checks and the single place that resolves the
current user (``AuthMiddleware`` + ``get_current_user``)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import re
import secrets
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from watchfiles import awatch

from app.config import Config
from app.files import PRIVATE_FILE_MODE, atomic_write, ensure_private_dir, locked
from app.models.catalog import ID_PATTERN
from app.models.users import (
    Credential,
    CredentialsFile,
    Invite,
    Role,
    User,
    UsersFile,
)
from app.progress import ProgressStore

log = logging.getLogger(__name__)

_ID_RE = re.compile(ID_PATTERN)
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
SESSION_REFRESH_SECONDS = 5 * 60
IP_MAX_FAILURES = 20
IP_WINDOW_SECONDS = 15 * 60
UNCHANGED: Any = object()  # sentinel for UserDirectory.assign_supervisors


def is_valid_username(value: str | None) -> bool:
    return bool(value) and _ID_RE.fullmatch(value) is not None


def utcnow() -> datetime:
    return datetime.now(UTC)


# =========================================================================== users.yaml


USERS_HEADER = """\
# Users of the workbook server. Human-editable; the app keeps comments.
# Roles: trainer (Fachbetreuer) | apprentice. See docs/PLAN.md section 3.
"""


class UserDirectory:
    """``users.yaml``: validated on load, hot-reloaded, written round-trip."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = UsersFile()
        self.error: str | None = None
        self._lock = threading.RLock()

    def get(self, username: str) -> User | None:
        with self._lock:
            return self._data.users.get(username)

    def all(self) -> dict[str, User]:
        with self._lock:
            return dict(self._data.users)

    def load(self) -> None:
        """Reload from disk. An invalid file keeps the last valid users."""
        try:
            text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
            raw = YAML(typ="safe").load(text) or {}
            data = UsersFile.model_validate(raw)
        except (OSError, YAMLError, ValidationError, ValueError) as exc:
            with self._lock:
                self.error = _short_error(exc)
            log.error("users.invalid file=%s", self.path.name)
            return
        with self._lock:
            self._data = data
            self.error = None
        log.info("users.loaded count=%d", len(data.users))

    # ------------------------------------------------------------------ writes

    def add(self, username: str, name: str, role: Role, workbooks: list[str] | None = None) -> None:
        def change(users: CommentedMap) -> None:
            if username in users:
                raise ValueError(f"User '{username}' already exists")
            entry = CommentedMap()
            entry["name"] = name
            entry["role"] = role
            if workbooks:
                seq = CommentedSeq(workbooks)
                seq.fa.set_flow_style()
                entry["workbooks"] = seq
            users[username] = entry

        self._modify(change)

    def set_active(self, username: str, active: bool) -> None:
        def change(users: CommentedMap) -> None:
            if username not in users:
                raise KeyError(username)
            entry = users[username]
            if active:
                entry.pop("active", None)  # default is true
            else:
                entry["active"] = False

        self._modify(change)

    def update_apprentice(
        self,
        username: str,
        *,
        name: str,
        workbooks: list[str] | None,
        supervisors: dict[str, tuple[str | None, dict[str, str]]],
        managed: Iterable[str],
    ) -> None:
        """Update name, workbook list and Fachbetreuer assignment of an apprentice.

        Only the workbooks in ``managed`` are rewritten in ``supervisors``; entries
        for other workbooks (e.g. a catalog that is currently missing) are kept.
        """

        def change(users: CommentedMap) -> None:
            if username not in users:
                raise KeyError(username)
            entry = users[username]
            entry["name"] = name
            if workbooks:
                seq = CommentedSeq(workbooks)
                seq.fa.set_flow_style()
                entry["workbooks"] = seq
            else:
                entry.pop("workbooks", None)
            current = entry.get("supervisors") or CommentedMap()
            for wid in managed:
                default, tasks = supervisors.get(wid, (None, {}))
                if not default and not tasks:
                    current.pop(wid, None)
                    continue
                block = CommentedMap()
                if default:
                    block["default"] = default
                if tasks:
                    block["tasks"] = CommentedMap(tasks)
                current[wid] = block
            if current:
                entry["supervisors"] = current
            else:
                entry.pop("supervisors", None)

        self._modify(change)

    def remove(self, username: str) -> int:
        """Delete the user and every Fachbetreuer assignment pointing to them.

        Returns the number of removed assignments (workbook defaults and task
        overrides in other users' ``supervisors``).
        """
        removed = 0

        def change(users: CommentedMap) -> None:
            nonlocal removed
            if username not in users:
                raise KeyError(username)
            del users[username]
            for entry in users.values():
                if not isinstance(entry, CommentedMap):
                    continue
                removed += _drop_supervisor(entry, username)

        self._modify(change)
        return removed

    def assign_supervisors(
        self,
        workbook_id: str,
        usernames: Iterable[str],
        *,
        default: str | None = UNCHANGED,
        tasks: dict[str, str | None] | None = None,
    ) -> None:
        """Change the Fachbetreuer of one workbook for several users in ONE write.

        ``default``: a trainer, ``None`` (nobody) or ``UNCHANGED``.
        ``tasks``: task id -> trainer (set an override) or ``None`` (remove the
        override, i.e. "same as workbook"). Overrides equal to the resulting
        workbook default are dropped. Other workbooks and comments are kept.
        """
        names = list(usernames)
        task_changes = tasks or {}

        def change(users: CommentedMap) -> None:
            for username in names:
                if username not in users:
                    raise KeyError(username)
            for username in names:
                entry = users[username]
                current = entry.get("supervisors")
                if current is None:
                    current = CommentedMap()
                block = current.get(workbook_id)
                if block is None:
                    block = CommentedMap()
                if default is not UNCHANGED:
                    if default:
                        if "default" in block:
                            block["default"] = default
                        else:
                            block.insert(0, "default", default)
                    else:
                        block.pop("default", None)
                overrides = block.get("tasks")
                if overrides is None:
                    overrides = CommentedMap()
                for task_id, trainer in task_changes.items():
                    if trainer:
                        overrides[task_id] = trainer
                    else:
                        overrides.pop(task_id, None)
                effective = block.get("default")
                for task_id in [k for k, v in overrides.items() if v == effective]:
                    del overrides[task_id]
                if overrides:
                    block["tasks"] = overrides
                else:
                    block.pop("tasks", None)
                if block:
                    current[workbook_id] = block
                else:
                    current.pop(workbook_id, None)
                if current:
                    entry["supervisors"] = current
                else:
                    entry.pop("supervisors", None)

        self._modify(change)

    def _modify(self, change: Callable[[CommentedMap], None]) -> None:
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        with locked(self.path):
            text = self.path.read_text(encoding="utf-8") if self.path.exists() else USERS_HEADER
            doc = yaml.load(text)
            if doc is None:
                doc = CommentedMap()
                doc.yaml_set_start_comment(USERS_HEADER.replace("# ", "").rstrip())
            if doc.get("users") is None:
                doc["users"] = CommentedMap()
            change(doc["users"])
            out = io.StringIO()
            yaml.dump(doc, out)
            new_text = out.getvalue()
            UsersFile.model_validate(YAML(typ="safe").load(new_text) or {})
            mode = self.path.stat().st_mode & 0o777 if self.path.exists() else PRIVATE_FILE_MODE
            atomic_write(self.path, new_text, mode=mode)
        self.load()

    # ------------------------------------------------------------------ watching

    async def watch(self, stop_event: asyncio.Event | None = None) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        name = self.path.name

        def only_users_file(_change, path: str) -> bool:
            return Path(path).name == name

        async for _ in awatch(
            directory,
            stop_event=stop_event,
            watch_filter=only_users_file,
            recursive=False,
            debounce=400,
        ):
            await asyncio.to_thread(self.load)


def _drop_supervisor(entry: CommentedMap, username: str) -> int:
    """Remove ``username`` from one users.yaml entry's ``supervisors`` block."""
    supervisors = entry.get("supervisors")
    if not isinstance(supervisors, CommentedMap):
        return 0
    removed = 0
    for wid in list(supervisors):
        block = supervisors[wid]
        if not isinstance(block, CommentedMap):
            continue
        if block.get("default") == username:
            del block["default"]
            removed += 1
        tasks = block.get("tasks")
        if isinstance(tasks, CommentedMap):
            for task_id in [k for k, v in tasks.items() if v == username]:
                del tasks[task_id]
                removed += 1
            if not tasks:
                del block["tasks"]
        if not block:
            del supervisors[wid]
    if not supervisors:
        del entry["supervisors"]
    return removed


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors(include_url=False)[:5]:
            loc = ".".join(str(p) for p in err["loc"])
            parts.append(f"{loc}: {err['msg']}")
        return "; ".join(parts)
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


# =========================================================================== credentials


class CredentialStore:
    """``data/credentials.json``. Read on every access (the CLI may write it)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> CredentialsFile:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return CredentialsFile()
        return CredentialsFile.model_validate(json.loads(text))

    def get(self, username: str) -> Credential | None:
        return self.read().users.get(username)

    def update(self, username: str, change: Callable[[Credential], None]) -> Credential:
        ensure_private_dir(self.path.parent)
        with locked(self.path):
            data = self.read()
            # New credentials start at a random session version, so a cookie of a
            # deleted user can never match a re-created account of the same name.
            cred = data.users.get(username) or Credential(
                session_version=secrets.randbelow(2**31) + 1
            )
            change(cred)
            data.users[username] = cred
            atomic_write(self.path, data.model_dump_json(indent=2) + "\n")
            return cred

    def remove(self, username: str) -> bool:
        """Delete the credentials of ``username``. True if there were any."""
        if not self.path.exists():
            return False
        with locked(self.path):
            data = self.read()
            if data.users.pop(username, None) is None:
                return False
            atomic_write(self.path, data.model_dump_json(indent=2) + "\n")
            return True

    def find_by_invite(self, token: str) -> tuple[str, Credential] | None:
        token_hash = hash_token(token)
        for username, cred in self.read().users.items():
            if cred.invite and hmac.compare_digest(cred.invite.token_hash, token_hash):
                return username, cred
        return None


def load_or_create_secret(data_dir: Path) -> bytes:
    ensure_private_dir(data_dir)
    path = data_dir / "secret.key"
    with locked(path):
        if not path.exists():
            atomic_write(path, secrets.token_bytes(64))
            log.info("secret.created")
        return path.read_bytes()


# =========================================================================== passwords & invites

_hasher = PasswordHasher()  # argon2id with library defaults
_dummy_hash: str | None = None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-ish time: hashes a dummy when there is no stored hash."""
    global _dummy_hash
    if password_hash is None:
        if _dummy_hash is None:
            _dummy_hash = _hasher.hash(secrets.token_urlsafe(16))
        password_hash = _dummy_hash
        result = False
    else:
        result = True
    try:
        _hasher.verify(password_hash, password[:MAX_PASSWORD_LENGTH])
    except (VerificationError, InvalidHashError):
        return False
    return result


def password_problem(password: str, repeat: str) -> str | None:
    """Locale key of the problem with a new password, or None if acceptable."""
    if password != repeat:
        return "auth.password_mismatch"
    if len(password) < MIN_PASSWORD_LENGTH:
        return "auth.password_too_short"
    if len(password) > MAX_PASSWORD_LENGTH:
        return "auth.password_too_long"
    return None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AccountService:
    """User lifecycle on top of the directory and the credential store."""

    def __init__(self, config: Config, directory: UserDirectory, store: CredentialStore):
        self.config = config
        self.directory = directory
        self.store = store

    def invite_link(self, token: str) -> str:
        return f"{self.config.base_url.rstrip('/')}/invite/{token}"

    def create_user(
        self, username: str, name: str, role: Role, workbooks: list[str] | None = None
    ) -> str:
        """Add the user to users.yaml and return a fresh invite link."""
        if not is_valid_username(username):
            raise ValueError(f"Invalid username: {username!r}")
        self.directory.load()
        self.directory.add(username, name, role, workbooks)
        return self.reset_access(username)

    def reset_access(self, username: str) -> str:
        """New invite, password removed, all sessions invalidated."""
        if not is_valid_username(username):
            raise ValueError(f"Invalid username: {username!r}")
        token = secrets.token_urlsafe(32)
        expires = utcnow() + timedelta(hours=self.config.invite_valid_hours)

        def change(cred: Credential) -> None:
            cred.password_hash = None
            cred.session_version += 1
            cred.invite = Invite(token_hash=hash_token(token), expires_at=expires)
            cred.failed_attempts = 0
            cred.locked_until = None

        self.store.update(username, change)
        log.info("auth.invite_created user=%s", username)
        return self.invite_link(token)

    def set_active(self, username: str, active: bool) -> None:
        self.directory.set_active(username, active)
        log.info("auth.user_%s user=%s", "activated" if active else "deactivated", username)

    def valid_invite(self, token: str) -> str | None:
        """Username for a valid, unexpired invite token, else None."""
        if not token or len(token) > 128:
            return None
        found = self.store.find_by_invite(token)
        if found is None:
            return None
        username, cred = found
        user = self.directory.get(username)
        if user is None or not user.active or cred.invite is None:
            return None
        if cred.invite.expires_at <= utcnow():
            return None
        return username

    def accept_invite(self, token: str, password: str) -> str | None:
        """Set the password for a valid invite (single use). Returns the username."""
        username = self.valid_invite(token)
        if username is None:
            return None
        token_hash = hash_token(token)
        accepted = False

        def change(cred: Credential) -> None:
            nonlocal accepted
            # Re-check under the lock so two parallel requests cannot both use it.
            if cred.invite is None or not hmac.compare_digest(cred.invite.token_hash, token_hash):
                return
            cred.password_hash = hash_password(password)
            cred.invite = None
            cred.failed_attempts = 0
            cred.locked_until = None
            accepted = True

        self.store.update(username, change)
        if accepted:
            log.info("auth.invite_accepted user=%s", username)
        return username if accepted else None

    def change_password(self, username: str, password: str) -> None:
        """Store a new password and end all sessions (the caller re-issues its own)."""

        def change(cred: Credential) -> None:
            cred.password_hash = hash_password(password)
            cred.session_version += 1
            cred.failed_attempts = 0
            cred.locked_until = None

        self.store.update(username, change)
        log.info("auth.password_changed user=%s", username)

    def delete_user(self, username: str, progress: ProgressStore, by: str) -> None:
        """Remove the user for good: users.yaml entry (and Fachbetreuer assignments
        pointing to them), credentials and every progress file."""
        if not is_valid_username(username):
            raise ValueError(f"Invalid username: {username!r}")
        # users.yaml first: the user's sessions end as soon as the entry is gone.
        self.directory.remove(username)
        self.store.remove(username)
        progress.delete_user(username)
        log.info("auth.user_deleted user=%s by=%s", username, by)

    def status(self, username: str) -> str:
        """``active`` | ``invited`` | ``invite_expired`` | ``no_access`` | ``inactive``."""
        user = self.directory.get(username)
        cred = self.store.get(username)
        if user is not None and not user.active:
            return "inactive"
        if cred and cred.password_hash:
            return "active"
        if cred and cred.invite:
            return "invited" if cred.invite.expires_at > utcnow() else "invite_expired"
        return "no_access"


# =========================================================================== login & rate limits


class IpRateLimiter:
    """In-memory sliding window of failed logins per client IP."""

    def __init__(self, limit: int = IP_MAX_FAILURES, window: float = IP_WINDOW_SECONDS):
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> deque[float]:
        hits = self._hits.setdefault(ip, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits

    def blocked(self, ip: str) -> bool:
        with self._lock:
            hits = self._hits.get(ip)
            if hits is None:
                return False
            return len(self._prune(ip, time.monotonic())) >= self.limit

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(ip, now).append(now)
            if len(self._hits) > 1000:  # forget IPs whose window has passed
                for key in [
                    k for k, hits in self._hits.items() if not hits or hits[-1] <= now - self.window
                ]:
                    del self._hits[key]


def client_ip(request: Request) -> str:
    """The client IP. X-Forwarded-For is trusted only from the local reverse proxy."""
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and _is_loopback(peer):
        last = forwarded.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            pass
    return peer or "unknown"


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "testclient"


class LoginService:
    def __init__(self, config: Config, accounts: AccountService, limiter: IpRateLimiter):
        self.config = config
        self.accounts = accounts
        self.limiter = limiter

    def authenticate(self, username: str, password: str, ip: str) -> User | None:
        username = (username or "").strip().lower()
        if self.limiter.blocked(ip):
            self._fail(username, ip, None)
            return None
        valid_name = is_valid_username(username)
        user = self.accounts.directory.get(username) if valid_name else None
        cred = self.accounts.store.get(username) if valid_name else None
        now = utcnow()
        locked_out = bool(cred and cred.locked_until and cred.locked_until > now)
        password_ok = verify_password(cred.password_hash if cred else None, password)
        if password_ok and not locked_out and user is not None and user.active:
            if cred and (cred.failed_attempts or cred.locked_until):
                self.accounts.store.update(username, _reset_failures)
            return user
        self._fail(username, ip, cred if user is not None else None)
        return None

    def check_current_password(self, username: str, password: str, ip: str) -> bool:
        """Re-check the password of a logged-in user (e.g. before changing it).

        A wrong password counts like a failed login (per-user lockout, IP limit).
        """
        cred = self.accounts.store.get(username)
        locked_out = bool(cred and cred.locked_until and cred.locked_until > utcnow())
        blocked = self.limiter.blocked(ip)
        password_ok = verify_password(cred.password_hash if cred else None, password)
        if password_ok and not locked_out and not blocked:
            return True
        self._fail(username, ip, cred, event="auth.password_change_failed")
        return False

    def _fail(
        self, username: str, ip: str, cred: Credential | None, event: str = "auth.login_failed"
    ) -> None:
        self.limiter.record_failure(ip)
        shown = username if is_valid_username(username) else "-"
        log.warning("%s user=%s ip=%s", event, shown, ip)
        if cred is None:
            return
        max_attempts = self.config.login_max_attempts
        lockout = timedelta(minutes=self.config.login_lockout_minutes)

        def change(c: Credential) -> None:
            if c.locked_until and c.locked_until > utcnow():
                return
            c.failed_attempts += 1
            if c.failed_attempts >= max_attempts:
                c.failed_attempts = 0
                c.locked_until = utcnow() + lockout
                log.warning("auth.locked user=%s", username)

        self.accounts.store.update(username, change)


def _reset_failures(cred: Credential) -> None:
    cred.failed_attempts = 0
    cred.locked_until = None


# =========================================================================== username suggestion

_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _slug(text: str) -> str:
    text = text.lower().translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", text)


def suggest_username(full_name: str, existing: Iterable[str]) -> str:
    """First letter of the first name + last name, transliterated; -2, -3 … on collision."""
    parts = [p for p in (_slug(part) for part in full_name.split()) if p]
    if not parts:
        base = "user"
    elif len(parts) == 1:
        base = parts[0]
    else:
        base = parts[0][0] + parts[-1]
    base = base[:56] or "user"
    if not base[0].isalnum():
        base = "u" + base
    taken = set(existing)
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


# =========================================================================== sessions & CSRF


@dataclass
class Identity:
    username: str
    user: User

    @property
    def is_trainer(self) -> bool:
        return self.user.role == "trainer"


class Sessions:
    def __init__(self, config: Config, secret: bytes):
        self.idle = timedelta(minutes=config.session_idle_minutes)
        self.secure = config.secure_cookies
        prefix = "__Host-" if self.secure else ""
        self.session_cookie = f"{prefix}session"
        self.csrf_cookie = f"{prefix}csrf"
        self._serializer = URLSafeSerializer(secret, salt="workbook-session")
        self._csrf_key = hmac.new(secret, b"workbook-csrf", hashlib.sha256).digest()

    # ------------------------------------------------------------------ session cookie

    def encode(self, username: str, session_version: int, issued_at: float) -> str:
        now = time.time()
        payload = {"user": username, "session_version": session_version,
                   "issued_at": int(issued_at), "last_seen": int(now)}  # fmt: skip
        return self._serializer.dumps(payload)

    def decode(self, value: str | None) -> dict[str, Any] | None:
        if not value or len(value) > 4096:
            return None
        try:
            payload = self._serializer.loads(value)
        except BadSignature:
            return None
        if not isinstance(payload, dict):
            return None
        if not is_valid_username(payload.get("user")):
            return None
        if not all(isinstance(payload.get(k), int) for k in ("session_version", "last_seen")):
            return None
        return payload

    def cookie_args(self, max_age: int | None) -> dict[str, Any]:
        return {"httponly": True, "secure": self.secure, "samesite": "strict", "path": "/",
                "max_age": max_age}  # fmt: skip

    # ------------------------------------------------------------------ CSRF

    def csrf_token(self, cookie_value: str) -> str:
        return hmac.new(self._csrf_key, cookie_value.encode(), hashlib.sha256).hexdigest()

    def csrf_valid(self, cookie_value: str | None, token: str | None) -> bool:
        if not cookie_value or not token:
            return False
        return hmac.compare_digest(self.csrf_token(cookie_value), token)


class AuthMiddleware(BaseHTTPMiddleware):
    """Resolves the current user once per request (the ONLY place that does it)
    and maintains the session and CSRF cookies."""

    async def dispatch(self, request: Request, call_next) -> Response:
        state = request.app.state
        sessions: Sessions = state.sessions
        request.state.identity = None
        request.state.set_session = None  # (value, max_age) or "clear"

        csrf_value = request.cookies.get(sessions.csrf_cookie)
        new_csrf = None
        if not csrf_value or len(csrf_value) > 128:
            csrf_value = new_csrf = secrets.token_urlsafe(32)
        request.state.csrf_value = csrf_value

        if state.config.auth_mode == "local":
            self._resolve_local(request, sessions)

        response = await call_next(request)

        if new_csrf:
            response.set_cookie(sessions.csrf_cookie, new_csrf, **sessions.cookie_args(None))
        action = request.state.set_session
        if action == "clear":
            response.delete_cookie(
                sessions.session_cookie, path="/", secure=sessions.secure, httponly=True,
                samesite="strict",
            )  # fmt: skip
        elif action:
            value, max_age = action
            response.set_cookie(sessions.session_cookie, value, **sessions.cookie_args(max_age))
        return response

    @staticmethod
    def _resolve_local(request: Request, sessions: Sessions) -> None:
        payload = sessions.decode(request.cookies.get(sessions.session_cookie))
        if payload is None:
            if request.cookies.get(sessions.session_cookie):
                request.state.set_session = "clear"
            return
        now = time.time()
        if now - payload["last_seen"] > sessions.idle.total_seconds():
            request.state.set_session = "clear"
            return
        username = payload["user"]
        state = request.app.state
        user = state.users.get(username)
        cred = state.credentials.get(username)
        if (
            user is None
            or not user.active
            or cred is None
            or not cred.password_hash
            or cred.session_version != payload["session_version"]
        ):
            request.state.set_session = "clear"
            return
        request.state.identity = Identity(username=username, user=user)
        if now - payload["last_seen"] >= SESSION_REFRESH_SECONDS:
            value = sessions.encode(username, cred.session_version, payload.get("issued_at", now))
            request.state.set_session = (value, int(sessions.idle.total_seconds()))


def start_session(request: Request, username: str) -> None:
    """Mark the response to carry a fresh session cookie for ``username``."""
    sessions: Sessions = request.app.state.sessions
    cred = request.app.state.credentials.get(username)
    version = cred.session_version if cred else 1
    value = sessions.encode(username, version, time.time())
    request.state.set_session = (value, int(sessions.idle.total_seconds()))


def end_session(request: Request) -> None:
    request.state.set_session = "clear"
    request.state.identity = None


# =========================================================================== dependencies


class LoginRequired(Exception):
    """Raised by ``get_current_user`` when nobody is logged in."""


class Forbidden(Exception):
    """Raised when the current user lacks the required role."""


class CsrfFailed(Exception):
    """Raised when Origin or CSRF token checks fail."""


def get_current_user(request: Request) -> Identity:
    identity: Identity | None = getattr(request.state, "identity", None)
    if identity is None:
        raise LoginRequired
    return identity


def require_trainer(request: Request) -> Identity:
    identity = get_current_user(request)
    if not identity.is_trainer:
        raise Forbidden
    return identity


def check_origin(request: Request) -> None:
    if request.headers.get("origin") != request.app.state.config.origin:
        raise CsrfFailed


async def verify_form(request: Request) -> None:
    """For HTML form POSTs: same-origin ``Origin`` header and a valid CSRF token."""
    check_origin(request)
    form = await request.form()
    token = form.get("csrf_token")
    sessions: Sessions = request.app.state.sessions
    cookie = request.cookies.get(sessions.csrf_cookie)
    if not isinstance(token, str) or not sessions.csrf_valid(cookie, token):
        raise CsrfFailed


def verify_json_api(request: Request) -> None:
    """For the JSON API (0.4.0): same-origin ``Origin`` and ``X-Workbook: 1``."""
    check_origin(request)
    if request.headers.get("x-workbook") != "1":
        raise CsrfFailed


def verify_json_api_any(request: Request) -> None:
    """JSON API that also has GET endpoints: reads need ``X-Workbook: 1`` (browsers
    send no Origin on same-origin GET), writes need the full check."""
    if request.method in ("GET", "HEAD"):
        if request.headers.get("x-workbook") != "1":
            raise CsrfFailed
        return
    verify_json_api(request)


def csrf_token_for(request: Request) -> str:
    return request.app.state.sessions.csrf_token(request.state.csrf_value)


def safe_next(target: str | None) -> str:
    """Only allow local absolute paths as redirect targets after login."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    if any(ord(ch) < 32 for ch in target):
        return "/"
    return target
