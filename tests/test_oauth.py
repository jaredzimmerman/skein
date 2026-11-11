"""OAuth sign-in (PLAN.md C8): metadata, DCR and CLI clients, consent, codes, tokens,
revocation, the v1→v2 migration and what never reaches a log. Everything goes through the
real ASGI app."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import re
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest
from starlette.testclient import TestClient

from skein import oauth as oauth_mod
from skein.app import create_app
from skein.cli import main
from skein.config import Config, load_config
from skein.service import Skein, is_loopback
from skein.store import Store

ORIGIN = "http://127.0.0.1:8771"
BASE = "/skein"
PUBLIC = "https://skein.example.com/skein"
RESOURCE = PUBLIC + "/mcp"
PRM_URL = "https://skein.example.com/.well-known/oauth-protected-resource/skein/mcp"
REDIRECT = "https://app.example.com/callback"
PLAIN_CHALLENGE = 'Bearer realm="skein"'
OAUTH_CHALLENGE = f'Bearer realm="skein", resource_metadata="{PRM_URL}"'
C8_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
REFRESH_RE = re.compile(r'<meta http-equiv="refresh" content="0;url=([^"]*)">')
CONTINUE_RE = re.compile(r'<a href="([^"]*)">Continue</a>')


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def basic(client_id: str, secret: str) -> dict[str, str]:
    """RFC 6749 §2.3.1 client authentication."""
    return {"Authorization": "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def oauth_config(tmp_path, audit_path, **overrides) -> Config:
    settings = {
        "db": str(tmp_path / "unused.db"),
        "audit_log": str(audit_path),
        "base_path": BASE,
        "public_url": PUBLIC,
        "oauth_enabled": True,
        **overrides,
    }
    return Config(**settings)


@pytest.fixture
def make_client(skein, tmp_path, audit_path):
    opened: list[TestClient] = []

    def make(**overrides) -> TestClient:
        client = TestClient(
            create_app(oauth_config(tmp_path, audit_path, **overrides), skein), base_url=ORIGIN
        )
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client, team):
    return make_client()


# -- flow helpers ------------------------------------------------------------------


def register(client: TestClient, **meta) -> dict:
    body = {"redirect_uris": [REDIRECT], "client_name": "Test App", **meta}
    r = client.post(f"{BASE}/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def authorize(client: TestClient, client_id: str, challenge: str, redirect_uri: str | None = REDIRECT,
              state: str = "st-123", **extra) -> httpx.Response:
    params = {"response_type": "code", "client_id": client_id, "code_challenge": challenge,
              "code_challenge_method": "S256", "state": state, **extra}
    if redirect_uri is not None:
        params["redirect_uri"] = redirect_uri
    return client.get(f"{BASE}/authorize", params=params, follow_redirects=False)


def consent_req(response: httpx.Response) -> str:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{PUBLIC}/oauth/consent?")
    return query(location)["req"]


def consent(client: TestClient, req: str, token: str, decision: str = "approve") -> httpx.Response:
    return client.post(
        f"{BASE}/oauth/consent", data={"req": req, "token": token, "decision": decision},
        follow_redirects=False,
    )


def handback(response: httpx.Response) -> str:
    """Where a decided consent sends the browser: a 200 no-store page with a meta refresh and
    a Continue link to the same address (not a 303, so C8's ``form-action 'self'`` holds)."""
    assert response.status_code == 200, response.text
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == C8_CSP
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    refresh_url = REFRESH_RE.findall(response.text)
    continue_url = CONTINUE_RE.findall(response.text)
    assert len(refresh_url) == 1 and continue_url == refresh_url, response.text
    return html.unescape(refresh_url[0])


def code_from(response: httpx.Response, redirect_uri: str = REDIRECT, state: str = "st-123") -> str:
    location = handback(response)
    assert location.startswith(redirect_uri + "?")
    q = query(location)
    assert q["state"] == state
    return q["code"]


def exchange(client: TestClient, reg: dict, code: str, verifier: str, redirect_uri: str = REDIRECT,
             **extra) -> httpx.Response:
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "redirect_uri": redirect_uri, "client_id": reg["client_id"], **extra}
    if reg.get("client_secret") and "client_secret" not in extra:
        data["client_secret"] = reg["client_secret"]
    return client.post(f"{BASE}/token", data=data)


def refresh(client: TestClient, reg: dict, refresh_token: str) -> httpx.Response:
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": reg["client_id"]}
    if reg.get("client_secret"):
        data["client_secret"] = reg["client_secret"]
    return client.post(f"{BASE}/token", data=data)


def sign_in(client: TestClient, member_token: str, reg: dict | None = None) -> dict:
    """The whole browser dance for one member. Returns the token response plus the client."""
    reg = reg or register(client)
    verifier, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge))
    code = code_from(consent(client, req, member_token))
    r = exchange(client, reg, code, verifier)
    assert r.status_code == 200, r.text
    return {**r.json(), "client": reg}


def whoami_rest(client: TestClient, token: str) -> httpx.Response:
    return client.get(f"{BASE}/api/whoami", headers=bearer(token))


def whoami_mcp(client: TestClient, token: str) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "skein_whoami", "arguments": {}}}
    return client.post(f"{BASE}/mcp", json=body, headers=bearer(token))


def audit_lines(audit_path) -> list[dict]:
    if not audit_path.exists():
        return []
    return [json.loads(line) for line in audit_path.read_text().splitlines()]


def oauth_events(audit_path) -> list[dict]:
    return [line for line in audit_lines(audit_path) if line.get("kind") == "oauth"]


# -- config -------------------------------------------------------------------------


def _toml(tmp_path, body: str) -> str:
    path = tmp_path / "cfg.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_config_oauth_keys_and_defaults(tmp_path):
    cfg = Config()
    assert (cfg.public_url, cfg.oauth_enabled) == ("", False)
    assert (cfg.oauth_access_token_ttl, cfg.oauth_refresh_token_ttl) == (3600, 2592000)
    path = _toml(tmp_path, f"""
[server]
base_path = "/skein"
public_url = "{PUBLIC}"
[oauth]
enabled = true
access_token_ttl = 600
refresh_token_ttl = 86400
""")
    cfg = load_config(path)
    assert cfg.public_url == PUBLIC and cfg.oauth_enabled is True
    assert (cfg.oauth_access_token_ttl, cfg.oauth_refresh_token_ttl) == (600, 86400)


