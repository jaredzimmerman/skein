"""MCP surface: lifespan composition, instructions, tool list, every tool, point-only
refusals, per-request identity under concurrency, error mapping."""

from __future__ import annotations

import json

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from skein.app import create_app
from skein.config import Config
from skein.mcp_tools import POINT_TOOLS, READ_ONLY, TOOLS

ORIGIN = "http://127.0.0.1:8771"
SECRET = "the-violet-umbrella-plan"
INIT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "skein-tests", "version": "0"},
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def jsonrpc(method: str, params: dict | None = None, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def config(tmp_path, audit_path) -> Config:
    return Config(db=str(tmp_path / "unused.db"), audit_log=str(audit_path))


@pytest.fixture
def client(skein, team, config):
    with TestClient(create_app(config, skein), base_url=ORIGIN) as c:
        yield c


def rpc(client: TestClient, token: str, method: str, params: dict | None = None, headers=None) -> dict:
    r = client.post("/mcp", json=jsonrpc(method, params), headers={**bearer(token), **(headers or {})})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    return body["result"]


def call(client: TestClient, token: str, tool: str, **args) -> dict:
    """Call a tool; return its dict. Checks the text content mirrors the structured content."""
    result = rpc(client, token, "tools/call", {"name": tool, "arguments": args})
    assert result["isError"] is False, result
    data = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == data
    return data


# -- composition ------------------------------------------------------------------


def test_mcp_session_manager_runs_inside_the_parent_lifespan(skein, team, config):
    """Verified, not assumed: FastMCP's session manager only serves once the parent app's
    lifespan has started it."""
    app = create_app(config, skein)
    body = jsonrpc("tools/call", {"name": "skein_whoami", "arguments": {}})
    no_lifespan = TestClient(app, base_url=ORIGIN)
    with pytest.raises(RuntimeError, match="Task group is not initialized"):
        no_lifespan.post("/mcp", json=body, headers=bearer(team["alpha"]))
    with TestClient(app, base_url=ORIGIN) as client:
        r = client.post("/mcp", json=body, headers=bearer(team["alpha"]))
        assert r.status_code == 200
        assert r.json()["result"]["structuredContent"]["member"] == "alpha"


def test_initialize_returns_instructions_built_from_config(skein, team, tmp_path):
    config = Config(db=str(tmp_path / "x.db"), owner="Sam", answer_time="by the next morning")
    with TestClient(create_app(config, skein), base_url=ORIGIN) as client:
        result = rpc(client, team["alpha"], "initialize", INIT)
    assert result["serverInfo"]["name"] == "skein"
    text = result["instructions"]
    assert "Sam's requests" in text and "on Sam's behalf" in text
    assert "by the next morning" in text
    assert "skein_whoami first" in text
    for tool in ("skein_inbox", "skein_claim", "skein_note", "skein_submit", "needs_input", "skein_ask",
                 "skein_answer", "skein_assign", "skein_follow_up", "skein_reassign"):
        assert tool in text
    assert "confirmation before sending, buying or posting" in text


def test_default_instructions_name_no_one(client, team):
    text = rpc(client, team["alpha"], "initialize", INIT)["instructions"]
    assert "the user's requests" in text
    assert "within about an hour" in text
    assert "Sam" not in text and "example.com" not in text


def test_tool_list_names_annotations_and_descriptions(client, team):
    tools = {t["name"]: t for t in rpc(client, team["alpha"], "tools/list")["tools"]}
    assert list(tools) == list(TOOLS)
    for name, tool in tools.items():
        hints = tool["annotations"]
        assert hints["readOnlyHint"] is (name in READ_ONLY), name
        assert tool["description"].strip(), name
        assert "ctx" not in tool["inputSchema"].get("properties", {}), name
        if name in POINT_TOOLS:
            assert tool["description"].startswith("Point only"), name
        else:
            assert "Point only" not in tool["description"], name
    assert READ_ONLY == {"skein_whoami", "skein_get_task", "skein_get_ask", "skein_tasks",
                         "skein_asks", "skein_members"}
    assert tools["skein_cancel"]["annotations"]["destructiveHint"] is True
    assert tools["skein_inbox"]["inputSchema"]["properties"]["limit"]["type"] == "integer"
    assert tools["skein_tasks"]["inputSchema"]["properties"]["limit"]["type"] == "integer"


def test_point_and_member_see_the_same_tools(client, team):
    names = lambda who: [t["name"] for t in rpc(client, team[who], "tools/list")["tools"]]  # noqa: E731
    assert names("lead") == names("alpha") == names("beta") == list(TOOLS)


# -- every tool ---------------------------------------------------------------------


def test_every_tool_is_reachable(client, team):
    lead, alpha = team["lead"], team["alpha"]
    used: set[str] = set()

    def c(token, tool, **args):
        used.add(tool)
        out = call(client, token, tool, **args)
        assert "error" not in out, (tool, out)
        return out

    assert c(lead, "skein_whoami")["role"] == "point"
    task = c(lead, "skein_assign", member="alpha", title="Find a venue", instructions="Two options.")
    tid = task["id"]
    assert [t["id"] for t in c(lead, "skein_tasks", status="open")["tasks"]] == [tid]
    assert {m["name"] for m in c(lead, "skein_members")["members"]} == {"lead", "alpha", "beta"}

    assert c(alpha, "skein_whoami") == {
        "member": "alpha", "display": "Alpha", "role": "member", "point": "lead", "point_display": "Lead",
    }
    assert [t["id"] for t in c(alpha, "skein_inbox", limit=5)["tasks"]] == [tid]
    assert c(alpha, "skein_get_task", task_id=tid)["instructions"] == "Two options."
    assert c(alpha, "skein_claim", task_id=tid)["status"] == "claimed"
    assert c(alpha, "skein_note", task_id=tid, note="looking")["notes"][-1]["from"] == "alpha"
    ask = c(alpha, "skein_ask", question="Budget?", context="venue", task_id=tid)
    assert c(lead, "skein_asks")["asks"][0]["id"] == ask["id"]
    assert c(lead, "skein_answer", ask_id=ask["id"], answer="Under 500")["status"] == "answered"
    assert c(alpha, "skein_get_ask", ask_id=ask["id"])["answer"] == "Under 500"
    assert c(alpha, "skein_submit", task_id=tid, result="Indoor?", status="needs_input")["status"] == "needs_input"
    assert c(lead, "skein_follow_up", task_id=tid, text="Indoor.")["status"] == "queued"
    assert c(lead, "skein_reassign", task_id=tid, member="beta", note="swap")["assignee"] == "beta"
    assert c(lead, "skein_cancel", task_id=tid)["status"] == "cancelled"
    assert used == set(TOOLS)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("skein_assign", {"member": "beta", "title": "T", "instructions": "I"}),
        ("skein_answer", {"ask_id": "a_0000000000", "answer": "x"}),
        ("skein_follow_up", {"task_id": "t_0000000000", "text": "x"}),
        ("skein_reassign", {"task_id": "t_0000000000", "member": "beta"}),
        ("skein_cancel", {"task_id": "t_0000000000"}),
        ("skein_tasks", {}),
        ("skein_asks", {}),
        ("skein_members", {}),
    ],
)
def test_point_only_tools_refuse_members(client, team, tool, args):
    assert tool in POINT_TOOLS
    out = call(client, team["alpha"], tool, **args)
    assert set(out) == {"error", "code"}
    assert out["code"] == "forbidden"


