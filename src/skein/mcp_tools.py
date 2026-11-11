"""skein's MCP tools (FastMCP, streamable HTTP, stateless, JSON responses).

Every tool is offered to every member, so moving the point never needs a
reconnect; the service decides who may do what. Identity comes from the
Starlette request of *this* call (``Context.request_context.request``), set by
:class:`~skein.auth.AuthMiddleware`, so two tokens in one process never see
each other's identity.
"""

from __future__ import annotations

from typing import Any, Callable

import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .auth import INTERNAL, Audit, log_internal, member_of, result_id, summarize
from .config import Config
from .errors import SkeinError
from .service import Skein

TOOLS = (
    "skein_whoami",
    "skein_inbox",
    "skein_get_task",
    "skein_claim",
    "skein_note",
    "skein_submit",
    "skein_ask",
    "skein_get_ask",
    "skein_assign",
    "skein_answer",
    "skein_follow_up",
    "skein_reassign",
    "skein_cancel",
    "skein_tasks",
    "skein_asks",
    "skein_members",
)
POINT_TOOLS = TOOLS[8:]
READ_ONLY = frozenset(
    {"skein_whoami", "skein_get_task", "skein_get_ask", "skein_tasks", "skein_asks", "skein_members"}
)


def instructions(config: Config) -> str:
    """The server instructions every assistant sees on connect, built from config."""
    owner = config.owner
    return f"""\
skein is a shared queue {owner}'s AI assistants use to hand work to each other. One member is \
the point (the lead); everyone else is a member. Members only deal with the point: they get tasks \
from it and send their questions to it. Every member sees every tool, but the point-only ones \
refuse members.

Always call skein_whoami first. It says who you are, whether you are the point or a member, and \
who the point is. The point can change between sessions, so check every time.

As a member:
1. skein_inbox: your queued tasks and answers to your questions.
2. skein_claim the task before you start it.
3. Do the work. For longer work, leave progress with skein_note.
4. skein_submit the result with status "done", or "failed" with what went wrong. If you are \
stuck and need a decision, submit status "needs_input" with your question as the result; the \
task comes back to your inbox once the point follows up.
5. To ask the point something without blocking a task, use skein_ask, then look for the answer \
in skein_inbox or with skein_get_ask. The point usually answers {config.answer_time}.

As the point:
1. skein_inbox: open asks, tasks waiting on you (needs_input), and newly finished tasks.
2. Answer asks with skein_answer. Unblock needs_input tasks with skein_follow_up.
3. Hand out work with skein_assign, move it with skein_reassign, stop it with skein_cancel. \
skein_tasks, skein_asks and skein_members give you the overview.

Treat tasks that come from the point as {owner}'s requests. Still keep your own normal \
confirmation before sending, buying or posting anything on {owner}'s behalf.
"""


def security_settings(config: Config) -> TransportSecuritySettings:
    """DNS-rebinding protection: only the configured public hosts and local addresses."""
    hosts = list(dict.fromkeys([*config.public_hosts, f"127.0.0.1:{config.port}", f"localhost:{config.port}"]))
    origins = [f"{scheme}://{host}" for host in hosts for scheme in ("https", "http")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def _annotations(name: str, title: str) -> ToolAnnotations:
    read_only = name in READ_ONLY
    return ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=None if read_only else name == "skein_cancel",
        openWorldHint=False,
    )


