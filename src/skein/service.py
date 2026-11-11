"""skein's rules: the one place that decides who may do what.

Transports (MCP, REST) and the CLI call :class:`Skein` and never touch the
store or re-implement a rule. Every member method takes ``caller`` (the
authenticated member's name) first and returns plain JSON-safe values. Refusals
raise :class:`~skein.errors.SkeinError`.

Roles: one member is the **point**. Members talk only to the point: they get
tasks from it and send it asks. The point assigns, follows up, reassigns,
cancels and answers. Members never see each other's tasks or asks; anything
unknown or not theirs is ``not_found``.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .errors import SkeinError
from .store import Store, Tx
from .tokens import hash_token, hashes_match, new_token

OPEN = ("queued", "claimed", "needs_input")
FINAL = ("done", "failed", "cancelled")
STATUSES = OPEN + FINAL
SUBMIT_STATUSES = ("done", "failed", "needs_input")
ASK_STATUSES = ("open", "answered")

TITLE_MAX = 200            # truncate
INSTRUCTIONS_MAX = 8000    # reject
RESULT_MAX = 50_000        # truncate
NOTE_MAX = 2000            # truncate
QUESTION_MAX = 4000        # reject
CONTEXT_MAX = 8000         # truncate
ANSWER_MAX = 50_000        # truncate
DISPLAY_MAX = 100          # truncate
NOTES_KEPT = 50
AMEND_PREVIEW = 1500
RESERVED_NAMES = frozenset({"server", "point", "all", "skein"})
AMEND_WINDOW = 7 * 24 * 3600.0
LIST_MAX = 100
INBOX_MAX = 25
TOKEN_MAX = 512

NAME_RE = re.compile(r"[a-z][a-z0-9_-]{1,31}")
POINT_KEY = "point"
SERVER = "server"

# OAuth clients (PLAN.md C8). Grant and token rules live in oauth.py; these are the
# owner's admin functions and the redirect URI rule both kinds of client share.
OAUTH_CLI = "cli"          # added by the owner with `skein oauth client add`
OAUTH_DCR = "dcr"          # registered by the app itself (RFC 7591)
REDIRECT_URI_MAX = 2000
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
BLOCKED_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "about", "blob", "ftp", "ws", "wss"})
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*")


# -- formatting ----------------------------------------------------------------


def iso(t: float | None) -> str | None:
    """Unix time → ``2027-01-15T08:00:00Z``; ``None`` stays ``None``."""
    if t is None:
        return None
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "assignee": row["assignee"],
        "created_by": row["created_by"],
        "status": row["status"],
        "created": iso(row["created"]),
        "claimed": iso(row["claimed"]),
        "updated": iso(row["updated"]),
        "finished": iso(row["finished"]),
    }


def _task_full(row: dict[str, Any]) -> dict[str, Any]:
    out = _task_summary(row)
    out["instructions"] = row["body"]
    out["notes"] = [{"t": iso(n["t"]), "from": n["from"], "text": n["text"]} for n in row["notes"]]
    out["result"] = row["result"]
    return out


def _ask_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "asker": row["asker"],
        "question": row["question"],
        "context": row["context"],
        "task_id": row["task_id"],
        "status": row["status"],
        "created": iso(row["created"]),
        "answered": iso(row["answered"]),
        "answer": row["answer"],
        "answered_by": row["answered_by"],
    }


# -- input checks --------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, marking the cut with an ellipsis."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(
    value: Any, field: str, limit: int | None = None, *, reject: bool = False, required: bool = True
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SkeinError("invalid", f"{field} must be text")
    if required and not value.strip():
        raise SkeinError("invalid", f"{field} can't be empty")
    if limit is not None and len(value) > limit:
        if reject:
            raise SkeinError(
                "invalid", f"{field} is too long ({len(value)} characters; the limit is {limit})"
            )
        value = _clip(value, limit)
    return value


def _limit(value: Any, default: int, hi: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise SkeinError("invalid", "limit must be a whole number")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise SkeinError("invalid", "limit must be a whole number") from None
    return max(1, min(hi, n))


def _ref(value: Any) -> str:
    """An id or name argument; anything that isn't a non-empty string matches nothing."""
    return value if isinstance(value, str) else ""