@pytest.mark.parametrize("body, key", [
    ('[oauth]\nenabled = "true"\n', "enabled"),
    ("[oauth]\nenabled = 1\n", "enabled"),
    ('[oauth]\naccess_token_ttl = "3600"\n', "access_token_ttl"),
    ("[oauth]\naccess_token_ttl = true\n", "access_token_ttl"),
    ("[oauth]\naccess_token_ttl = 0\n", "access_token_ttl"),
    ("[oauth]\nrefresh_token_ttl = 1.5\n", "refresh_token_ttl"),
    ("[oauth]\nrefresh_token_ttl = -5\n", "refresh_token_ttl"),
    ("[server]\npublic_url = 5\n", "public_url"),
    ("[oauth]\nsomething = 1\n", "something"),
])
def test_config_oauth_strict_types(tmp_path, body, key):
    with pytest.raises(ValueError, match=key):
        load_config(_toml(tmp_path, body))


def test_config_oauth_enabled_needs_public_url(tmp_path):
    with pytest.raises(ValueError, match="public_url"):
        load_config(_toml(tmp_path, "[oauth]\nenabled = true\n"))
    with pytest.raises(ValueError, match="public_url"):
        Config(oauth_enabled=True)


@pytest.mark.parametrize("url, base", [
    ("skein.example.com/skein", "/skein"),           # no scheme
    ("ftp://skein.example.com/skein", "/skein"),
    ("http://skein.example.com/skein", "/skein"),    # http only for localhost
    ("https://skein.example.com/skein/", "/skein"),  # trailing slash
    ("https://skein.example.com/skein?x=1", "/skein"),
    ("https://skein.example.com/skein#x", "/skein"),
    ("https://skein.example.com/other", "/skein"),   # path must equal base_path
    ("https://skein.example.com", "/skein"),
    ("https://skein.example.com/skein", ""),
    ("https://user:pw@skein.example.com/skein", "/skein"),
])
def test_config_public_url_validation(url, base):
    with pytest.raises(ValueError, match="public_url"):
        Config(public_url=url, base_path=base)


@pytest.mark.parametrize("url, base", [
    (PUBLIC, BASE),
    ("https://skein.example.com", ""),
    ("http://localhost:8771", ""),
    ("http://127.0.0.1:8771/skein", "/skein"),
])
def test_config_public_url_accepted(url, base):
    assert Config(public_url=url, base_path=base, oauth_enabled=True).public_url == url


# -- metadata -----------------------------------------------------------------------


def _check_as_metadata(meta: dict, public: str) -> None:
    assert meta["issuer"].rstrip("/") == public
    assert meta["authorization_endpoint"] == f"{public}/authorize"
    assert meta["token_endpoint"] == f"{public}/token"
    assert meta["registration_endpoint"] == f"{public}/register"
    assert meta["revocation_endpoint"] == f"{public}/revoke"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["scopes_supported"] == ["skein"]
    assert meta["response_types_supported"] == ["code"]
    assert set(meta["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert "none" in meta["token_endpoint_auth_methods_supported"]


def test_metadata_root_path_inserted_and_base_appended(client):
    for path in (
        "/.well-known/oauth-authorization-server/skein",
        "/.well-known/openid-configuration/skein",
        "/skein/.well-known/oauth-authorization-server",
        "/skein/.well-known/openid-configuration",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        _check_as_metadata(r.json(), PUBLIC)
        assert r.json()["issuer"] == PUBLIC
    for path in (
        "/.well-known/oauth-protected-resource/skein/mcp",
        "/skein/.well-known/oauth-protected-resource",
        "/skein/.well-known/oauth-protected-resource/mcp",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        prm = r.json()
        assert prm["resource"] == RESOURCE
        assert prm["authorization_servers"] == [PUBLIC]
        assert prm["scopes_supported"] == ["skein"]


def test_metadata_without_base_path(make_client, team):
    client = make_client(base_path="", public_url="https://skein.example.com")
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        _check_as_metadata(client.get(path).json(), "https://skein.example.com")
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        assert client.get(path).json()["resource"] == "https://skein.example.com/mcp"
    r = client.get("/api/whoami")
    expected = 'resource_metadata="https://skein.example.com/.well-known/oauth-protected-resource/mcp"'
    assert expected in r.headers["www-authenticate"]


# -- 401 challenge ------------------------------------------------------------------


@pytest.mark.parametrize("path", [f"{BASE}/api/whoami", f"{BASE}/mcp"])
def test_401_carries_resource_metadata_when_enabled(client, path):
    r = client.get(path)
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
    assert r.headers["www-authenticate"] == OAUTH_CHALLENGE
    r = client.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers=bearer("skeino_not-a-real-token"))
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == OAUTH_CHALLENGE


def test_oauth_disabled_has_no_routes_and_plain_challenge(make_client, team):
    client = make_client(oauth_enabled=False)
    alpha = bearer(team["alpha"])
    r = client.get(f"{BASE}/api/whoami")
    assert r.status_code == 401 and r.headers["www-authenticate"] == PLAIN_CHALLENGE
    for path in (
        "/.well-known/oauth-authorization-server/skein",
        "/.well-known/oauth-protected-resource/skein/mcp",
        "/skein/.well-known/oauth-authorization-server",
        "/skein/authorize", "/skein/token", "/skein/register", "/skein/revoke",
        "/skein/oauth/consent",
    ):
        r = client.get(path)
        assert r.status_code == 401, path  # unknown paths stay behind auth, as before
        assert r.headers["www-authenticate"] == PLAIN_CHALLENGE
        assert client.get(path, headers=alpha).status_code == 404, path
    assert client.post(f"{BASE}/token", headers=alpha, data={"grant_type": "x"}).status_code == 404
    assert whoami_rest(client, team["alpha"]).json()["member"] == "alpha"
    assert whoami_mcp(client, team["beta"]).json()["result"]["structuredContent"]["member"] == "beta"


def test_access_tokens_stop_working_when_oauth_is_turned_off(make_client, team):
    on = make_client()
    access = sign_in(on, team["alpha"])["access_token"]
    assert whoami_rest(on, access).json()["member"] == "alpha"
    off = make_client(oauth_enabled=False)
    assert whoami_rest(off, access).status_code == 401
    assert whoami_rest(off, team["alpha"]).json()["member"] == "alpha"


# -- full flow with a dynamically registered client ---------------------------------


def test_full_flow_with_dcr_client(client, team, skein):
    reg = register(client, client_name="Gemini Test")
    assert reg["client_id"] and reg["client_secret"]
    assert reg["scope"] == "skein"
    verifier, challenge = pkce()

    r = authorize(client, reg["client_id"], challenge)
    req = consent_req(r)
    assert r.headers["cache-control"] == "no-store"

    page = client.get(f"{BASE}/oauth/consent", params={"req": req})
    assert page.status_code == 200
    assert "Gemini Test" in page.text
    assert "app.example.com" in page.text  # where approval sends the browser
    assert 'type="password"' in page.text

    wrong = consent(client, req, "skein_wrong-token")
    assert wrong.status_code == 400
    shown = html.unescape(wrong.text)
    assert "didn't match" in shown and "4 tries left" in shown
    assert "skein_wrong-token" not in wrong.text

    code = code_from(consent(client, req, team["alpha"]))
    # the request is used up once approved
    assert client.get(f"{BASE}/oauth/consent", params={"req": req}).status_code == 400

    r = exchange(client, reg, code, verifier)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    tokens = r.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["access_token"].startswith("skeino_")
    assert tokens["refresh_token"].startswith("skeinr_")
    assert tokens["expires_in"] == 3600
    assert tokens["scope"] == "skein"

    access = tokens["access_token"]
    assert whoami_rest(client, access).json()["member"] == "alpha"
    mcp = whoami_mcp(client, access).json()["result"]["structuredContent"]
    assert mcp["member"] == "alpha" and mcp["role"] == "member"
    # X-API-Key carries an access token just like a member token
    assert client.get(f"{BASE}/api/whoami", headers={"X-API-Key": access}).json()["member"] == "alpha"
    # the point signs in the same way and keeps its role
    point = sign_in(client, team["lead"])["access_token"]
    assert whoami_rest(client, point).json()["role"] == "point"
    # fixed tokens keep working next to OAuth
    assert whoami_rest(client, team["beta"]).json()["member"] == "beta"


def test_public_dcr_client_without_secret(client, team):
    reg = register(client, token_endpoint_auth_method="none")
    assert "client_secret" not in reg
    tokens = sign_in(client, team["beta"], reg)
    assert whoami_rest(client, tokens["access_token"]).json()["member"] == "beta"


def test_deny_redirects_with_access_denied(client, team):
    reg = register(client)
    _, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge))
    r = consent(client, req, "", decision="deny")
    location = handback(r)
    assert location.startswith(REDIRECT + "?")
    assert query(location) == {"error": "access_denied", "state": "st-123"}
    assert client.get(f"{BASE}/oauth/consent", params={"req": req}).status_code == 400


def test_handback_page_escapes_the_redirect(client, team):
    """The address goes into an attribute twice; a hostile state and a query with ``&`` and
    ``'`` come out escaped, and the browser reads back exactly the redirect skein built."""
    redirect = "https://app.example.com/it's/cb?next=a&mode=x"
    reg = register(client, redirect_uris=[redirect])
    verifier, challenge = pkce()
    state = '"><script>alert(1)</script>'
    req = consent_req(authorize(client, reg["client_id"], challenge, redirect_uri=redirect, state=state))
    r = consent(client, req, team["alpha"])
    assert "<script>" not in r.text and "it's" not in r.text
    assert "&amp;" in REFRESH_RE.findall(r.text)[0]
    location = handback(r)
    q = query(location)
    assert q["state"] == state and q["next"] == "a" and q["mode"] == "x"
    assert location.startswith("https://app.example.com/it's/cb?")
    assert exchange(client, reg, q["code"], verifier, redirect_uri=redirect).status_code == 200


def test_handback_to_a_private_use_scheme(client, team):
    """Apps on a device get the same page; the Continue link is there if a browser will not
    follow a refresh to an app scheme on its own."""
    app_redirect = "com.example.app:/oauth/callback"
    reg = register(client, redirect_uris=[app_redirect], token_endpoint_auth_method="none")
    verifier, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge, redirect_uri=app_redirect))
    code = code_from(consent(client, req, team["beta"]), redirect_uri=app_redirect)
    r = exchange(client, reg, code, verifier, redirect_uri=app_redirect)
    assert r.status_code == 200, r.text
    assert whoami_rest(client, r.json()["access_token"]).json()["member"] == "beta"


