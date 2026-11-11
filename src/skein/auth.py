"""Who is calling, and a record of what they did.

:class:`AuthMiddleware` turns the presented token into a member name and puts
it in the ASGI scope (``scope["skein.member"]``), where REST handlers and MCP
tools read it per request. Nothing else decides identity.

:class:`Audit` writes one JSON line per request or tool call. It records ids,
outcomes and the *lengths* of text fields, never their content, and never a
token. A value in an id position is written only when it is shaped like that
kind of id (task id, ask id, member name); anything else is recorded by length,
so free text a caller puts there never reaches the log.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs

import anyio.to_thread
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .service import NAME_RE

MEMBER_KEY = "skein.member"

# Argument names whose values are free text: the audit log keeps only their length.
TEXT_FIELDS = frozenset(
    {"title", "instructions", "note", "result", "question", "context", "text", "answer"}
)
TASK_ID_RE = re.compile(r"t_[0-9a-f]{10}")
ASK_ID_RE = re.compile(r"a_[0-9a-f]{10}")
# Argument names whose values are ids or member names, and the shapes each may take. The
# audit log keeps a value only if it fully matches one of its key's shapes, else its length.
ID_SHAPES: dict[str, tuple[re.Pattern[str], ...]] = {
    "task_id": (TASK_ID_RE,),
    "ask_id": (ASK_ID_RE,),
    "member": (NAME_RE,),
    "id": (TASK_ID_RE, ASK_ID_RE),
}
ID_FIELDS = frozenset(ID_SHAPES)
UA_MAX = 200
PATH_MAX = 200

UNAUTHORIZED = {"error": "unauthorized"}
CHALLENGE = {"WWW-Authenticate": 'Bearer realm="skein"'}
INTERNAL = {"error": "internal error", "code": "internal"}


def member_of(conn: HTTPConnection) -> str | None:
    """The authenticated member name for this request (set by :class:`AuthMiddleware`)."""
    value = conn.scope.get(MEMBER_KEY)
    return value if isinstance(value, str) and value else None


def extract_token(headers: Headers, query_string: bytes, url_tokens: bool) -> str | None:
    """The presented token: ``Authorization: Bearer``, then ``X-API-Key``, then ``?token=``
    (only when ``url_tokens`` is on)."""
    scheme, _, value = headers.get("authorization", "").strip().partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    key = headers.get("x-api-key", "").strip()
    if key:
        return key
    if url_tokens and query_string:
        values = parse_qs(query_string.decode("latin-1")).get("token")
        if values and values[0].strip():
            return values[0].strip()
    return None


class AuthMiddleware:
    """Pure ASGI middleware: every HTTP request except ``open_paths`` needs a member token.

    Failure → 401 ``{"error": "unauthorized"}`` with a ``WWW-Authenticate`` challenge.
    Unknown tokens and disabled members fail the same way.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        authenticate: Callable[[str], str | None],
        url_tokens: bool,
        open_paths: frozenset[str],
        audit: Audit,
    ) -> None:
        self.app = app
        self.authenticate = authenticate
        self.url_tokens = url_tokens
        self.open_paths = open_paths
        self.audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.open_paths:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        token = extract_token(headers, scope.get("query_string", b""), self.url_tokens)
        member = await anyio.to_thread.run_sync(self.authenticate, token) if token else None
        if member is None:
            client = scope.get("client")
            self.audit.log(
                kind="auth_failed",
                reason="invalid" if token else "missing",
                method=scope.get("method"),
                path=(scope.get("path") or "")[:PATH_MAX],
                ua=headers.get("user-agent", "")[:UA_MAX],
                ip=client[0] if client else None,
            )
            response = JSONResponse(UNAUTHORIZED, status_code=401, headers=CHALLENGE)
            await response(scope, receive, send)
            return
        scope[MEMBER_KEY] = member
        await self.app(scope, receive, send)


def id_shaped(key: str, value: str) -> bool:
    """Whether ``value`` is shaped like the id or name the argument ``key`` holds."""
    return any(shape.fullmatch(value) for shape in ID_SHAPES.get(key, ()))


def summarize(args: dict[str, Any]) -> tuple[dict[str, str], dict[str, int]]:
    """Split call arguments into ``ids`` and ``lens`` (lengths only).

    An id field is kept in ``ids`` only when :func:`id_shaped`; otherwise its length goes
    to ``lens`` under the same key, like a text field.
    """
    ids: dict[str, str] = {}
    lens: dict[str, int] = {}
    for key, value in args.items():
        if key in ID_FIELDS and isinstance(value, str) and value:
            if id_shaped(key, value):
                ids[key] = value
            else:
                lens[key] = len(value)
        elif key in TEXT_FIELDS and isinstance(value, str):
            lens[key] = len(value)
    return ids, lens


def result_id(out: Any) -> str | None:
    """The id of the task or ask a call returned, if any."""
    value = out.get("id") if isinstance(out, dict) else None
    return value if isinstance(value, str) and id_shaped("id", value) else None


def _now_iso() -> str:
    now = datetime.fromtimestamp(time.time(), tz=timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class Audit:
    """Appends one JSON object per line to ``path``. ``path=""`` disables it.

    Callers pass ids, outcomes and lengths only. A write failure is reported on
    stderr and never fails the request.
    """

    def __init__(self, path: str = "") -> None:
        self.path = path
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def log(self, **fields: Any) -> None:
        if not self.path:
            return
        record = {"t": _now_iso(), **{k: v for k, v in fields.items() if v not in (None, {}, "")}}
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":"), default=str) + "\n"
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            print(f"skein: audit log write failed: {type(exc).__name__}", file=sys.stderr)


def log_internal(where: str, exc: BaseException) -> None:
    """Report an unexpected exception on stderr: type and frames, never its message
    (a message can quote request text)."""
    frames = traceback.extract_tb(exc.__traceback__)
    trail = " <- ".join(f"{f.name}({f.filename.rsplit('/', 1)[-1]}:{f.lineno})" for f in reversed(frames))
    print(f"skein: internal error in {where}: {type(exc).__name__} at {trail}", file=sys.stderr)