def _append_note(notes: list[dict[str, Any]], t: float, frm: str, text: str) -> list[dict[str, Any]]:
    return (list(notes) + [{"t": t, "from": frm, "text": text}])[-NOTES_KEPT:]


def is_loopback(host: str | None) -> bool:
    """A loopback host; an IPv6 literal may keep its URL brackets (``[::1]``, as pydantic gives it)."""
    host = (host or "").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host in LOOPBACK_HOSTS


def check_redirect_uri(uri: Any) -> str:
    """An OAuth redirect URI skein will send codes to, or ``invalid``.

    Allowed: https with a host; http only to a loopback host (RFC 8252 §7.3); a private-use
    scheme such as ``com.example.app:/cb`` (RFC 8252 §7.1). Refused: fragments (RFC 6749
    §3.1.2), plain http elsewhere, and schemes a browser would run or read locally.
    """
    if not isinstance(uri, str) or not uri or len(uri) > REDIRECT_URI_MAX:
        raise SkeinError("invalid", "a redirect URI must be a URL of at most 2000 characters")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in uri):
        raise SkeinError("invalid", "a redirect URI can't contain spaces or control characters")
    if "#" in uri:
        raise SkeinError("invalid", "a redirect URI can't have a #fragment")
    try:
        parts = urlsplit(uri)
        host = parts.hostname
    except ValueError:
        raise SkeinError("invalid", f"not a valid redirect URI: {uri[:100]}") from None
    scheme = parts.scheme.lower()
    if not _SCHEME_RE.fullmatch(scheme) or not (parts.netloc or parts.path):
        raise SkeinError("invalid", f"not a valid redirect URI: {uri[:100]}")
    if scheme in BLOCKED_SCHEMES:
        raise SkeinError("invalid", f"redirect URIs can't use {scheme}:")
    if scheme == "https" and not host:
        raise SkeinError("invalid", "an https redirect URI needs a host")
    if scheme == "http" and not is_loopback(host):
        raise SkeinError(
            "invalid", "a redirect URI must use https (plain http only to localhost or 127.0.0.1)"
        )
    if parts.username is not None or parts.password is not None:
        raise SkeinError("invalid", "a redirect URI can't contain credentials")
    return uri