def test_five_bad_tokens_end_the_request(client, team, audit_path):
    reg = register(client)
    _, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge))
    for left in (4, 3, 2, 1):
        assert f"{left} tr" in consent(client, req, f"skein_bad-{left}").text
    last = consent(client, req, "skein_bad-0")
    assert last.status_code == 400 and "start again" in last.text.lower()
    # even the right token is refused now
    assert consent(client, req, team["alpha"]).status_code == 400
    failed = [e for e in oauth_events(audit_path) if e["event"] == "consent_failed"]
    assert [e["attempts"] for e in failed] == [1, 2, 3, 4, 5]
    assert all(e["client_id"] == reg["client_id"] for e in failed)


def test_disabled_member_token_is_refused_at_consent(client, team, skein):
    skein.set_disabled("beta", True)
    reg = register(client)
    _, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge))
    assert consent(client, req, team["beta"]).status_code == 400


def test_access_token_cannot_be_used_as_consent_token(client, team):
    access = sign_in(client, team["alpha"])["access_token"]
    reg = register(client)
    _, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge))
    assert consent(client, req, access).status_code == 400


# -- consent page -------------------------------------------------------------------


def test_consent_page_headers_and_escaping(client, team):
    reg = register(client, client_name='<script>alert("x")</script> & Co')
    _, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge))
    _, other_challenge = pkce()
    denied_req = consent_req(authorize(client, reg["client_id"], other_challenge))
    for r in (
        client.get(f"{BASE}/oauth/consent", params={"req": req}),
        consent(client, req, "skein_wrong"),
        client.get(f"{BASE}/oauth/consent", params={"req": "nope"}),
        consent(client, denied_req, "", decision="deny"),  # the hand-back page
    ):
        # exactly C8: the form posts only here, and the hand-back is not a form submission
        assert r.headers["content-security-policy"] == C8_CSP
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["content-type"].startswith("text/html")
        assert "<script>" not in r.text
        assert "http://" not in r.text.replace("http://www.w3.org", "")  # no external resources
    page = client.get(f"{BASE}/oauth/consent", params={"req": req}).text
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; Co" in page


def test_consent_page_for_unknown_request(client):
    r = client.get(f"{BASE}/oauth/consent", params={"req": "missing"})
    assert r.status_code == 400 and "expired" in r.text.lower()
    r = client.get(f"{BASE}/oauth/consent")
    assert r.status_code == 400


# -- codes, PKCE, redirect URIs -----------------------------------------------------


def _code_for(client: TestClient, token: str, reg: dict | None = None, redirect_uri: str = REDIRECT):
    reg = reg or register(client)
    verifier, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge, redirect_uri=redirect_uri))
    return reg, verifier, code_from(consent(client, req, token), redirect_uri=redirect_uri)


def test_code_reuse_is_rejected_and_revokes_its_tokens(client, team):
    reg, verifier, code = _code_for(client, team["alpha"])
    first = exchange(client, reg, code, verifier)
    assert first.status_code == 200
    again = exchange(client, reg, code, verifier)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    # RFC 6749 §4.1.2: a replayed code takes down what it issued
    assert whoami_rest(client, first.json()["access_token"]).status_code == 401
    assert refresh(client, reg, first.json()["refresh_token"]).status_code == 400