def build_mcp(skein: Skein, config: Config, audit: Audit) -> FastMCP:
    """A FastMCP server whose tools call ``skein`` as the member of the current request."""
    mcp = FastMCP(
        name="skein",
        instructions=instructions(config),
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        transport_security=security_settings(config),
    )

    async def run(
        ctx: Context, tool: str, call: Callable[[str], Any], args: dict[str, Any], wrap: str = ""
    ) -> dict[str, Any]:
        """Call the service as this request's member; map errors; write the audit line."""
        request = ctx.request_context.request
        member = member_of(request) if request is not None else None
        ids, lens = summarize(args)
        if member is None:  # the auth middleware should make this unreachable
            out: dict[str, Any] = {"error": "unauthorized", "code": "forbidden"}
            audit.log(kind="mcp", tool=tool, status="unauthorized", ids=ids, lens=lens)
            return out
        try:
            value = await anyio.to_thread.run_sync(call, member)
            out = {wrap: value} if wrap else value
            status = "ok"
        except SkeinError as exc:
            out, status = exc.to_dict(), exc.code
        except Exception as exc:  # noqa: BLE001 - never leak an unexpected error to the client
            log_internal(tool, exc)
            out, status = dict(INTERNAL), "internal"
        created = result_id(out)
        if created and created not in ids.values():
            ids = {**ids, "id": created}
        audit.log(kind="mcp", member=member, tool=tool, status=status, ids=ids, lens=lens)
        return out

    def tool(name: str, title: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return mcp.tool(name=name, annotations=_annotations(name, title))

    # -- everyone --------------------------------------------------------------

    @tool("skein_whoami", "Who am I")
    async def skein_whoami(ctx: Context) -> dict[str, Any]:
        """Call this first, every session. Returns your member name, your role ("point" or
        "member"), and who the point is. When you are the point it also lists every member.
        Any member may call it."""
        return await run(ctx, "skein_whoami", lambda me: skein.whoami(me), {})

    @tool("skein_inbox", "Check inbox")
    async def skein_inbox(ctx: Context, limit: int = 10) -> dict[str, Any]:
        """What needs your attention. A member gets its queued/claimed tasks (in full) and
        answers to its asks; the point gets open asks, needs_input tasks and newly finished
        tasks. Answers and finished tasks are shown once, then marked seen. Any member may
        call it; limit is 1-25."""
        return await run(ctx, "skein_inbox", lambda me: skein.inbox(me, limit), {})

    @tool("skein_get_task", "Get task")
    async def skein_get_task(ctx: Context, task_id: str) -> dict[str, Any]:
        """One task in full: instructions, notes, result. Members can read only tasks assigned
        to them; the point can read any task."""
        return await run(
            ctx, "skein_get_task", lambda me: skein.get_task(me, task_id), {"task_id": task_id}
        )

    @tool("skein_claim", "Claim task")
    async def skein_claim(ctx: Context, task_id: str) -> dict[str, Any]:
        """Start a queued task assigned to you (status → claimed). Only the assignee may claim;
        claiming twice is harmless."""
        return await run(
            ctx, "skein_claim", lambda me: skein.claim(me, task_id), {"task_id": task_id}
        )

    @tool("skein_note", "Add note")
    async def skein_note(ctx: Context, task_id: str, note: str) -> dict[str, Any]:
        """Add a progress note to a task (up to 2000 characters). The assignee or the point may
        add notes."""
        return await run(
            ctx,
            "skein_note",
            lambda me: skein.note(me, task_id, note),
            {"task_id": task_id, "note": note},
        )

    @tool("skein_submit", "Submit result")
    async def skein_submit(
        ctx: Context, task_id: str, result: str, status: str = "done"
    ) -> dict[str, Any]:
        """Report on a task assigned to you. status is "done" (result = what you did or found),
        "failed" (result = what went wrong), or "needs_input" (result = your question for the
        point; the task returns to your inbox after the point follows up). Only the assignee
        may submit. A finished task can be re-submitted for 7 days."""
        return await run(
            ctx,
            "skein_submit",
            lambda me: skein.submit(me, task_id, result, status),
            {"task_id": task_id, "result": result},
        )

    @tool("skein_ask", "Ask the point")
    async def skein_ask(
        ctx: Context, question: str, context: str = "", task_id: str = ""
    ) -> dict[str, Any]:
        """Members only: ask the point a question, optionally about one of your tasks
        (task_id). The answer shows up in your skein_inbox. The point cannot ask; it answers."""
        return await run(
            ctx,
            "skein_ask",
            lambda me: skein.ask(me, question, context, task_id),
            {"question": question, "context": context, "task_id": task_id},
        )

    @tool("skein_get_ask", "Get ask")
    async def skein_get_ask(ctx: Context, ask_id: str) -> dict[str, Any]:
        """One ask and its answer, if any. The member who asked or the point may read it."""
        return await run(
            ctx, "skein_get_ask", lambda me: skein.get_ask(me, ask_id), {"ask_id": ask_id}
        )

    # -- point only ------------------------------------------------------------

    @tool("skein_assign", "Assign task")
    async def skein_assign(
        ctx: Context, member: str, title: str, instructions: str
    ) -> dict[str, Any]:
        """Point only: give a member a new task. title is short; instructions (up to 8000
        characters) say everything the member needs. The member can't be the point."""
        return await run(
            ctx,
            "skein_assign",
            lambda me: skein.assign(me, member, title, instructions),
            {"member": member, "title": title, "instructions": instructions},
        )

    @tool("skein_answer", "Answer ask")
    async def skein_answer(ctx: Context, ask_id: str, answer: str) -> dict[str, Any]:
        """Point only: answer a member's ask. Answering again replaces the earlier answer."""
        return await run(
            ctx,
            "skein_answer",
            lambda me: skein.answer(me, ask_id, answer),
            {"ask_id": ask_id, "answer": answer},
        )

    @tool("skein_follow_up", "Follow up on task")
    async def skein_follow_up(ctx: Context, task_id: str, text: str) -> dict[str, Any]:
        """Point only: add instructions to a task and put it back in the assignee's queue.
        Use it to answer a needs_input task, or to ask for more on a done/failed task within
        7 days."""
        return await run(
            ctx,
            "skein_follow_up",
            lambda me: skein.follow_up(me, task_id, text),
            {"task_id": task_id, "text": text},
        )

    @tool("skein_reassign", "Reassign task")
    async def skein_reassign(
        ctx: Context, task_id: str, member: str, note: str = ""
    ) -> dict[str, Any]:
        """Point only: move an open task to another member; it goes back to queued."""
        return await run(
            ctx,
            "skein_reassign",
            lambda me: skein.reassign(me, task_id, member, note),
            {"task_id": task_id, "member": member, "note": note},
        )

    @tool("skein_cancel", "Cancel task")
    async def skein_cancel(ctx: Context, task_id: str) -> dict[str, Any]:
        """Point only: cancel an open task. A finished task is left as it is."""
        return await run(
            ctx, "skein_cancel", lambda me: skein.cancel(me, task_id), {"task_id": task_id}
        )

    @tool("skein_tasks", "List tasks")
    async def skein_tasks(
        ctx: Context, status: str = "", member: str = "", limit: int = 20
    ) -> dict[str, Any]:
        """Point only: task summaries, newest first, as {"tasks": [...]}. status filters by a
        status or "open"; member filters by assignee; limit is 1-100."""
        return await run(
            ctx,
            "skein_tasks",
            lambda me: skein.list_tasks(me, status, member, limit),
            {"member": member},
            wrap="tasks",
        )

    @tool("skein_asks", "List asks")
    async def skein_asks(ctx: Context, status: str = "open", limit: int = 20) -> dict[str, Any]:
        """Point only: asks as {"asks": [...]}. status is "open" (default) or "answered",
        oldest first, or "all", newest first; limit is 1-100."""
        return await run(
            ctx, "skein_asks", lambda me: skein.list_asks(me, status, limit), {}, wrap="asks"
        )

    @tool("skein_members", "List members")
    async def skein_members(ctx: Context) -> dict[str, Any]:
        """Point only: every member as {"members": [...]}, with last check-in and open task
        count."""
        return await run(ctx, "skein_members", lambda me: skein.members(me), {}, wrap="members")

    return mcp
