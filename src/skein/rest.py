"""skein's REST mirror of the MCP tools, under ``<base_path>/api``.

JSON in, JSON out. Errors are ``{"error", "code"}`` with not_found 404,
forbidden 403, invalid 400, conflict 409; a body over 1 MiB is 413
``{"error": "request body too large", "code": "invalid"}`` before any parsing;
anything unexpected is 500 ``{"error": "internal error", "code": "internal"}``.
Every rule lives in the service; handlers only unpack the request and call it
as the member the auth middleware identified.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import CHALLENGE, INTERNAL, UNAUTHORIZED, Audit, log_internal, member_of, result_id, summarize
from .errors import SkeinError
from .service import Skein

STATUS = {"not_found": 404, "forbidden": 403, "invalid": 400, "conflict": 409}
BODY_MAX = 1024 * 1024  # bytes; larger request bodies are refused unread
TOO_LARGE = {"error": "request body too large", "code": "invalid"}

# (method, path, who) for every route, relative to <base_path>/api. Also served as help.
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", "/whoami", "everyone"),
    ("GET", "/inbox?limit=", "everyone"),
    ("GET", "/tasks/{id}", "everyone"),
    ("POST", "/tasks/{id}/claim", "everyone"),
    ("POST", "/tasks/{id}/note {note}", "everyone"),
    ("POST", "/tasks/{id}/result {result, status}", "everyone"),
    ("POST", "/asks {question, context, task_id}", "everyone"),
    ("GET", "/asks/{id}", "everyone"),
    ("POST", "/tasks {member, title, instructions}", "point"),
    ("GET", "/tasks?status=&member=&limit=", "point"),
    ("POST", "/tasks/{id}/follow-up {text}", "point"),
    ("POST", "/tasks/{id}/reassign {member, note}", "point"),
    ("POST", "/tasks/{id}/cancel", "point"),
    ("GET", "/asks?status=&limit=", "point"),
    ("POST", "/asks/{id}/answer {answer}", "point"),
    ("GET", "/members", "point"),
)

Call = Callable[[str, Request, dict[str, Any]], Any]


def error_response(code: str, message: str, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status, headers=headers)


class BodyTooLarge(Exception):
    """The request body is over :data:`BODY_MAX` (answered 413, nothing parsed)."""


async def _read_body(request: Request) -> bytes:
    """The raw body, refusing more than :data:`BODY_MAX` bytes: a larger Content-Length
    is refused before reading, and a streamed body as soon as it passes the cap."""
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:  # absent or malformed: the streamed count below still holds
        declared = 0
    if declared > BODY_MAX:
        raise BodyTooLarge
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > BODY_MAX:
            raise BodyTooLarge
    return bytes(raw)


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await _read_body(request)
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
        # Lone surrogates parse but can't be stored; refuse them here as malformed input.
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError):
        raise SkeinError("invalid", "the body must be a JSON object (UTF-8)") from None
    if not isinstance(body, dict):
        raise SkeinError("invalid", "the body must be a JSON object")
    return body


def _int_param(request: Request, name: str) -> int | None:
    value = request.query_params.get(name)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value.strip())
    except ValueError:
        raise SkeinError("invalid", f"{name} must be a whole number") from None


def _endpoint(
    audit: Audit, method: str, label: str, call: Call, *, body: bool = False, wrap: str = ""
) -> Callable[[Request], Any]:
    id_key = "ask_id" if label.startswith("/asks") else "task_id"

    async def endpoint(request: Request) -> JSONResponse:
        member = member_of(request)
        payload: dict[str, Any] = {}
        value: Any = None
        status, code = 200, None
        if member is None:  # the auth middleware should make this unreachable
            response = JSONResponse(UNAUTHORIZED, status_code=401, headers=CHALLENGE)
            status, code = 401, "unauthorized"
        else:
            try:
                payload = await _json_body(request) if body else {}
                value = await run_in_threadpool(call, member, request, payload)
                response = JSONResponse({wrap: value} if wrap else value)
            except BodyTooLarge:
                status, code = 413, "invalid"
                response = JSONResponse(TOO_LARGE, status_code=413)
            except SkeinError as exc:
                status, code = STATUS[exc.code], exc.code
                response = JSONResponse(exc.to_dict(), status_code=status)
            except Exception as exc:  # noqa: BLE001 - never leak an unexpected error to the client
                log_internal(f"{method} {label}", exc)
                status, code = 500, "internal"
                response = JSONResponse(INTERNAL, status_code=500)
        args = dict(payload)
        if "id" in request.path_params:
            args[id_key] = request.path_params["id"]
        ids, lens = summarize(args)
        created = result_id(value) if not wrap else None
        if created and created not in ids.values():
            ids["id"] = created
        audit.log(
            kind="rest", member=member, route=f"{method} {label}", status=status, code=code,
            ids=ids, lens=lens,
        )
        return response

    return endpoint


def routes(skein: Skein, audit: Audit, prefix: str) -> list[Route]:
    """The REST routes, mounted at ``prefix`` (``<base_path>/api``)."""

    def tid(request: Request) -> str:
        return request.path_params["id"]

    table: list[tuple[str, str, Call, dict[str, Any]]] = [
        # everyone
        ("GET", "/whoami", lambda me, r, b: skein.whoami(me), {}),
        ("GET", "/inbox", lambda me, r, b: skein.inbox(me, _int_param(r, "limit")), {}),
        ("GET", "/tasks/{id}", lambda me, r, b: skein.get_task(me, tid(r)), {}),
        ("POST", "/tasks/{id}/claim", lambda me, r, b: skein.claim(me, tid(r)), {}),
        (
            "POST", "/tasks/{id}/note",
            lambda me, r, b: skein.note(me, tid(r), b.get("note")),
            {"body": True},
        ),
        (
            "POST", "/tasks/{id}/result",
            lambda me, r, b: skein.submit(me, tid(r), b.get("result"), b.get("status", "done")),
            {"body": True},
        ),
        (
            "POST", "/asks",
            lambda me, r, b: skein.ask(me, b.get("question"), b.get("context", ""), b.get("task_id", "")),
            {"body": True},
        ),
        ("GET", "/asks/{id}", lambda me, r, b: skein.get_ask(me, r.path_params["id"]), {}),
        # point only (the service refuses everyone else)
        (
            "POST", "/tasks",
            lambda me, r, b: skein.assign(me, b.get("member"), b.get("title"), b.get("instructions")),
            {"body": True},
        ),
        (
            "GET", "/tasks",
            lambda me, r, b: skein.list_tasks(
                me, r.query_params.get("status", ""), r.query_params.get("member", ""),
                _int_param(r, "limit"),
            ),
            {"wrap": "tasks"},
        ),
        (
            "POST", "/tasks/{id}/follow-up",
            lambda me, r, b: skein.follow_up(me, tid(r), b.get("text")),
            {"body": True},
        ),
        (
            "POST", "/tasks/{id}/reassign",
            lambda me, r, b: skein.reassign(me, tid(r), b.get("member"), b.get("note", "")),
            {"body": True},
        ),
        ("POST", "/tasks/{id}/cancel", lambda me, r, b: skein.cancel(me, tid(r)), {}),
        (
            "GET", "/asks",
            lambda me, r, b: skein.list_asks(me, r.query_params.get("status", ""), _int_param(r, "limit")),
            {"wrap": "asks"},
        ),
        (
            "POST", "/asks/{id}/answer",
            lambda me, r, b: skein.answer(me, r.path_params["id"], b.get("answer")),
            {"body": True},
        ),
        ("GET", "/members", lambda me, r, b: skein.members(me), {"wrap": "members"}),
    ]
    return [
        Route(prefix + path, _endpoint(audit, method, path, call, **opts), methods=[method])
        for method, path, call, opts in table
    ]