def test_wrong_pkce_verifier_is_rejected(client, team):
    reg, verifier, code = _code_for(client, team["alpha"])
    r = exchange(client, reg, code, pkce()[0])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_plain_pkce_is_refused(client, team):
    reg = register(client)
    verifier, _ = pkce()
    r = authorize(client, reg["client_id"], verifier, code_challenge_method="plain")
    assert r.status_code in (302, 400)
    assert "code=" not in r.headers.get("location", "")
    if r.status_code == 302:
        assert query(r.headers["location"])["error"] == "invalid_request"


def test_missing_pkce_is_refused(client, team):
    reg = register(client)
    r = client.get(f"{BASE}/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
    }, follow_redirects=False)
    assert "/oauth/consent" not in r.headers.get("location", "")


def test_redirect_uri_mismatch_is_rejected(client, team):
    reg = register(client)
    _, challenge = pkce()
    r = authorize(client, reg["client_id"], challenge, redirect_uri="https://evil.example.com/cb")
    assert r.status_code == 400
    assert "location" not in r.headers  # never redirect to an unregistered URI
    # at the token endpoint the redirect_uri must match the one the code was issued for
    other = "https://app.example.com/other"
    reg2 = register(client, redirect_uris=[REDIRECT, other])
    reg2, verifier, code = _code_for(client, team["alpha"], reg2)
    r = exchange(client, reg2, code, verifier, redirect_uri=other)
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_code_is_bound_to_its_client(client, team):
    reg, verifier, code = _code_for(client, team["alpha"])
    thief = register(client)
    r = exchange(client, thief, code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert exchange(client, reg, code, verifier).status_code == 200


def test_unknown_resource_is_refused(client, team):
    reg = register(client)
    _, challenge = pkce()
    r = authorize(client, reg["client_id"], challenge, resource="https://other.example.com/mcp")
    assert r.status_code == 302
    assert query(r.headers["location"])["error"] == "invalid_request"
    ok = authorize(client, reg["client_id"], challenge, resource=RESOURCE)
    consent_req(ok)


# -- refresh and revoke -------------------------------------------------------------


def test_refresh_rotates_and_old_refresh_is_rejected(client, team):
    tokens = sign_in(client, team["alpha"])
    reg = tokens["client"]
    r = refresh(client, reg, tokens["refresh_token"])
    assert r.status_code == 200, r.text
    fresh = r.json()
    assert fresh["refresh_token"] != tokens["refresh_token"]
    assert fresh["access_token"] != tokens["access_token"]
    assert whoami_rest(client, fresh["access_token"]).json()["member"] == "alpha"
    old = refresh(client, reg, tokens["refresh_token"])
    assert old.status_code == 400 and old.json()["error"] == "invalid_grant"
    assert refresh(client, reg, fresh["refresh_token"]).status_code == 200


def test_refresh_token_is_bound_to_its_client(client, team):
    tokens = sign_in(client, team["alpha"])
    other = register(client)
    r = refresh(client, other, tokens["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_revoke_ends_the_grant(client, team):
    tokens = sign_in(client, team["alpha"])
    reg = tokens["client"]
    r = client.post(f"{BASE}/revoke", data={"token": tokens["access_token"], "client_id": reg["client_id"],
                                            "client_secret": reg["client_secret"]})
    assert r.status_code == 200
    assert whoami_rest(client, tokens["access_token"]).status_code == 401
    assert refresh(client, reg, tokens["refresh_token"]).status_code == 400


@pytest.mark.parametrize("token_kind", ["access_token", "refresh_token"])
def test_public_client_revokes_with_token_and_client_id_only(client, team, token_kind):
    """RFC 7009 §2.1 as a public client sends it: no client_secret field at all."""
    reg = register(client, token_endpoint_auth_method="none")
    tokens = sign_in(client, team["alpha"], reg)
    r = client.post(f"{BASE}/revoke", data={"token": tokens[token_kind], "client_id": reg["client_id"]})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert whoami_rest(client, tokens["access_token"]).status_code == 401
    assert refresh(client, reg, tokens["refresh_token"]).status_code == 400


def test_secret_client_revokes_with_basic(client, team):
    reg = register(client, token_endpoint_auth_method="client_secret_basic")
    tokens = sign_in(client, team["alpha"], reg)
    r = client.post(f"{BASE}/revoke", data={"token": tokens["refresh_token"]},
                    headers=basic(reg["client_id"], reg["client_secret"]))
    assert r.status_code == 200, r.text
    assert whoami_rest(client, tokens["access_token"]).status_code == 401
    assert refresh(client, reg, tokens["refresh_token"]).status_code == 400


def test_secret_client_must_authenticate_to_revoke(client, team):
    reg = register(client)  # client_secret_post by default: it holds a secret
    tokens = sign_in(client, team["alpha"], reg)
    access = tokens["access_token"]
    for data, headers in (
        ({"token": access, "client_id": reg["client_id"]}, {}),                      # no secret at all
        ({"token": access, "client_id": reg["client_id"], "client_secret": ""}, {}),  # an empty one
        ({"token": access, "client_id": reg["client_id"], "client_secret": "0" * 64}, {}),
        ({"token": access}, basic(reg["client_id"], "0" * 64)),                     # wrong Basic secret
    ):
        r = client.post(f"{BASE}/revoke", data=data, headers=headers)
        assert r.status_code == 401, (data, r.text)
    assert whoami_rest(client, access).json()["member"] == "alpha"
    assert refresh(client, reg, tokens["refresh_token"]).status_code == 200


# -- client authentication with HTTP Basic (RFC 6749 §2.3.1) ------------------------


def _secret_client(client: TestClient, skein: Skein, source: str) -> dict:
    if source == "dcr":
        return register(client, token_endpoint_auth_method="client_secret_basic")
    added = skein.add_oauth_client("Pre-registered", [REDIRECT])
    return {"client_id": added["client_id"], "client_secret": added["client_secret"]}


@pytest.mark.parametrize("source", ["dcr", "cli"])
def test_basic_auth_without_body_client_id(client, team, skein, source):
    """The MCP TypeScript SDK prefers Basic and then sends no client_id in the body."""
    reg = _secret_client(client, skein, source)
    auth = basic(reg["client_id"], reg["client_secret"])
    reg_, verifier, code = _code_for(client, team["alpha"], reg)
    r = client.post(f"{BASE}/token", headers=auth, data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT,
    })
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert whoami_rest(client, tokens["access_token"]).json()["member"] == "alpha"
    r = client.post(f"{BASE}/token", headers=auth,
                    data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200, r.text
    assert whoami_rest(client, r.json()["access_token"]).json()["member"] == "alpha"
    # the same id in both places is fine too
    r = client.post(f"{BASE}/token", headers=auth, data={
        "grant_type": "refresh_token", "refresh_token": r.json()["refresh_token"], "client_id": reg["client_id"],
    })
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("other_auth", ["none", "client_secret_post"])
def test_basic_auth_client_id_mismatch_is_invalid_client(client, team, other_auth):
    reg = register(client, token_endpoint_auth_method="client_secret_basic")
    other = register(client, token_endpoint_auth_method=other_auth)
    reg_, verifier, code = _code_for(client, team["alpha"], reg)
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT}
    mine = basic(reg["client_id"], reg["client_secret"])
    theirs = basic(other["client_id"], other.get("client_secret", ""))
    for headers, body_id in ((mine, other["client_id"]), (theirs, reg["client_id"])):
        r = client.post(f"{BASE}/token", headers=headers, data={**data, "client_id": body_id})
        assert r.status_code == 401, r.text
        assert r.json()["error"] == "invalid_client"
        assert r.headers["www-authenticate"].startswith("Basic")
        assert r.headers["cache-control"] == "no-store"
    r = client.post(f"{BASE}/revoke", headers=mine, data={"token": "skeino_x", "client_id": other["client_id"]})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    # nothing was spent: the code still works with the right credentials
    r = client.post(f"{BASE}/token", headers=mine, data=data)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("source", ["dcr", "cli"])
def test_basic_auth_wrong_secret_is_401(client, team, skein, source):
    reg = _secret_client(client, skein, source)
    reg_, verifier, code = _code_for(client, team["alpha"], reg)
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT}
    for secret in ("0" * 64, "", reg["client_secret"] + "x"):
        r = client.post(f"{BASE}/token", headers=basic(reg["client_id"], secret), data=data)
        assert r.status_code == 401, (secret, r.text)
    r = client.post(f"{BASE}/token", headers=basic(reg["client_id"], reg["client_secret"]), data=data)
    assert r.status_code == 200, r.text
    r = client.post(f"{BASE}/token", headers=basic(reg["client_id"], "0" * 64),
                    data={"grant_type": "refresh_token", "refresh_token": r.json()["refresh_token"]})
    assert r.status_code == 401, r.text


# -- dynamic registration input -----------------------------------------------------


@pytest.mark.parametrize("body", [
    b"not json",
    b'{"redirect_uris": ["https://app.example.com/callback"]',  # cut short
    b"\xff\xfe\x00",                                            # not text
    b"[" * 100_000,                                             # nested too deep to parse
    b"[]",
    b'"https://app.example.com/callback"',
    b'{"redirect_uris": ["https://app.example.com/callback"], "client_name": "\\ud800"}',
    b'{"redirect_uris": ["https://app.example.com/callback"], "software_id": "x\\udc00"}',
    b'{"redirect_uris": ["https://app.example.com/callback"], "contacts": ["\\ud800@example.com"]}',
    b'{"redirect_uris": ["https://app.example.com/callback"], "\\ud800": 1}',
])
def test_register_refuses_bad_bodies_with_400(client, body, capsys):
    r = client.post(f"{BASE}/register", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid_client_metadata"
    assert client.app.state.skein.list_oauth_clients() == []
    assert "Traceback" not in "".join(capsys.readouterr())
    # and a good registration still goes through afterwards
    register(client)


# -- loopback redirect URIs (RFC 8252 §7.3) -----------------------------------------


def test_is_loopback_accepts_bracketed_ipv6():
    assert is_loopback("[::1]") and is_loopback("::1") and is_loopback("LOCALHOST")
    assert not is_loopback("[::2]") and not is_loopback("[127.0.0.1") and not is_loopback(None)


@pytest.mark.parametrize("registered, requested, ok", [
    ("http://127.0.0.1:5000/cb", "http://127.0.0.1:6000/cb", True),
    ("http://127.0.0.1:5000/cb", "http://127.0.0.1/cb", True),
    ("http://[::1]:5000/cb", "http://[::1]:6000/cb", True),
    ("http://localhost:5000/cb", "http://localhost:6000/cb", True),
    ("http://127.0.0.1:5000/cb", "http://127.0.0.1:6000/cb#f", False),
    ("http://127.0.0.1:5000/cb", "http://127.0.0.1:6000/cb#", False),
    ("http://127.0.0.1:5000/cb", "http://user:pw@127.0.0.1:6000/cb", False),
    ("http://127.0.0.1:5000/cb", "http://user@127.0.0.1:6000/cb", False),
    ("http://[::1]:5000/cb", "http://user@[::1]:6000/cb", False),
    ("http://[::1]:5000/cb", "http://[::1]:6000/cb#f", False),
    ("http://127.0.0.1:5000/cb", "http://localhost:6000/cb", False),
    ("http://127.0.0.1:5000/cb", "http://127.0.0.1:6000/other", False),
    ("http://127.0.0.1:5000/cb", "https://127.0.0.1:6000/cb", False),
    ("https://app.example.com/cb", "https://app.example.com:8443/cb", False),
])
def test_loopback_redirect_matching(registered, requested, ok):
    assert oauth_mod.redirect_allowed(requested, [registered]) is ok


def test_loopback_sign_in_on_another_port(client, team):
    reg = register(client, redirect_uris=["http://127.0.0.1:5000/cb", "http://[::1]:5000/cb"],
                   token_endpoint_auth_method="none")
    for requested in ("http://127.0.0.1:5000/cb#f", "http://user:pw@127.0.0.1:6000/cb",
                      "http://[::1]:6000/cb#f", "http://localhost:6000/cb"):
        _, challenge = pkce()
        r = authorize(client, reg["client_id"], challenge, redirect_uri=requested)
        assert r.status_code == 400 and "location" not in r.headers, requested
    for requested in ("http://127.0.0.1:6123/cb", "http://[::1]:6123/cb"):
        reg_, verifier, code = _code_for(client, team["alpha"], reg, redirect_uri=requested)
        r = exchange(client, reg, code, verifier, redirect_uri=requested)
        assert r.status_code == 200, (requested, r.text)
        assert whoami_rest(client, r.json()["access_token"]).json()["member"] == "alpha"


# -- expiry (injectable clock) ------------------------------------------------------


def test_expired_pending_request_is_rejected(client, team, clock):
    reg = register(client)
    _, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge))
    clock.advance(601)
    assert client.get(f"{BASE}/oauth/consent", params={"req": req}).status_code == 400
    assert consent(client, req, team["alpha"]).status_code == 400