class Skein:
    """The service. ``clock`` is injectable so tests can move time."""

    def __init__(
        self, store: Store | str | os.PathLike[str], clock: Callable[[], float] = time.time
    ) -> None:
        self.store = store if isinstance(store, Store) else Store(store)
        self.clock = clock

    def _now(self) -> float:
        return float(self.clock())

    # -- shared checks ---------------------------------------------------------

    @staticmethod
    def _caller(db: Tx, caller: Any) -> dict[str, Any]:
        row = db.get_member(caller) if isinstance(caller, str) and caller else None
        if row is None or row["disabled"]:
            raise SkeinError("forbidden", "unknown or disabled member")
        return row

    @staticmethod
    def _point(db: Tx) -> str | None:
        return db.get_meta(POINT_KEY) or None

    def _is_point(self, db: Tx, caller: str) -> bool:
        self._caller(db, caller)
        return self._point(db) == caller

    def _require_point(self, db: Tx, caller: str, action: str) -> None:
        if not self._is_point(db, caller):
            point = self._point(db)
            who = f" ({point})" if point else ""
            raise SkeinError(
                "forbidden",
                f"only the point{who} can {action}; members use skein_inbox, skein_submit "
                "and skein_ask",
            )

    @staticmethod
    def _task(db: Tx, caller: str, task_id: Any, is_point: bool) -> dict[str, Any]:
        """A task the caller may see: the point sees all, a member only its own."""
        tid = _ref(task_id)
        row = db.get_task(tid) if tid else None
        if row is None or (not is_point and row["assignee"] != caller):
            raise SkeinError("not_found", f"no task {tid or '(none)'}")
        return row

    def _target(self, db: Tx, member: Any) -> str:
        name = _ref(member)
        if not name:
            raise SkeinError("invalid", "member is required")
        row = db.get_member(name)
        if row is None:
            raise SkeinError("not_found", f"no member named {name}")
        if row["disabled"]:
            raise SkeinError("conflict", f"{name} is disabled")
        if self._point(db) == name:
            raise SkeinError(
                "conflict", f"{name} is the point; the point routes work, it isn't assigned it"
            )
        return name

    @staticmethod
    def _in_amend_window(row: dict[str, Any], now: float) -> bool:
        if row["status"] not in ("done", "failed"):
            return False
        finished = row["finished"] if row["finished"] is not None else row["updated"]
        return finished is not None and now - finished <= AMEND_WINDOW

    def _accepts_changes(self, row: dict[str, Any], now: float, what: str) -> None:
        """Open tasks accept changes; done/failed only inside the amend window."""
        status = row["status"]
        if status in OPEN:
            return
        if status == "cancelled":
            raise SkeinError("conflict", f"task {row['id']} is cancelled; it accepts nothing")
        if not self._in_amend_window(row, now):
            raise SkeinError(
                "conflict",
                f"task {row['id']} finished more than 7 days ago; it no longer accepts {what}",
            )

    @staticmethod
    def _members_list(db: Tx) -> list[dict[str, Any]]:
        counts = db.count_tasks_by_assignee(OPEN)
        return [
            {
                "name": m["name"],
                "display": m["display"],
                "last_seen": iso(m["last_seen"]),
                "disabled": bool(m["disabled"]),
                "open_tasks": counts.get(m["name"], 0),
            }
            for m in db.list_members()
        ]

    @staticmethod
    def _new_id(prefix: str, taken: Callable[[str], Any]) -> str:
        """``prefix`` + 10 hex. Called inside a write transaction, so check-then-insert holds."""
        for _ in range(20):
            new_id = prefix + secrets.token_hex(5)
            if not taken(new_id):
                return new_id
        raise RuntimeError("could not allocate a unique id")

    # -- everyone --------------------------------------------------------------

    def whoami(self, caller: str) -> dict[str, Any]:
        """Who the caller is, their role, and who the point is."""
        with self.store.tx(write=False) as db:
            me = self._caller(db, caller)
            point = self._point(db)
            point_row = db.get_member(point) if point else None
            out: dict[str, Any] = {
                "member": me["name"],
                "display": me["display"],
                "role": "point" if point == caller else "member",
                "point": point,
                "point_display": point_row["display"] if point_row else None,
            }
            if point == caller:
                out["members"] = self._members_list(db)
            return out

    def inbox(self, caller: str, limit: Any = 10) -> dict[str, Any]:
        """What needs the caller's attention. Marks returned answers/results as seen."""
        n = _limit(limit, 10, INBOX_MAX)
        now = self._now()
        with self.store.tx() as db:
            self._caller(db, caller)
            db.update_member(caller, last_seen=now)
            point = self._point(db)
            if point == caller:
                asks = db.list_asks(statuses=("open",), limit=n)
                needs = db.list_tasks(statuses=("needs_input",), limit=n)
                finished = db.list_tasks(statuses=("done", "failed"), seen_by_point=0, limit=n)
                for row in finished:
                    db.update_task(row["id"], seen_by_point=1)
                return {
                    "role": "point",
                    "asks": [_ask_dict(a) for a in asks],
                    "needs_input": [_task_full(t) for t in needs],
                    "finished": [_task_full(t) for t in finished],
                    "checked_in_at": iso(now),
                }
            tasks = db.list_tasks(statuses=("queued", "claimed"), assignee=caller, limit=n)
            answers = db.list_asks(statuses=("answered",), asker=caller, seen_by_asker=0, limit=n)
            for row in answers:
                db.update_ask(row["id"], seen_by_asker=1)
            return {
                "role": "member",
                "point": point,
                "tasks": [_task_full(t) for t in tasks],
                "answers": [_ask_dict(a) for a in answers],
                "checked_in_at": iso(now),
            }

    def get_task(self, caller: str, task_id: str) -> dict[str, Any]:
        """One task in full. Members see only tasks assigned to them; the point sees all."""
        with self.store.tx(write=False) as db:
            is_point = self._is_point(db, caller)
            return _task_full(self._task(db, caller, task_id, is_point))

    def claim(self, caller: str, task_id: str) -> dict[str, Any]:
        """The assignee starts a queued task (claiming again is harmless)."""
        now = self._now()
        with self.store.tx() as db:
            row = self._task(db, caller, task_id, self._is_point(db, caller))
            if row["assignee"] != caller:
                raise SkeinError("forbidden", "only the assignee can claim this task")
            if row["status"] not in ("queued", "claimed"):
                raise SkeinError(
                    "conflict", f"task {row['id']} is {row['status']}; only queued tasks can be claimed"
                )
            fields: dict[str, Any] = {"status": "claimed", "updated": now}
            if row["claimed"] is None:
                fields["claimed"] = now
            db.update_task(row["id"], **fields)
            return _task_full(db.get_task(row["id"]))  # type: ignore[arg-type]

    def note(self, caller: str, task_id: str, text: str) -> dict[str, Any]:
        """Add a progress note (assignee or point)."""
        now = self._now()
        with self.store.tx() as db:
            row = self._task(db, caller, task_id, self._is_point(db, caller))
            text = _text(text, "note", NOTE_MAX)
            self._accepts_changes(row, now, "notes")
            notes = _append_note(row["notes"], now, caller, text)
            db.update_task(row["id"], notes=notes, updated=now)
            return _task_full(db.get_task(row["id"]))  # type: ignore[arg-type]

    def submit(
        self, caller: str, task_id: str, result: str, status: str = "done"
    ) -> dict[str, Any]:
        """The assignee reports done, failed, or needs_input (with the question as result)."""
        now = self._now()
        with self.store.tx() as db:
            row = self._task(db, caller, task_id, self._is_point(db, caller))
            if row["assignee"] != caller:
                raise SkeinError("forbidden", "only the assignee can submit a result")
            if status not in SUBMIT_STATUSES:
                raise SkeinError("invalid", "status must be done, failed or needs_input")
            result = _text(result, "result", RESULT_MAX)
            if row["status"] == "needs_input":
                raise SkeinError(
                    "conflict",
                    f"task {row['id']} is waiting on the point (needs_input); it comes back "
                    "to your inbox after a follow-up",
                )
            self._accepts_changes(row, now, "results")
            notes = row["notes"]
            if row["result"]:
                verb = "amended" if row["status"] in FINAL else "replaced"
                notes = _append_note(
                    notes, now, SERVER, f"result {verb}; previous: " + row["result"][:AMEND_PREVIEW]
                )
            fields: dict[str, Any] = {
                "status": status,
                "result": result,
                "notes": notes,
                "updated": now,
                "finished": now if status in ("done", "failed") else None,
                "seen_by_point": 0,
            }
            if row["claimed"] is None:
                fields["claimed"] = now
            db.update_task(row["id"], **fields)
            return _task_full(db.get_task(row["id"]))  # type: ignore[arg-type]

    def ask(self, caller: str, question: str, context: str = "", task_id: str = "") -> dict[str, Any]:
        """A member asks the point a question, optionally about one of its tasks."""
        now = self._now()
        with self.store.tx() as db:
            self._caller(db, caller)
            point = self._point(db)
            if point is None:
                raise SkeinError(
                    "conflict", "no point is set yet, so there's nobody to ask; tell the owner"
                )
            if point == caller:
                raise SkeinError(
                    "forbidden",
                    "you are the point: the point answers asks, it doesn't send them. "
                    "Assign a task or follow up instead.",
                )
            question = _text(question, "question", QUESTION_MAX, reject=True)
            context = _text(context, "context", CONTEXT_MAX, required=False)
            tid = ""
            if task_id is not None and task_id != "":
                tid = self._task(db, caller, task_id, False)["id"]
            row = {
                "asker": caller,
                "question": question,
                "context": context,
                "task_id": tid,
                "status": "open",
                "created": now,
                "seen_by_asker": 0,
            }
            ask_id = self._new_id("a_", db.get_ask)
            db.insert_ask({**row, "id": ask_id})
            return _ask_dict(db.get_ask(ask_id))  # type: ignore[arg-type]

    def get_ask(self, caller: str, ask_id: str) -> dict[str, Any]:
        """One ask (asker or point). The asker reading an answer marks it seen."""
        with self.store.tx() as db:
            is_point = self._is_point(db, caller)
            aid = _ref(ask_id)
            row = db.get_ask(aid) if aid else None
            if row is None or (not is_point and row["asker"] != caller):
                raise SkeinError("not_found", f"no ask {aid or '(none)'}")
            if row["asker"] == caller and row["status"] == "answered" and not row["seen_by_asker"]:
                db.update_ask(row["id"], seen_by_asker=1)
            return _ask_dict(row)

    # -- point only ------------------------------------------------------------

    def assign(self, caller: str, member: str, title: str, instructions: str) -> dict[str, Any]:
        """The point gives a member a task."""
        now = self._now()
        with self.store.tx() as db:
            self._require_point(db, caller, "assign tasks")
            target = self._target(db, member)
            title = _clip(" ".join(_text(title, "title").split()), TITLE_MAX)
            instructions = _text(instructions, "instructions", INSTRUCTIONS_MAX, reject=True)
            row = {
                "title": title,
                "body": instructions,
                "assignee": target,
                "created_by": caller,
                "status": "queued",
                "created": now,
                "updated": now,
                "claimed": None,
                "finished": None,
                "result": None,
                "notes": [],
                "seen_by_point": 1,
            }
            task_id = self._new_id("t_", db.get_task)
            db.insert_task({**row, "id": task_id})
            return _task_full(db.get_task(task_id))  # type: ignore[arg-type]

    def answer(self, caller: str, ask_id: str, text: str) -> dict[str, Any]:
        """The point answers an ask (answering again overwrites)."""
        now = self._now()
        with self.store.tx() as db:
            self._require_point(db, caller, "answer asks")
            aid = _ref(ask_id)
            row = db.get_ask(aid) if aid else None
            if row is None:
                raise SkeinError("not_found", f"no ask {aid or '(none)'}")
            text = _text(text, "answer", ANSWER_MAX)
            db.update_ask(
                row["id"], status="answered", answer=text, answered_by=caller, answered=now,
                seen_by_asker=0,
            )
            return _ask_dict(db.get_ask(row["id"]))  # type: ignore[arg-type]

    def follow_up(self, caller: str, task_id: str, text: str) -> dict[str, Any]:
        """The point adds instructions and puts the task back in the queue."""
        now = self._now()
        with self.store.tx() as db:
            self._require_point(db, caller, "follow up on tasks")
            row = self._task(db, caller, task_id, True)
            text = _text(text, "follow-up", NOTE_MAX)
            self._accepts_changes(row, now, "follow-ups")
            notes = _append_note(row["notes"], now, caller, text)
            db.update_task(row["id"], notes=notes, status="queued", finished=None, updated=now)
            return _task_full(db.get_task(row["id"]))  # type: ignore[arg-type]

    def reassign(self, caller: str, task_id: str, member: str, note: str = "") -> dict[str, Any]:
        """The point moves an open task to another member; it goes back to queued."""
        now = self._now()
        with self.store.tx() as db:
            self._require_point(db, caller, "reassign tasks")
            row = self._task(db, caller, task_id, True)
            if row["status"] not in OPEN:
                raise SkeinError(
                    "conflict", f"task {row['id']} is {row['status']}; only open tasks can be reassigned"
                )
            target = self._target(db, member)
            extra = _text(note, "note", NOTE_MAX, required=False).strip()
            text = f"reassigned from {row['assignee']} to {target}" + (f": {extra}" if extra else "")
            notes = _append_note(row["notes"], now, caller, _clip(text, NOTE_MAX))
            db.update_task(
                row["id"], assignee=target, status="queued", claimed=None, notes=notes, updated=now
            )
            return _task_full(db.get_task(row["id"]))  # type: ignore[arg-type]

    def cancel(self, caller: str, task_id: str) -> dict[str, Any]:
        """The point cancels an open task. Final tasks are left alone."""
        now = self._now()
        with self.store.tx() as db:
            self._require_point(db, caller, "cancel tasks")
            row = self._task(db, caller, task_id, True)
            if row["status"] in FINAL:
                return {"id": row["id"], "status": row["status"], "note": "already final"}
            db.update_task(row["id"], status="cancelled", finished=now, updated=now)
            return _task_full(db.get_task(row["id"]))  # type: ignore[arg-type]

    def list_tasks(
        self, caller: str, status: str = "", member: str = "", limit: Any = 20
    ) -> list[dict[str, Any]]:
        """Task summaries, newest first. ``status`` may be a status or ``open``."""
        n = _limit(limit, 20, LIST_MAX)
        with self.store.tx(write=False) as db:
            self._require_point(db, caller, "list tasks")
            if not status:
                statuses = None
            elif status == "open":
                statuses = OPEN
            elif status in STATUSES:
                statuses = (status,)
            else:
                raise SkeinError("invalid", f"status must be one of: open, {', '.join(STATUSES)}")
            if member is not None and not isinstance(member, str):
                raise SkeinError("invalid", "member must be a name")
            rows = db.list_tasks(
                statuses=statuses, assignee=member or None, newest_first=True, limit=n
            )
            return [_task_summary(r) for r in rows]

    def list_asks(self, caller: str, status: str = "open", limit: Any = 20) -> list[dict[str, Any]]:
        """Asks: ``open`` (default) or ``answered`` oldest first; ``all`` newest first."""
        n = _limit(limit, 20, LIST_MAX)
        with self.store.tx(write=False) as db:
            self._require_point(db, caller, "list asks")
            status = status or "open"
            if status == "all":
                rows = db.list_asks(newest_first=True, limit=n)
            elif status in ASK_STATUSES:
                rows = db.list_asks(statuses=(status,), limit=n)
            else:
                raise SkeinError("invalid", "status must be open, answered or all")
            return [_ask_dict(r) for r in rows]

    def members(self, caller: str) -> list[dict[str, Any]]:
        """Every member with last check-in and open task count."""
        with self.store.tx(write=False) as db:
            self._require_point(db, caller, "list members")
            return self._members_list(db)

    # -- admin (CLI only; no caller) -------------------------------------------

    def add_member(self, name: str, display: str = "") -> dict[str, Any]:
        """Create a member. The plaintext token is returned once and never stored."""
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise SkeinError(
                "invalid",
                "member names are 2-32 characters: a lowercase letter, then lowercase "
                "letters, digits, - or _",
            )
        if name in RESERVED_NAMES:
            raise SkeinError("invalid", f"{name} is a reserved name; pick another")
        if display is None:
            display = ""
        if not isinstance(display, str):
            raise SkeinError("invalid", "display must be text")
        display = _clip(" ".join(display.split()), DISPLAY_MAX) or name
        token = new_token()
        now = self._now()
        with self.store.tx() as db:
            if db.get_member(name) is not None:
                raise SkeinError("conflict", f"a member named {name} already exists")
            db.insert_member(name, display, hash_token(token), now)
        return {"name": name, "display": display, "token": token}

    def rotate_token(self, name: str) -> dict[str, Any]:
        """Replace a member's token; the old one stops working at once, and so does every
        OAuth sign-in made with it (codes and tokens)."""
        token = new_token()
        with self.store.tx() as db:
            if not db.update_member(_ref(name), token_sha256=hash_token(token)):
                raise SkeinError("not_found", f"no member named {name}")
            db.delete_oauth_member(name)
        return {"name": name, "token": token}

    def set_disabled(self, name: str, disabled: bool) -> dict[str, Any]:
        """Disable or re-enable a member. Disabled tokens are refused. Disabling also ends
        the member's OAuth sign-ins for good: re-enabling does not bring them back."""
        with self.store.tx() as db:
            if disabled and _ref(name) and self._point(db) == name:
                raise SkeinError("conflict", "move the point first (skein point set NAME)")
            if not db.update_member(_ref(name), disabled=1 if disabled else 0):
                raise SkeinError("not_found", f"no member named {name}")
            if disabled:
                db.delete_oauth_member(name)
        return {"name": name, "disabled": bool(disabled)}

    def set_point(self, name: str) -> dict[str, Any]:
        """Make a member the point. Existing tasks are left as they are."""
        with self.store.tx() as db:
            row = db.get_member(_ref(name)) if _ref(name) else None
            if row is None:
                raise SkeinError("not_found", f"no member named {name}")
            if row["disabled"]:
                raise SkeinError("conflict", f"{name} is disabled; enable it first")
            previous = self._point(db)
            db.set_meta(POINT_KEY, name)
        return {"point": name, "previous": previous}

    def get_point(self) -> str | None:
        """The point's name, or None if none is set."""
        with self.store.tx(write=False) as db:
            return self._point(db)

    def list_members(self) -> list[dict[str, Any]]:
        """Every member (same shape as the point's ``members``)."""
        with self.store.tx(write=False) as db:
            return self._members_list(db)

    def status(self) -> dict[str, Any]:
        """A summary for the owner: the point, members, and open counts."""
        with self.store.tx(write=False) as db:
            return {
                "point": self._point(db),
                "members": self._members_list(db),
                "open_tasks": db.count_tasks(statuses=OPEN),
                "open_asks": db.count_asks(statuses=("open",)),
            }

    # -- OAuth clients (admin) -------------------------------------------------

    def add_oauth_client(self, name: str, redirect_uris: Iterable[str] = ()) -> dict[str, Any]:
        """Pre-register an OAuth client for an app that takes a client id and secret.

        The secret is returned once and only its hash is kept. With no redirect URIs the
        client locks to the first https redirect URI used in an approved sign-in.
        """
        if not isinstance(name, str) or not name.strip():
            raise SkeinError("invalid", "an OAuth client needs a name")
        name = _clip(" ".join(name.split()), DISPLAY_MAX)
        uris = list(dict.fromkeys(check_redirect_uri(u) for u in (redirect_uris or ())))
        secret = secrets.token_hex(32)
        now = self._now()
        with self.store.tx() as db:
            client_id = self._new_id("cli_", db.get_oauth_client)
            db.insert_oauth_client(
                {
                    "client_id": client_id,
                    "name": name,
                    "source": OAUTH_CLI,
                    "secret_sha256": hash_token(secret),
                    "auth_method": "client_secret_post",
                    "redirect_uris": uris,
                    "created": now,
                }
            )
        return {"client_id": client_id, "client_secret": secret, "name": name, "redirect_uris": uris}

    def list_oauth_clients(self) -> list[dict[str, Any]]:
        """Every OAuth client (never its secret), with how many sign-ins are still live."""
        now = self._now()
        with self.store.tx(write=False) as db:
            live = db.count_oauth_tokens_by_client("refresh", now)
            return [
                {
                    "client_id": c["client_id"],
                    "name": c["name"],
                    "source": c["source"],
                    "redirect_uris": c["redirect_uris"],
                    "created": iso(c["created"]),
                    "sign_ins": live.get(c["client_id"], 0),
                }
                for c in db.list_oauth_clients()
            ]

    def remove_oauth_client(self, client_id: str) -> dict[str, Any]:
        """Remove an OAuth client and revoke everything issued to it."""
        cid = _ref(client_id)
        with self.store.tx() as db:
            row = db.get_oauth_client(cid) if cid else None
            if row is None:
                raise SkeinError("not_found", f"no OAuth client {cid or '(none)'}")
            revoked = db.delete_oauth_client(cid)
        return {"client_id": cid, "name": row["name"], "tokens_revoked": revoked}

    def authenticate(self, token: Any) -> str | None:
        """Token → member name, or None if unknown or the member is disabled."""
        if not isinstance(token, str) or not token or len(token) > TOKEN_MAX:
            return None
        digest = hash_token(token)
        with self.store.tx(write=False) as db:
            row = db.get_member_by_token_hash(digest)
        if row is None or row["disabled"] or not hashes_match(row["token_sha256"], digest):
            return None
        return row["name"]
