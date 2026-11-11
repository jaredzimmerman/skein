"""HTTP surface: auth, healthz, /mcp quirks, Host checks, REST, audit log, base_path."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from skein.app import create_app
from skein.config import Config
from skein.mcp_tools import TOOLS

ORIGIN = "http://127.0.0.1:8771"  # a Host the MCP transport allows by default
SECRET = "the-violet-umbrella-plan"  # task text that must never reach the audit log


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def make_client(skein, tmp_path, audit_path):
    """Build an app on the shared service and open a TestClient (runs the lifespan)."""
    opened: list[TestClient] = []

    def make(**overrides) -> TestClient:
        settings = {"db": str(tmp_path / "unused.db"), "audit_log": str(audit_path), **overrides}
        client = TestClient(create_app(Config(**settings), skein), base_url=ORIGIN)
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client, team):
    return make_client()


def audit_lines(audit_path) -> list[dict]:
    return [json.loads(line) for line in audit_path.read_text().splitlines()]


def mcp_call(client: TestClient, token: str, tool: str, args: dict | None = None, headers=None, **kw):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}},
    }
    return client.post("/mcp", json=body, headers={**bearer(token), **(headers or {})}, **kw)


# -- healthz and auth -----------------------------------------------------------


def test_healthz_needs_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


@pytest.mark.parametrize("path", ["/api/whoami", "/mcp", "/api/nope"])
def test_no_token_is_401_with_challenge(client, path):
    r = client.get(path)
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
    assert r.headers["www-authenticate"] == 'Bearer realm="skein"'


def test_wrong_token_is_401(client):
    r = client.get("/api/whoami", headers=bearer("skein_not-a-real-token"))
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_mcp_post_without_token_is_401(client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    r = client.post("/mcp", json=body)
    assert r.status_code == 401


def test_disabled_member_is_401(client, skein, team):
    assert client.get("/api/whoami", headers=bearer(team["alpha"])).status_code == 200
    skein.set_disabled("alpha", True)
    r = client.get("/api/whoami", headers=bearer(team["alpha"]))
    assert r.status_code == 401
    assert mcp_call(client, team["alpha"], "skein_whoami").status_code == 401


def test_x_api_key_header_is_accepted(client, team):
    r = client.get("/api/whoami", headers={"X-API-Key": team["beta"]})
    assert r.status_code == 200
    assert r.json()["member"] == "beta"


def test_bearer_scheme_is_case_insensitive(client, team):
    r = client.get("/api/whoami", headers={"Authorization": f"bearer {team['beta']}"})
    assert r.json()["member"] == "beta"


def test_query_token_refused_when_url_tokens_off(client, team):
    r = client.get("/api/whoami", params={"token": team["alpha"]})
    assert r.status_code == 401


def test_query_token_accepted_when_url_tokens_on(make_client, team):
    client = make_client(url_tokens=True)
    r = client.get("/api/whoami", params={"token": team["alpha"]})
    assert r.status_code == 200
    assert r.json()["member"] == "alpha"
    r = mcp_call(client, "", "skein_whoami", params={"token": team["alpha"]})
    assert r.status_code == 200
    assert r.json()["result"]["structuredContent"]["member"] == "alpha"


def test_rotated_token_stops_working(client, skein, team):
    skein.rotate_token("alpha")
    assert client.get("/api/whoami", headers=bearer(team["alpha"])).status_code == 401


# -- /mcp quirks and host checks -------------------------------------------------


def test_mcp_get_event_stream_is_405_allow_post(client, team):
    r = client.get("/mcp", headers={**bearer(team["alpha"]), "Accept": "text/event-stream"})
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"


def test_mcp_plain_get_returns_help_json(client, team):
    r = client.get("/mcp", headers=bearer(team["alpha"]))
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "skein"
    assert data["member"] == "alpha"
    assert "Bearer" in data["auth"] and "token=" not in data["auth"]
    assert data["tools"] == list(TOOLS)
    routes = [r["route"] for r in data["rest"]]
    assert "GET /api/whoami" in routes
    assert "POST /api/tasks/{id}/result {result, status}" in routes
    assert len(routes) == 16


def test_mcp_trailing_slash_also_works(client, team):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    r = client.post("/mcp/", json=body, headers=bearer(team["alpha"]))
    assert r.status_code == 200
    assert len(r.json()["result"]["tools"]) == 16


def test_mcp_rejects_unlisted_host(client, team):
    r = mcp_call(client, team["alpha"], "skein_whoami", headers={"Host": "evil.example.net"})
    assert r.status_code == 421


def test_mcp_accepts_localhost_and_public_hosts(make_client, team):
    client = make_client(public_hosts=("skein.example.com",))
    for host in ("skein.example.com", "localhost:8771", "127.0.0.1:8771"):
        r = mcp_call(client, team["alpha"], "skein_whoami", headers={"Host": host})
        assert r.status_code == 200, host
        assert r.json()["result"]["structuredContent"]["member"] == "alpha"
    r = mcp_call(client, team["alpha"], "skein_whoami", headers={"Host": "localhost:9999"})
    assert r.status_code == 421


def test_mcp_refuses_foreign_origin(client, team):
    r = mcp_call(client, team["alpha"], "skein_whoami", headers={"Origin": "https://evil.example.net"})
    assert r.status_code == 403


# -- REST -------------------------------------------------------------------------


def test_rest_full_round_trip(client, team):
    lead, alpha = bearer(team["lead"]), bearer(team["alpha"])

    r = client.post(
        "/api/tasks",
        json={"member": "alpha", "title": "Find a venue", "instructions": "Two options, Friday."},
        headers=lead,
    )
    assert r.status_code == 200, r.text
    task = r.json()
    tid = task["id"]
    assert task["status"] == "queued" and task["assignee"] == "alpha"

    inbox = client.get("/api/inbox", headers=alpha).json()
    assert inbox["role"] == "member"
    assert [t["id"] for t in inbox["tasks"]] == [tid]

    assert client.post(f"/api/tasks/{tid}/claim", headers=alpha).json()["status"] == "claimed"
    r = client.post(f"/api/tasks/{tid}/note", json={"note": "checking two places"}, headers=alpha)
    assert r.json()["notes"][-1]["text"] == "checking two places"

    r = client.post(
        f"/api/tasks/{tid}/result",
        json={"result": "Indoor or outdoor?", "status": "needs_input"},
        headers=alpha,
    )
    assert r.json()["status"] == "needs_input"

    point_inbox = client.get("/api/inbox", headers=lead).json()
    assert [t["id"] for t in point_inbox["needs_input"]] == [tid]

    r = client.post(f"/api/tasks/{tid}/follow-up", json={"text": "Indoor."}, headers=lead)
    assert r.json()["status"] == "queued"
    assert [t["id"] for t in client.get("/api/inbox", headers=alpha).json()["tasks"]] == [tid]

    r = client.post(f"/api/tasks/{tid}/result", json={"result": "Booked the hall."}, headers=alpha)
    assert r.status_code == 200
    assert r.json()["status"] == "done"

    finished = client.get("/api/inbox", headers=lead).json()["finished"]
    assert [(t["id"], t["status"], t["result"]) for t in finished] == [(tid, "done", "Booked the hall.")]
    assert client.get("/api/inbox", headers=lead).json()["finished"] == []
    assert client.get(f"/api/tasks/{tid}", headers=alpha).json()["result"] == "Booked the hall."


def test_rest_asks_round_trip(client, team):
    lead, alpha = bearer(team["lead"]), bearer(team["alpha"])
    r = client.post("/api/asks", json={"question": "Which city?", "context": "for the trip"}, headers=alpha)
    assert r.status_code == 200
    aid = r.json()["id"]
    assert [a["id"] for a in client.get("/api/asks", headers=lead).json()["asks"]] == [aid]
    r = client.post(f"/api/asks/{aid}/answer", json={"answer": "Lisbon"}, headers=lead)
    assert r.json()["status"] == "answered"
    answers = client.get("/api/inbox", headers=alpha).json()["answers"]
    assert [(a["id"], a["answer"]) for a in answers] == [(aid, "Lisbon")]
    assert client.get(f"/api/asks/{aid}", headers=alpha).json()["answer"] == "Lisbon"
    assert client.get("/api/asks?status=all&limit=5", headers=lead).json()["asks"][0]["id"] == aid


def test_rest_point_lists_are_wrapped(client, team):
    lead = bearer(team["lead"])
    client.post("/api/tasks", json={"member": "beta", "title": "T", "instructions": "I"}, headers=lead)
    tasks = client.get("/api/tasks?status=open&member=beta&limit=5", headers=lead).json()
    assert list(tasks) == ["tasks"] and len(tasks["tasks"]) == 1
    assert client.get("/api/asks", headers=lead).json() == {"asks": []}
    members = client.get("/api/members", headers=lead).json()
    assert list(members) == ["members"]
    assert {m["name"] for m in members["members"]} == {"lead", "alpha", "beta"}
    whoami = client.get("/api/whoami", headers=lead).json()
    assert whoami["role"] == "point" and len(whoami["members"]) == 3


def test_rest_reassign_and_cancel(client, team):
    lead = bearer(team["lead"])
    tid = client.post(
        "/api/tasks", json={"member": "alpha", "title": "T", "instructions": "I"}, headers=lead
    ).json()["id"]
    r = client.post(f"/api/tasks/{tid}/reassign", json={"member": "beta", "note": "alpha is busy"}, headers=lead)
    assert r.json()["assignee"] == "beta"
    assert client.get(f"/api/tasks/{tid}", headers=bearer(team["alpha"])).status_code == 404
    assert client.post(f"/api/tasks/{tid}/cancel", headers=lead).json()["status"] == "cancelled"
    assert client.post(f"/api/tasks/{tid}/cancel", headers=lead).json()["note"] == "already final"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/tasks", {"member": "beta", "title": "T", "instructions": "I"}),
        ("get", "/api/tasks", None),
        ("post", "/api/tasks/t_0000000000/follow-up", {"text": "x"}),
        ("post", "/api/tasks/t_0000000000/reassign", {"member": "beta"}),
        ("post", "/api/tasks/t_0000000000/cancel", None),
        ("get", "/api/asks", None),
        ("post", "/api/asks/a_0000000000/answer", {"answer": "x"}),
        ("get", "/api/members", None),
    ],
)
def test_rest_point_routes_refuse_members_with_403(client, team, method, path, body):
    kwargs = {"headers": bearer(team["alpha"])}
    if body is not None:
        kwargs["json"] = body
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"
    assert set(r.json()) == {"error", "code"}


def test_rest_error_code_mapping(client, skein, team):
    lead, alpha = bearer(team["lead"]), bearer(team["alpha"])
    # 404: unknown, or another member's task
    assert client.get("/api/tasks/t_0000000000", headers=alpha).json()["code"] == "not_found"
    beta_task = client.post(
        "/api/tasks", json={"member": "beta", "title": "T", "instructions": "I"}, headers=lead
    ).json()["id"]
    r = client.get(f"/api/tasks/{beta_task}", headers=alpha)
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert client.get("/api/asks/a_0000000000", headers=alpha).status_code == 404
    # 400: empty title, bad limit, bad body, bad status
    r = client.post("/api/tasks", json={"member": "beta", "title": " ", "instructions": "I"}, headers=lead)
    assert (r.status_code, r.json()["code"]) == (400, "invalid")
    r = client.get("/api/inbox?limit=lots", headers=alpha)
    assert (r.status_code, r.json()["code"]) == (400, "invalid")
    r = client.post("/api/asks", content=b"{not json", headers={**alpha, "Content-Type": "application/json"})
    assert (r.status_code, r.json()["code"]) == (400, "invalid")
    r = client.post("/api/asks", json=["a", "list"], headers=alpha)
    assert r.status_code == 400
    r = client.post("/api/asks", content=b'{"question": "\\ud800"}', headers=alpha)
    assert (r.status_code, r.json()["code"]) == (400, "invalid")
    r = client.post(f"/api/tasks/{beta_task}/result", json={"result": "x", "status": "maybe"}, headers=bearer(team["beta"]))
    assert (r.status_code, r.json()["code"]) == (400, "invalid")
    # 403: the point can't ask
    r = client.post("/api/asks", json={"question": "Hm?"}, headers=lead)
    assert (r.status_code, r.json()["code"]) == (403, "forbidden")
    # 409: assigning to a disabled member, submitting on a needs_input task
    skein.set_disabled("alpha", True)
    r = client.post("/api/tasks", json={"member": "alpha", "title": "T", "instructions": "I"}, headers=lead)
    assert (r.status_code, r.json()["code"]) == (409, "conflict")
    beta = bearer(team["beta"])
    client.post(f"/api/tasks/{beta_task}/result", json={"result": "Q?", "status": "needs_input"}, headers=beta)
    r = client.post(f"/api/tasks/{beta_task}/result", json={"result": "done"}, headers=beta)
    assert (r.status_code, r.json()["code"]) == (409, "conflict")


def test_rest_unknown_route_and_wrong_method_are_json(client, team):
    alpha = bearer(team["alpha"])
    r = client.get("/api/nope", headers=alpha)
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    r = client.delete("/api/whoami", headers=alpha)
    assert r.status_code == 405
    assert r.json()["code"] == "invalid"
    assert "GET" in r.headers["allow"]


def test_rest_unexpected_error_is_500_without_details(client, skein, team, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(skein, "whoami", boom)
    r = client.get("/api/whoami", headers=bearer(team["alpha"]))
    assert r.status_code == 500
    assert r.json() == {"error": "internal error", "code": "internal"}
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert SECRET not in err and team["alpha"] not in err


# -- audit log ----------------------------------------------------------------------


def test_audit_log_has_lengths_never_text_or_tokens(client, team, audit_path):
    lead, alpha = bearer(team["lead"]), bearer(team["alpha"])
    instructions = f"Plan: {SECRET} for the weekend."
    tid = client.post(
        "/api/tasks", json={"member": "alpha", "title": f"Title {SECRET}", "instructions": instructions},
        headers=lead,
    ).json()["id"]
    client.post(f"/api/tasks/{tid}/note", json={"note": SECRET}, headers=alpha)
    client.post(f"/api/tasks/{tid}/result", json={"result": SECRET * 3}, headers=alpha)
    client.post("/api/asks", json={"question": SECRET, "context": SECRET}, headers=alpha)
    mcp_call(client, team["alpha"], "skein_note", {"task_id": tid, "note": SECRET})
    client.get("/api/whoami", headers=bearer("skein_wrong-" + SECRET), params={"token": "q-" + SECRET})

    raw = audit_path.read_text()
    assert SECRET not in raw
    for token in team.values():
        assert token not in raw
    lines = audit_lines(audit_path)

    assign = next(l for l in lines if l.get("route") == "POST /tasks")
    assert assign["kind"] == "rest" and assign["member"] == "lead" and assign["status"] == 200
    assert assign["ids"] == {"member": "alpha", "id": tid}
    assert assign["lens"] == {"title": len(f"Title {SECRET}"), "instructions": len(instructions)}

    result = next(l for l in lines if l.get("route") == "POST /tasks/{id}/result")
    assert result["ids"] == {"task_id": tid} and result["lens"] == {"result": len(SECRET) * 3}

    ask = next(l for l in lines if l.get("route") == "POST /asks")
    assert ask["lens"] == {"question": len(SECRET), "context": len(SECRET)}

    tool = next(l for l in lines if l.get("tool") == "skein_note")
    assert tool["kind"] == "mcp" and tool["member"] == "alpha" and tool["status"] == "ok"
    assert tool["ids"] == {"task_id": tid} and tool["lens"] == {"note": len(SECRET)}

    failed = lines[-1]
    assert failed["kind"] == "auth_failed" and failed["reason"] == "invalid"
    assert failed["method"] == "GET" and failed["path"] == "/api/whoami"
    assert failed["ip"] and "ua" in failed
    assert all("t" in l for l in lines)


def test_audit_log_records_refusals(client, team, audit_path):
    client.get("/api/members", headers=bearer(team["alpha"]))
    line = audit_lines(audit_path)[-1]
    assert (line["route"], line["status"], line["code"], line["member"]) == (
        "GET /members", 403, "forbidden", "alpha",
    )


def test_no_audit_log_when_disabled(make_client, team, audit_path):
    client = make_client(audit_log="")
    client.get("/api/whoami", headers=bearer(team["alpha"]))
    client.get("/api/whoami")
    assert not audit_path.exists()


# -- base_path -------------------------------------------------------------------


def test_base_path_mounts_everything(make_client, team):
    client = make_client(base_path="/skein")
    alpha = bearer(team["alpha"])
    assert client.get("/skein/healthz").text == "ok"
    assert client.get("/skein/api/whoami", headers=alpha).json()["member"] == "alpha"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "skein_whoami", "arguments": {}}}
    r = client.post("/skein/mcp", json=body, headers=alpha)
    assert r.json()["result"]["structuredContent"]["member"] == "alpha"
    help_ = client.get("/skein/mcp", headers=alpha).json()
    assert help_["mcp"]["endpoint"] == "/skein/mcp"
    assert "GET /skein/api/whoami" in [r["route"] for r in help_["rest"]]
    # nothing is served outside the base path
    assert client.get("/healthz").status_code == 401
    assert client.get("/api/whoami", headers=alpha).status_code == 404
    assert client.get("/skein/mcp", headers={**alpha, "Accept": "text/event-stream"}).status_code == 405


def test_create_app_builds_its_own_service_from_config(tmp_path):
    from skein.service import Skein

    db = tmp_path / "own.db"
    token = Skein(db).add_member("solo")["token"]
    with TestClient(create_app(Config(db=str(db))), base_url=ORIGIN) as client:
        assert client.get("/api/whoami", headers=bearer(token)).json()["member"] == "solo"