def test_expired_code_is_rejected(client, team, clock):
    reg, verifier, code = _code_for(client, team["alpha"])
    clock.advance(601)
    r = exchange(client, reg, code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_expired_access_token_is_rejected_and_refresh_works(client, team, clock):
    tokens = sign_in(client, team["alpha"])
    clock.advance(3599)
    assert whoami_rest(client, tokens["access_token"]).status_code == 200
    clock.advance(2)
    r = whoami_rest(client, tokens["access_token"])
    assert r.status_code == 401 and r.headers["www-authenticate"] == OAUTH_CHALLENGE
    fresh = refresh(client, tokens["client"], tokens["refresh_token"]).json()
    assert whoami_rest(client, fresh["access_token"]).json()["member"] == "alpha"


def test_expired_refresh_token_is_rejected(make_client, team, clock):
    client = make_client(oauth_access_token_ttl=60, oauth_refresh_token_ttl=120)
    tokens = sign_in(client, team["alpha"])
    assert tokens["expires_in"] == 60
    clock.advance(121)
    assert refresh(client, tokens["client"], tokens["refresh_token"]).status_code == 400


# -- member changes revoke OAuth ----------------------------------------------------


def test_rotate_token_revokes_oauth_tokens(client, team, skein):
    tokens = sign_in(client, team["alpha"])
    pending = _code_for(client, team["alpha"])
    other = sign_in(client, team["beta"])
    skein.rotate_token("alpha")
    assert whoami_rest(client, tokens["access_token"]).status_code == 401
    assert refresh(client, tokens["client"], tokens["refresh_token"]).status_code == 400
    reg, verifier, code = pending
    assert exchange(client, reg, code, verifier).status_code == 400  # outstanding codes too
    assert whoami_rest(client, other["access_token"]).json()["member"] == "beta"


def test_disabling_a_member_revokes_oauth_tokens(client, team, skein):
    tokens = sign_in(client, team["alpha"])
    skein.set_disabled("alpha", True)
    assert whoami_rest(client, tokens["access_token"]).status_code == 401
    skein.set_disabled("alpha", False)
    # re-enabling does not bring the old grant back
    assert whoami_rest(client, tokens["access_token"]).status_code == 401
    assert refresh(client, tokens["client"], tokens["refresh_token"]).status_code == 400
    assert whoami_rest(client, team["alpha"]).json()["member"] == "alpha"


def _grants(skein: Skein, member: str) -> list[str]:
    """Every code and token the store holds for ``member``."""
    with skein.store.tx(write=False) as db:
        rows = db.conn.execute(
            "SELECT 'code' FROM oauth_codes WHERE member = ? "
            "UNION ALL SELECT kind FROM oauth_tokens WHERE member = ?", (member, member),
        ).fetchall()
    return [r[0] for r in rows]


@pytest.mark.parametrize("change", ["rotate", "disable"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_member_change_racing_consent_leaves_no_grant(client, team, skein, audit_path, monkeypatch,
                                                      change, when):
    """A rotate or disable that commits while a consent POST is in flight wins either way:
    just before the approval's write transaction (the token no longer matches, so no code) or
    just after it (the code it wrote is deleted with the member's other grants)."""
    decide = oauth_mod.SkeinOAuth._decide

    def member_change() -> None:
        if change == "rotate":
            skein.rotate_token("alpha")
        else:
            skein.set_disabled("alpha", True)

    def racing(self, *args):
        # the form's token has been read; the decision has not been written yet (or just was)
        if when == "before":
            member_change()
        outcome = decide(self, *args)
        if when == "after":
            member_change()
        return outcome

    monkeypatch.setattr(oauth_mod.SkeinOAuth, "_decide", racing)
    reg = register(client)
    verifier, challenge = pkce()
    req = consent_req(authorize(client, reg["client_id"], challenge))
    r = consent(client, req, team["alpha"])
    if when == "before":
        assert r.status_code == 400 and "location" not in r.headers
        assert "match an enabled member" in r.text
        consents = [e["event"] for e in oauth_events(audit_path) if e["event"].startswith("consent")]
        assert consents == ["consent_failed"]
    else:
        assert exchange(client, reg, code_from(r), verifier).status_code == 400
    assert _grants(skein, "alpha") == []  # no code, so no token can ever follow
    assert whoami_rest(client, team["alpha"]).status_code == 401  # the old fixed token is dead too


# -- CLI clients --------------------------------------------------------------------


@pytest.fixture
def cli_cfg(tmp_path, db_path, monkeypatch):
    monkeypatch.delenv("SKEIN_CONFIG", raising=False)
    path = tmp_path / "skein.toml"
    path.write_text(f'[storage]\ndb = "{db_path.as_posix()}"\n', encoding="utf-8")
    return path


def cli(cli_cfg, capsys, *args) -> tuple[int, str, str]:
    code = main(["--config", str(cli_cfg), *args])
    out, err = capsys.readouterr()
    return code, out, err


def _field(out: str, name: str) -> str:
    values = [ln.split(":", 1)[1].strip() for ln in out.splitlines() if ln.startswith(name + ":")]
    assert len(values) == 1, out
    return values[0]


def test_cli_client_with_secret_and_trust_on_first_use(client, team, cli_cfg, capsys):
    code, out, err = cli(cli_cfg, capsys, "oauth", "client", "add", "gemini")
    assert code == 0, err
    reg = {"client_id": _field(out, "client_id"), "client_secret": _field(out, "client_secret")}
    assert "not be shown again" in out
    first, second = "https://first.example.com/cb", "https://second.example.com/cb"

    # unlocked: plain http (not loopback) and a missing redirect_uri are refused
    _, challenge = pkce()
    assert authorize(client, reg["client_id"], challenge, redirect_uri="http://first.example.com/cb").status_code == 400
    assert authorize(client, reg["client_id"], challenge, redirect_uri=None).status_code == 400

    # two sign-ins start before either is approved; the first approval locks the client
    verifier, challenge = pkce()
    req1 = consent_req(authorize(client, reg["client_id"], challenge, redirect_uri=first))
    v2, c2 = pkce()
    req2 = consent_req(authorize(client, reg["client_id"], c2, redirect_uri=second))
    code1 = code_from(consent(client, req1, team["alpha"]), redirect_uri=first)
    late = consent(client, req2, team["alpha"])
    assert late.status_code == 400 and "redirect address" in html.unescape(late.text)

    # wrong secret: refused (post and basic); right secret: accepted
    bad = exchange(client, reg, code1, verifier, redirect_uri=first, client_secret="0" * 64)
    assert bad.status_code == 401 and bad.json()["error"] == "unauthorized_client"
    basic = base64.b64encode(f"{reg['client_id']}:{'0' * 64}".encode()).decode()
    data = {"grant_type": "authorization_code", "code": code1, "code_verifier": verifier,
            "redirect_uri": first, "client_id": reg["client_id"]}
    assert client.post(f"{BASE}/token", data=data, headers={"Authorization": f"Basic {basic}"}).status_code == 401
    no_secret = client.post(f"{BASE}/token", data=data)
    assert no_secret.status_code == 401
    basic = base64.b64encode(f"{reg['client_id']}:{reg['client_secret']}".encode()).decode()
    r = client.post(f"{BASE}/token", data=data, headers={"Authorization": f"Basic {basic}"})
    assert r.status_code == 200, r.text
    assert whoami_rest(client, r.json()["access_token"]).json()["member"] == "alpha"

    # locked now: exact match only
    _, challenge = pkce()
    assert authorize(client, reg["client_id"], challenge, redirect_uri=second).status_code == 400
    assert authorize(client, reg["client_id"], challenge, redirect_uri=first + "/x").status_code == 400
    consent_req(authorize(client, reg["client_id"], challenge, redirect_uri=first))
    code, out, _ = cli(cli_cfg, capsys, "oauth", "client", "list")
    assert code == 0 and reg["client_id"] in out and first in out and "gemini" in out
    assert reg["client_secret"] not in out


def test_cli_client_with_redirect_uris_is_exact_from_the_start(client, team, cli_cfg, capsys):
    loopback = "http://localhost:7777/oauth/callback"
    code, out, err = cli(cli_cfg, capsys, "oauth", "client", "add", "local-cli",
                         "--redirect-uri", loopback, "--redirect-uri", REDIRECT)
    assert code == 0, err
    reg = {"client_id": _field(out, "client_id"), "client_secret": _field(out, "client_secret")}
    _, challenge = pkce()
    assert authorize(client, reg["client_id"], challenge, redirect_uri="https://other.example.com/cb").status_code == 400
    reg, verifier, code_ = _code_for(client, team["beta"], reg, redirect_uri=loopback)
    r = exchange(client, reg, code_, verifier, redirect_uri=loopback)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("uri", [
    "http://app.example.com/cb", "javascript:alert(1)", "data:text/html,hi", "file:///etc/x",
    "https://app.example.com/cb#frag", "not a uri",
])
def test_cli_client_refuses_bad_redirect_uris(cli_cfg, capsys, uri):
    code, out, err = cli(cli_cfg, capsys, "oauth", "client", "add", "bad", "--redirect-uri", uri)
    assert code == 1 and "redirect" in err.lower()
    assert "client_secret" not in out


def test_cli_client_remove_revokes_tokens(client, team, cli_cfg, capsys, skein):
    tokens = sign_in(client, team["alpha"])
    cid = tokens["client"]["client_id"]
    code, out, _ = cli(cli_cfg, capsys, "oauth", "client", "list")
    assert cid in out and "dcr" in out
    code, out, err = cli(cli_cfg, capsys, "oauth", "client", "remove", cid)
    assert code == 0, err
    assert whoami_rest(client, tokens["access_token"]).status_code == 401
    assert cli(cli_cfg, capsys, "oauth", "client", "remove", cid)[0] == 1
    _, challenge = pkce()
    assert authorize(client, cid, challenge).status_code == 400


def test_cli_oauth_client_service_functions(skein):
    added = skein.add_oauth_client("Gemini", ["https://app.example.com/cb"])
    assert added["client_id"].startswith("cli_") and len(added["client_secret"]) >= 43
    listed = skein.list_oauth_clients()
    assert [c["client_id"] for c in listed] == [added["client_id"]]
    assert listed[0]["source"] == "cli" and listed[0]["redirect_uris"] == ["https://app.example.com/cb"]
    assert "client_secret" not in listed[0] and "secret_sha256" not in listed[0]
    removed = skein.remove_oauth_client(added["client_id"])
    assert removed["client_id"] == added["client_id"]
    assert skein.list_oauth_clients() == []


# -- dynamic registration limits ----------------------------------------------------


@pytest.mark.parametrize("uris", [
    ["http://app.example.com/cb"], ["javascript:alert(1)"], ["https://app.example.com/cb#x"],
])
def test_dcr_refuses_bad_redirect_uris(client, uris):
    r = client.post(f"{BASE}/register", json={"redirect_uris": uris})
    assert r.status_code == 400
    assert r.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")


def test_dcr_accepts_loopback_and_private_schemes(client):
    register(client, redirect_uris=["http://127.0.0.1:7777/cb", "http://localhost/cb"])
    register(client, redirect_uris=["com.example.app:/oauth/callback"])


def test_dcr_is_capped(make_client, team, monkeypatch, clock):
    monkeypatch.setattr(oauth_mod, "REGISTER_LIMIT", (1000, 60.0))
    monkeypatch.setattr(oauth_mod, "DCR_MAX_CLIENTS", 3)
    client = make_client()
    used = register(client)
    sign_in(client, team["alpha"], used)  # has tokens: never pruned
    register(client)
    register(client)
    r = client.post(f"{BASE}/register", json={"redirect_uris": [REDIRECT]})
    assert r.status_code == 400 and "limit" in r.json()["error_description"]
    # registrations over a day old that never signed anyone in make room
    clock.advance(86400 + 1)
    register(client)
    register(client)
    assert client.post(f"{BASE}/register", json={"redirect_uris": [REDIRECT]}).status_code == 400
    assert used["client_id"] in {c["client_id"] for c in client.app.state.skein.list_oauth_clients()}


def test_dcr_is_rate_limited(make_client, monkeypatch):
    monkeypatch.setattr(oauth_mod, "REGISTER_LIMIT", (3, 60.0))
    client = make_client()
    for _ in range(3):
        register(client)
    r = client.post(f"{BASE}/register", json={"redirect_uris": [REDIRECT]})
    assert r.status_code == 429
    assert "retry-after" in r.headers


def test_pending_requests_are_capped(make_client, team, monkeypatch):
    monkeypatch.setattr(oauth_mod, "PENDING_MAX", 3)
    client = make_client()
    reg = register(client)
    reqs = []
    for _ in range(5):
        _, challenge = pkce()
        reqs.append(consent_req(authorize(client, reg["client_id"], challenge)))
    alive = [client.get(f"{BASE}/oauth/consent", params={"req": r}).status_code for r in reqs]
    assert alive == [400, 400, 200, 200, 200]  # oldest dropped first


# -- identity isolation -------------------------------------------------------------


def test_two_oauth_members_concurrently_keep_their_identity(skein, team, tmp_path, audit_path):
    config = oauth_config(tmp_path, audit_path)
    with TestClient(create_app(config, skein), base_url=ORIGIN) as client:
        access = {name: sign_in(client, team[name])["access_token"] for name in ("alpha", "beta")}
    mine = {
        "alpha": {skein.assign("lead", "alpha", f"a{i}", "do it")["id"] for i in range(2)},
        "beta": {skein.assign("lead", "beta", f"b{i}", "do it")["id"] for i in range(3)},
    }
    app = create_app(config, skein)
    rounds = 10
    results: list[tuple[str, str, dict]] = []

    async def one(name: str, i: int) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers=bearer(access[name])) as c:
            body = {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                    "params": {"name": "skein_inbox", "arguments": {}}}
            mcp = (await c.post(f"{BASE}/mcp", json=body)).json()["result"]["structuredContent"]
            rest = (await c.get(f"{BASE}/api/whoami")).json()
            results.append((name, "mcp", mcp))
            results.append((name, "rest", rest))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            async with anyio.create_task_group() as tg:
                for i in range(rounds):
                    tg.start_soon(one, "alpha", i)
                    tg.start_soon(one, "beta", i)

    anyio.run(run)
    assert len(results) == 4 * rounds
    for name, kind, data in results:
        if kind == "rest":
            assert data["member"] == name
        else:
            assert {t["id"] for t in data["tasks"]} == mine[name]


# -- store migration ----------------------------------------------------------------

V1_SCHEMA = """
CREATE TABLE members(name TEXT PRIMARY KEY, display TEXT NOT NULL, token_sha256 TEXT UNIQUE NOT NULL,
    created REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, last_seen REAL);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, assignee TEXT NOT NULL,
    created_by TEXT NOT NULL, status TEXT NOT NULL, created REAL, claimed REAL, updated REAL,
    finished REAL, result TEXT, notes TEXT NOT NULL DEFAULT '[]', seen_by_point INTEGER NOT NULL DEFAULT 1);
CREATE TABLE asks(id TEXT PRIMARY KEY, asker TEXT NOT NULL, question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
    created REAL, answered REAL, answer TEXT, answered_by TEXT, seen_by_asker INTEGER NOT NULL DEFAULT 0);
CREATE INDEX tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX tasks_status ON tasks(status);
CREATE INDEX asks_status ON asks(status);
CREATE INDEX asks_asker_status ON asks(asker, status);
"""


def test_v1_database_migrates_in_place(tmp_path, clock, audit_path):
    path = tmp_path / "live.db"
    token = "skein_" + "v" * 43
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO members VALUES ('sam', 'Sam', ?, 1.0, 0, NULL)",
                 (hashlib.sha256(token.encode()).hexdigest(),))
    conn.execute("INSERT INTO members VALUES ('lead', 'Lead', 'x' , 1.0, 0, NULL)")
    conn.execute("INSERT INTO meta VALUES ('point', 'lead')")
    conn.execute("INSERT INTO tasks(id, title, body, assignee, created_by, status, created, updated) "
                 "VALUES ('t_0123456789', 'Old task', 'body', 'sam', 'lead', 'queued', 2.0, 2.0)")
    conn.execute("INSERT INTO asks(id, asker, question, status, created) "
                 "VALUES ('a_0123456789', 'sam', 'why?', 'open', 3.0)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    skein = Skein(Store(path), clock=clock)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"oauth_clients", "oauth_pending", "oauth_codes", "oauth_tokens"} <= tables
    finally:
        conn.close()
    assert skein.authenticate(token) == "sam"
    assert skein.get_point() == "lead"
    assert skein.get_task("sam", "t_0123456789")["title"] == "Old task"
    assert skein.get_ask("sam", "a_0123456789")["question"] == "why?"
    Store(path)  # reopening a v2 file is a no-op
    assert skein.authenticate(token) == "sam"

    # and the migrated database serves OAuth sign-in
    config = oauth_config(tmp_path, audit_path, db=str(path))
    with TestClient(create_app(config, skein), base_url=ORIGIN) as client:
        access = sign_in(client, token)["access_token"]
        assert whoami_rest(client, access).json()["member"] == "sam"


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 3")
    conn.close()
    with pytest.raises(RuntimeError, match="schema version 3"):
        Store(path)


# -- nothing secret reaches a log ---------------------------------------------------


def test_audit_and_logs_never_contain_secrets(client, team, audit_path, capsys, caplog, cli_cfg):
    caplog.set_level(logging.DEBUG)
    secrets_seen: list[str] = list(team.values())
    code, out, _ = cli(cli_cfg, capsys, "oauth", "client", "add", "cli-app", "--redirect-uri", REDIRECT)
    cli_reg = {"client_id": _field(out, "client_id"), "client_secret": _field(out, "client_secret")}
    secrets_seen.append(cli_reg["client_secret"])

    reg = register(client, client_name="Audited")
    secrets_seen.append(reg["client_secret"])
    for r in (reg, cli_reg):
        verifier, challenge = pkce()
        req = consent_req(authorize(client, r["client_id"], challenge))
        wrong = "skein_wrong-" + secrets.token_hex(8)
        secrets_seen.append(wrong)
        consent(client, req, wrong)
        code = code_from(consent(client, req, team["alpha"]))
        secrets_seen += [verifier, code]
        bad = exchange(client, r, code, pkce()[0])  # wrong verifier
        assert bad.status_code == 400
        tokens = exchange(client, r, code, verifier).json()
        exchange(client, r, code, verifier)  # replay
        secrets_seen += [tokens["access_token"], tokens["refresh_token"]]
    tokens = sign_in(client, team["beta"], reg)
    fresh = refresh(client, reg, tokens["refresh_token"]).json()
    secrets_seen += [tokens["access_token"], tokens["refresh_token"], fresh["access_token"], fresh["refresh_token"]]
    whoami_rest(client, fresh["access_token"])
    whoami_rest(client, "skeino_" + "x" * 43)
    client.post(f"{BASE}/revoke", data={"token": fresh["refresh_token"], "client_id": reg["client_id"],
                                        "client_secret": reg["client_secret"]})

    out, err = capsys.readouterr()
    raw = audit_path.read_text() + out + err + caplog.text
    for secret in secrets_seen:
        assert secret not in raw

    events = oauth_events(audit_path)
    kinds = {e["event"] for e in events}
    assert {"register", "consent_failed", "consent_ok", "token_issue", "token_refresh",
            "token_revoke", "code_reuse", "token_failed"} <= kinds
    ok = next(e for e in events if e["event"] == "consent_ok")
    assert ok["member"] == "alpha" and ok["client_id"] == reg["client_id"]
    issue = next(e for e in events if e["event"] == "token_issue")
    assert issue["member"] == "alpha" and issue["client_id"]
    assert all(set(e) <= {"t", "kind", "event", "client_id", "member", "attempts", "status", "source",
                          "route"} for e in events)