def test_tool_errors_are_dicts_not_protocol_errors(client, team):
    assert call(client, team["alpha"], "skein_get_task", task_id="t_0000000000")["code"] == "not_found"
    assert call(client, team["lead"], "skein_ask", question="Hm?")["code"] == "forbidden"
    out = call(client, team["lead"], "skein_assign", member="alpha", title="", instructions="I")
    assert out["code"] == "invalid"
    out = call(client, team["lead"], "skein_assign", member="lead", title="T", instructions="I")
    assert out["code"] == "conflict"


def test_unexpected_error_is_internal_without_details(client, skein, team, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(skein, "inbox", boom)
    assert call(client, team["alpha"], "skein_inbox") == {"error": "internal error", "code": "internal"}
    err = capsys.readouterr().err
    assert "RuntimeError" in err and SECRET not in err


@pytest.mark.parametrize("accept", ["*/*", "text/plain", "application/json", None])
def test_post_accept_header_is_normalized(client, team, accept):
    headers = bearer(team["alpha"])
    if accept is not None:
        headers["Accept"] = accept
    r = client.post("/mcp", json=jsonrpc("tools/call", {"name": "skein_whoami", "arguments": {}}), headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["result"]["structuredContent"]["member"] == "alpha"


def test_tool_calls_are_audited_with_lengths_only(client, team, audit_path):
    call(client, team["alpha"], "skein_ask", question=SECRET, context=SECRET * 2)
    call(client, team["alpha"], "skein_members")
    raw = audit_path.read_text()
    assert SECRET not in raw and team["alpha"] not in raw
    ask, members = [json.loads(l) for l in raw.splitlines()][-2:]
    assert ask["tool"] == "skein_ask" and ask["member"] == "alpha" and ask["status"] == "ok"
    assert ask["lens"] == {"question": len(SECRET), "context": 2 * len(SECRET)}
    assert ask["ids"]["id"].startswith("a_")
    assert (members["tool"], members["status"]) == ("skein_members", "forbidden")


def test_free_text_in_an_id_position_is_audited_as_a_length(client, team, audit_path):
    free = "Plan: the violet umbrella for the weekend"
    assert call(client, team["alpha"], "skein_get_task", task_id=free)["code"] == "not_found"
    # name-shaped text is still not an ask id
    assert call(client, team["alpha"], "skein_get_ask", ask_id=SECRET)["code"] == "not_found"
    assert call(client, team["lead"], "skein_assign", member=free, title="t", instructions="i")["code"] == "not_found"
    ask_id = call(client, team["alpha"], "skein_ask", question="q")["id"]
    call(client, team["lead"], "skein_answer", ask_id=ask_id, answer="a")

    raw = audit_path.read_text()
    assert free not in raw and SECRET not in raw
    get_task, get_ask, assign, ask, answer = [json.loads(l) for l in raw.splitlines()][-5:]
    assert "ids" not in get_task and get_task["lens"] == {"task_id": len(free)}
    assert "ids" not in get_ask and get_ask["lens"] == {"ask_id": len(SECRET)}
    assert "ids" not in assign and assign["lens"] == {"member": len(free), "title": 1, "instructions": 1}
    assert ask["ids"] == {"id": ask_id}
    assert answer["ids"] == {"ask_id": ask_id} and answer["lens"] == {"answer": 1}


# -- identity per request, concurrently ---------------------------------------------


@pytest.mark.anyio
async def test_identity_is_per_request_under_concurrency(skein, team, config):
    """Two members with different tokens call skein_whoami and skein_inbox at the same time,
    through the real MCP transport, and each sees only its own identity and tasks."""
    mine = {
        "alpha": {skein.assign("lead", "alpha", f"alpha task {i}", "do it")["id"] for i in range(3)},
        "beta": {skein.assign("lead", "beta", f"beta task {i}", "do it")["id"] for i in range(2)},
    }
    app = create_app(config, skein)
    rounds = 8

    def http(name: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=bearer(team[name])
        )

    async def with_mcp_client(name: str, results: dict) -> None:
        """The SDK's own streamable HTTP client, one ClientSession per member."""
        async with http(name) as client:
            async with streamable_http_client(f"{ORIGIN}/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    seen = []
                    for _ in range(rounds):
                        who = await session.call_tool("skein_whoami", {})
                        inbox = await session.call_tool("skein_inbox", {})
                        seen.append((who.structuredContent, inbox.structuredContent))
                        await anyio.sleep(0)
                    results[name] = seen

    async def with_raw_posts(name: str, i: int, results: dict) -> None:
        """Plain JSON-RPC POSTs, many in flight at once."""
        async with http(name) as client:
            who = await client.post("/mcp", json=jsonrpc("tools/call", {"name": "skein_whoami", "arguments": {}}, i))
            inbox = await client.post("/mcp", json=jsonrpc("tools/call", {"name": "skein_inbox", "arguments": {}}, i))
            results[(name, i)] = (
                who.json()["result"]["structuredContent"],
                inbox.json()["result"]["structuredContent"],
            )

    def check(name: str, who: dict, inbox: dict) -> None:
        assert who["member"] == name and who["role"] == "member"
        assert inbox["role"] == "member"
        assert {t["id"] for t in inbox["tasks"]} == mine[name]
        assert {t["assignee"] for t in inbox["tasks"]} == {name}

    async with app.router.lifespan_context(app):
        sdk_results: dict = {}
        raw_results: dict = {}
        async with anyio.create_task_group() as tg:
            tg.start_soon(with_mcp_client, "alpha", sdk_results)
            tg.start_soon(with_mcp_client, "beta", sdk_results)
            for i in range(rounds):
                tg.start_soon(with_raw_posts, "alpha", i, raw_results)
                tg.start_soon(with_raw_posts, "beta", i, raw_results)

    assert set(sdk_results) == {"alpha", "beta"}
    for name, seen in sdk_results.items():
        assert len(seen) == rounds
        for who, inbox in seen:
            check(name, who, inbox)
    assert len(raw_results) == 2 * rounds
    for (name, _), (who, inbox) in raw_results.items():
        check(name, who, inbox)
