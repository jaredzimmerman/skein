"""OAuth 2.1 sign-in for MCP clients that connect only through OAuth (PLAN.md C8).

skein is its own authorization server and resource server. OAuth is one more way to
*become* a member: the person signing in pastes that member's skein token once, on
skein's consent page, and the app gets its own revocable tokens. Fixed member tokens
keep working everywhere.

```
app  → GET  <base>/authorize        SDK handler → authorize(): store a pending request (10 min)
     ← 302  <public_url>/oauth/consent?req=<id>
you  → POST <base>/oauth/consent    member token → one-time code (10 min, hashed)
     ← 303  <redirect_uri>?code=…&state=…
app  → POST <base>/token            SDK handler → skeino_… access + skeinr_… refresh tokens
app  → <base>/mcp, <base>/api/…     Bearer skeino_… → AuthMiddleware → authenticate() → member
```

From mcp 1.30 (``mcp.server.auth``) this uses the provider protocol and its models, the
authorize, token, register and revoke handlers, ``build_metadata``, the metadata
handlers and ``ClientAuthenticator``. They are mounted here rather than through
``create_auth_routes`` because that mounts at the root path and builds a
``ClientAuthenticator`` that compares plaintext secrets, while skein keeps only secret
hashes (see :class:`HashedSecretAuthenticator`, which wraps the SDK's).

Where the rules live: the grant rules (expiry, bad-attempt limit, what a code or token
is bound to, single use, refresh rotation, trust-on-first-use locking) are in this
module; ``store.py`` only reads and writes the rows; ``service.py`` owns the owner's
client admin functions and ends a member's grants when its token rotates or it is
disabled. Member identity still has one path: :meth:`SkeinOAuth.authenticate` returns
a member name to the same auth middleware fixed tokens go through.

Every secret (client secrets, codes, access and refresh tokens, and the member token
typed on the consent page) is stored only as a SHA-256 hash, or not at all, and never
reaches the audit log: OAuth events record client ids, member names and counts only.

Time: rules use the service's injectable clock. The SDK's token handler also checks
``expires_at`` against the real clock, so expiry times handed to it are translated to
"real now + time left".
"""

from __future__ import annotations

import base64
import binascii
import html
import math
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import anyio.to_thread
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, build_resource_metadata_url, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.streamable_http import MCP_PROTOCOL_VERSION_HEADER
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE, RequestBodyLimitMiddleware
from mcp.shared.auth import (
    InvalidRedirectUriError,
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import AnyHttpUrl, AnyUrl, Field, ValidationError
from starlette.datastructures import FormData
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, request_response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import Audit
from .config import Config
from .errors import SkeinError
from .service import OAUTH_CLI, OAUTH_DCR, TOKEN_MAX, Skein, check_redirect_uri, is_loopback
from .tokens import hash_token, hashes_match

SCOPE = "skein"
ACCESS_PREFIX = "skeino_"
REFRESH_PREFIX = "skeinr_"
PENDING_TTL = 600.0          # a consent request lives 10 minutes
PENDING_MAX_ATTEMPTS = 5     # wrong member tokens before the request is dropped
PENDING_MAX = 200            # live consent requests kept; the oldest go first
CODE_TTL = 600.0             # authorization codes live 10 minutes
DCR_MAX_CLIENTS = 50         # dynamically registered clients
DCR_IDLE_AFTER = 24 * 3600.0  # at the cap, unused registrations older than this make room
REGISTER_LIMIT = (10, 60.0)  # registrations per window (seconds), whole server
CONSENT_BODY_MAX = 64 * 1024
CLIENT_ID_MAX = 200
STATE_MAX = 2000
NAME_MAX = 100
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # S256: base64url of 32 bytes, no padding
_CSP_SOURCE_RE = re.compile(r"[a-z][a-z0-9+.-]*:(//[a-z0-9.:\[\]-]+)?")

PAGE_HEADERS = {
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


# -- small helpers -----------------------------------------------------------------


async def _run(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking (SQLite) call off the event loop, like the rest of skein."""
    return await anyio.to_thread.run_sync(fn, *args)


def _new_secret(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def _unmatchable() -> str:
    """A ``client_secret`` value no request can present: what the SDK sees for a
    confidential client whose real secret we only know by hash."""
    return "unmatchable:" + secrets.token_hex(32)


def _form_str(form: FormData, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _member_for_token(db: Any, token: str) -> str | None:
    """The enabled member a fixed member token belongs to, read in the caller's transaction
    (the same check as :meth:`Skein.authenticate`, but not in a transaction of its own)."""
    if not token or len(token) > TOKEN_MAX:
        return None
    digest = hash_token(token)
    row = db.get_member_by_token_hash(digest)
    if row is None or row["disabled"] or not hashes_match(row["token_sha256"], digest):
        return None
    return row["name"]


def _basic_secret(header: str) -> str | None:
    """The secret in ``Authorization: Basic …``, decoded the way the SDK decodes it."""
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    if ":" not in decoded:
        return None
    return unquote(decoded.split(":", 1)[1])


def _same_url(a: str, b: str) -> bool:
    try:
        return AnyUrl(a) == AnyUrl(b)
    except ValidationError:
        return a == b


def _loopback_any_port(registered: str, requested: str) -> bool:
    """RFC 8252 §7.3: a loopback http redirect URI matches whatever port the app opened."""
    try:
        r, q = AnyUrl(registered), AnyUrl(requested)
    except ValidationError:
        return False
    return (
        r.scheme == q.scheme == "http"
        and is_loopback(r.host)
        and (r.host or "").lower() == (q.host or "").lower()
        and r.path == q.path
        and r.query == q.query
    )


def redirect_allowed(requested: str, registered: list[str]) -> bool:
    return any(_same_url(u, requested) or _loopback_any_port(u, requested) for u in registered)


def _csp_source(uri: str) -> str | None:
    """The CSP source for where the consent form's redirect goes (origin, or ``scheme:``)."""
    try:
        parts = urlsplit(uri)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme in ("http", "https") and host:
        host = f"[{host}]" if ":" in host else host
        source = f"{scheme}://{host}" + (f":{port}" if port else "")
    else:
        source = f"{scheme}:"
    return source if _CSP_SOURCE_RE.fullmatch(source) else None


def _where(uri: str) -> str:
    """Where approving sends the browser, for people: the host, or the app's scheme."""
    try:
        parts = urlsplit(uri)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "the app"
    if parts.scheme in ("http", "https") and host:
        return host + (f":{port}" if port else "")
    return f"the app ({parts.scheme}:)"


class RateLimiter:
    """At most ``limit`` events per ``window`` seconds (sliding), for the whole process."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self.events: deque[float] = deque()

    def allow(self) -> tuple[bool, int]:
        """(allowed, seconds until the next event would be allowed)."""
        now = self.clock()
        while self.events and now - self.events[0] >= self.window:
            self.events.popleft()
        if len(self.events) >= self.limit:
            return False, max(1, math.ceil(self.window - (now - self.events[0])))
        self.events.append(now)
        return True, 0


# -- SDK-facing models ------------------------------------------------------------


class SkeinClient(OAuthClientInformationFull):
    """A client as the SDK handlers see it.

    skein has one scope and grants it whatever was asked (RFC 6749 §3.3 lets the server
    choose; the token response names it). Redirect URIs match exactly, except that a
    loopback http URI may use any port (RFC 8252 §7.3). An owner-added client with no
    redirect URI yet (``unlocked``) accepts any https redirect URI until the first
    approved sign-in locks it to that one (trust on first use).
    """

    unlocked: bool = Field(default=False, exclude=True)

    def validate_scope(self, requested_scope: str | None) -> list[str] | None:
        return [SCOPE]

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if self.unlocked:
            if redirect_uri is None:
                raise InvalidRedirectUriError("redirect_uri is required for this client's first sign-in")
            if redirect_uri.scheme != "https":
                raise InvalidRedirectUriError("this client's first redirect_uri must use https")
            try:
                check_redirect_uri(str(redirect_uri))
            except SkeinError as exc:
                raise InvalidRedirectUriError(exc.message) from None
            return redirect_uri
        if redirect_uri is not None and self.redirect_uris:
            registered = [str(u) for u in self.redirect_uris]
            if redirect_allowed(str(redirect_uri), registered):
                return redirect_uri
        return super().validate_redirect_uri(redirect_uri)


class HashedSecretAuthenticator(ClientAuthenticator):
    """The SDK's client authentication for the token and revoke endpoints, with client
    secrets kept only as hashes.

    The SDK compares the presented secret with ``client.client_secret`` from
    ``get_client``. For one request, this hands it a client whose ``client_secret`` is
    the presented secret when (and only when) its hash matches the stored hash, and an
    unmatchable value otherwise; the SDK then does every check as usual. A confidential
    client may authenticate with ``client_secret_basic`` or ``client_secret_post``
    (both are advertised): the method is taken from whether a Basic header is present.
    """

    def __init__(self, oauth: "SkeinOAuth") -> None:
        super().__init__(oauth)  # type: ignore[arg-type]
        self.oauth = oauth

    async def authenticate_request(self, request: Request) -> OAuthClientInformationFull:
        form = await request.form()  # Starlette caches it for the SDK's own reads
        header = request.headers.get("authorization", "")
        basic = header.startswith("Basic ")
        presented = _basic_secret(header) if basic else (_form_str(form, "client_secret") or None)
        oauth = self.oauth

        class OneRequest:
            async def get_client(self, client_id: str) -> SkeinClient | None:
                return await _run(oauth.client_for_secret, client_id, presented, basic)

        return await ClientAuthenticator(OneRequest()).authenticate_request(request)  # type: ignore[arg-type]


# -- consent outcomes ----------------------------------------------------------------


@dataclass
class _Outcome:
    kind: str  # form | expired | locked | conflict | redirect
    view: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    url: str = ""


# -- the provider ----------------------------------------------------------------------


class SkeinOAuth:
    """skein's authorization server: the SDK provider protocol, the consent page, the
    routes, and the access-token check the auth middleware uses."""

    def __init__(self, skein: Skein, config: Config, audit: Audit) -> None:
        if not config.oauth_enabled or not config.public_url:
            raise ValueError("SkeinOAuth needs oauth.enabled and server.public_url")
        self.skein = skein
        self.store = skein.store
        self.audit = audit
        self.base = config.base_path
        self.issuer = config.public_url
        self.resource = config.public_url + "/mcp"
        self.resource_metadata_url = str(build_resource_metadata_url(AnyHttpUrl(self.resource)))
        self.challenge = f'Bearer realm="skein", resource_metadata="{self.resource_metadata_url}"'
        self.access_ttl = config.oauth_access_token_ttl
        self.refresh_ttl = config.oauth_refresh_token_ttl
        self.consent_path = f"{self.base}/oauth/consent"
        self.accepted_resources = frozenset(
            {self.resource, self.resource + "/", self.issuer, self.issuer + "/"}
        )
        self.register_limiter = RateLimiter(*REGISTER_LIMIT)

    def _now(self) -> float:
        return float(self.skein.clock())

    def _sdk_time(self, expires: float, now: float) -> int:
        """An expiry for the SDK, which compares it with the real clock."""
        return math.ceil(time.time() + (expires - now))

    # -- clients ---------------------------------------------------------------------

    @staticmethod
    def _client_model(row: dict[str, Any], secret: str | None = None, basic: bool = False) -> SkeinClient:
        confidential = row["secret_sha256"] is not None
        uris: list[AnyUrl] = []
        for uri in row["redirect_uris"]:
            try:
                uris.append(AnyUrl(uri))
            except ValidationError:
                continue
        method = row["auth_method"]
        if confidential:
            method = "client_secret_basic" if basic else "client_secret_post"
        return SkeinClient(
            client_id=row["client_id"],
            client_name=row["name"] or None,
            client_secret=(secret or _unmatchable()) if confidential else None,
            token_endpoint_auth_method=method,
            redirect_uris=uris or None,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=SCOPE,
            unlocked=row["source"] == OAUTH_CLI and not row["redirect_uris"],
        )

    def _client_row(self, client_id: Any) -> dict[str, Any] | None:
        if not isinstance(client_id, str) or not client_id or len(client_id) > CLIENT_ID_MAX:
            return None
        with self.store.tx(write=False) as db:
            return db.get_oauth_client(client_id)

    def client_for_secret(self, client_id: str, presented: str | None, basic: bool) -> SkeinClient | None:
        """The client for one token/revoke request, carrying the presented secret only if
        it matches the stored hash (see :class:`HashedSecretAuthenticator`)."""
        row = self._client_row(client_id)
        if row is None:
            return None
        secret = None
        if row["secret_sha256"] is not None and presented and len(presented) <= TOKEN_MAX:
            if hashes_match(row["secret_sha256"], hash_token(presented)):
                secret = presented
        return self._client_model(row, secret=secret, basic=basic)

    async def get_client(self, client_id: str) -> SkeinClient | None:
        row = await _run(self._client_row, client_id)
        return None if row is None else self._client_model(row)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await _run(self._register, client_info)

    def _register(self, info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (info.redirect_uris or [])]
        if not uris:
            raise RegistrationError("invalid_redirect_uri", "at least one redirect_uri is required")
        for uri in uris:
            try:
                check_redirect_uri(uri)
            except SkeinError as exc:
                raise RegistrationError("invalid_redirect_uri", exc.message) from None
        if info.token_endpoint_auth_method not in AUTH_METHODS:
            raise RegistrationError(
                "invalid_client_metadata",
                "token_endpoint_auth_method must be none, client_secret_post or client_secret_basic",
            )
        info.scope = SCOPE  # the registration response tells the app what it got
        name = " ".join((info.client_name or "").split())[:NAME_MAX]
        now = self._now()
        with self.store.tx() as db:
            full = db.count_oauth_clients(OAUTH_DCR) >= DCR_MAX_CLIENTS
            if full:
                db.purge_oauth(now)
                db.delete_idle_oauth_clients(OAUTH_DCR, now - DCR_IDLE_AFTER)
                full = db.count_oauth_clients(OAUTH_DCR) >= DCR_MAX_CLIENTS
            if not full:
                db.insert_oauth_client(
                    {
                        "client_id": info.client_id,
                        "name": name,
                        "source": OAUTH_DCR,
                        "secret_sha256": hash_token(info.client_secret) if info.client_secret else None,
                        "auth_method": info.token_endpoint_auth_method,
                        "redirect_uris": uris,
                        "created": now,
                    }
                )
        if full:
            raise RegistrationError(
                "invalid_client_metadata",
                "this skein has reached its limit of registered apps; ask its owner to remove "
                "unused ones (skein oauth client list, skein oauth client remove ID)",
            )
        self.audit.log(kind="oauth", event="register", client_id=info.client_id, source=OAUTH_DCR)

    # -- authorize → consent → code ------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        return await _run(self._authorize, client, params)

    def _authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource not in self.accepted_resources:
            raise AuthorizeError("invalid_request", f"unknown resource; this server is {self.resource}")
        if not CHALLENGE_RE.fullmatch(params.code_challenge):
            raise AuthorizeError(
                "invalid_request", "code_challenge must be an S256 challenge (43 base64url characters)"
            )
        if params.state is not None and len(params.state) > STATE_MAX:
            raise AuthorizeError("invalid_request", f"state is longer than {STATE_MAX} characters")
        now = self._now()
        pending_id = _new_secret()
        with self.store.tx() as db:
            db.purge_oauth(now)
            db.trim_oauth_pending(PENDING_MAX - 1)
            db.insert_oauth_pending(
                {
                    "id": pending_id,
                    "client_id": client.client_id,
                    "redirect_uri": str(params.redirect_uri),
                    "redirect_explicit": 1 if params.redirect_uri_provided_explicitly else 0,
                    "code_challenge": params.code_challenge,
                    "state": params.state,
                    "scope": SCOPE,
                    "resource": self.resource,
                    "created": now,
                    "expires": now + PENDING_TTL,
                    "attempts": 0,
                }
            )
        return f"{self.issuer}/oauth/consent?req={pending_id}"

    @staticmethod
    def _live_pending(db: Any, req: str, now: float) -> dict[str, Any] | None:
        if not req or len(req) > 100:
            return None
        row = db.get_oauth_pending(req)
        if row is None or row["expires"] <= now:
            return None
        return row

    @staticmethod
    def _view(pending: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
        return {
            "client_id": client["client_id"],
            "client_name": client["name"],
            "source": client["source"],
            "redirect_uri": pending["redirect_uri"],
            "attempts": pending["attempts"],
        }

    def _pending_view(self, req: str) -> dict[str, Any] | None:
        with self.store.tx(write=False) as db:
            pending = self._live_pending(db, req, self._now())
            client = db.get_oauth_client(pending["client_id"]) if pending else None
            return self._view(pending, client) if pending and client else None

    def _deny(self, req: str) -> _Outcome:
        with self.store.tx() as db:
            pending = self._live_pending(db, req, self._now())
            if pending is None:
                return _Outcome("expired")
            db.delete_oauth_pending(req)
        self.audit.log(kind="oauth", event="consent_denied", client_id=pending["client_id"])
        url = construct_redirect_uri(pending["redirect_uri"], error="access_denied", state=pending["state"])
        return _Outcome("redirect", url=url)

    def _decide(self, req: str, token: str) -> _Outcome:
        """Approve as the member whose fixed token this is, or count a token that matches no
        enabled member.

        The token is resolved inside the write transaction that issues the code, so a
        ``rotate_token`` or disable commits either before it (the token no longer matches: no
        code) or after it (and then deletes the new code with the member's other grants).
        """
        now = self._now()
        event: dict[str, Any] = {}
        with self.store.tx() as db:
            pending = self._live_pending(db, req, now)
            client = db.get_oauth_client(pending["client_id"]) if pending else None
            if pending is None or client is None:
                return _Outcome("expired")
            cid = client["client_id"]
            member = _member_for_token(db, token)
            if member is None:
                attempts = pending["attempts"] + 1
                event = {"event": "consent_failed", "client_id": cid, "attempts": attempts}
                if attempts >= PENDING_MAX_ATTEMPTS:
                    db.delete_oauth_pending(req)
                    outcome = _Outcome("locked")
                else:
                    db.set_oauth_pending_attempts(req, attempts)
                    view = self._view({**pending, "attempts": attempts}, client)
                    left = PENDING_MAX_ATTEMPTS - attempts
                    outcome = _Outcome(
                        "form", view=view,
                        error=f"That token didn't match an enabled member. "
                        f"{left} {'try' if left == 1 else 'tries'} left.",
                    )
            elif client["source"] == OAUTH_CLI and client["redirect_uris"] and not redirect_allowed(
                pending["redirect_uri"], client["redirect_uris"]
            ):
                # an owner-added client locked (trust on first use) after this request began
                db.delete_oauth_pending(req)
                event = {"event": "consent_conflict", "client_id": cid, "member": member}
                outcome = _Outcome("conflict", view=self._view(pending, client))
            else:
                if client["source"] == OAUTH_CLI and not client["redirect_uris"]:
                    db.set_oauth_client_redirect_uris(cid, [pending["redirect_uri"]])
                code = _new_secret()
                db.insert_oauth_code(
                    {
                        "code_sha256": hash_token(code),
                        "client_id": cid,
                        "member": member,
                        "redirect_uri": pending["redirect_uri"],
                        "redirect_explicit": pending["redirect_explicit"],
                        "code_challenge": pending["code_challenge"],
                        "scope": pending["scope"],
                        "resource": pending["resource"],
                        "grant_id": secrets.token_hex(16),
                        "created": now,
                        "expires": now + CODE_TTL,
                    }
                )
                db.delete_oauth_pending(req)
                event = {"event": "consent_ok", "client_id": cid, "member": member}
                url = construct_redirect_uri(pending["redirect_uri"], code=code, state=pending["state"])
                outcome = _Outcome("redirect", url=url)
        self.audit.log(kind="oauth", **event)
        return outcome

    # -- codes and tokens (SDK provider protocol) --------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return await _run(self._load_code, client, authorization_code)

    def _load_code(self, client: OAuthClientInformationFull, code: str) -> AuthorizationCode | None:
        if not isinstance(code, str) or not code or len(code) > TOKEN_MAX:
            return None
        now = self._now()
        with self.store.tx() as db:
            row = db.get_oauth_code(hash_token(code))
            if row is None or row["client_id"] != client.client_id:
                return None
            if row["used"] is not None:
                # RFC 6749 §4.1.2: a code presented twice revokes what it issued
                db.delete_oauth_grant(row["grant_id"])
            elif row["expires"] > now:
                return AuthorizationCode(
                    code=code,
                    scopes=row["scope"].split(),
                    expires_at=self._sdk_time(row["expires"], now),
                    client_id=row["client_id"],
                    code_challenge=row["code_challenge"],
                    redirect_uri=AnyUrl(row["redirect_uri"]),
                    redirect_uri_provided_explicitly=bool(row["redirect_explicit"]),
                    resource=row["resource"],
                    subject=row["member"],
                )
            else:
                return None
        self.audit.log(kind="oauth", event="code_reuse", client_id=row["client_id"], member=row["member"])
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        return await _run(self._exchange_code, client, authorization_code)

    def _exchange_code(self, client: OAuthClientInformationFull, code: AuthorizationCode) -> OAuthToken:
        now = self._now()
        digest = hash_token(code.code)
        with self.store.tx() as db:
            row = db.get_oauth_code(digest)
            if (
                row is None
                or row["client_id"] != client.client_id
                or row["expires"] <= now
                or not db.mark_oauth_code_used(digest, now)
            ):
                raise TokenError("invalid_grant", "authorization code is invalid, expired or already used")
            member = db.get_member(row["member"])
            if member is None or member["disabled"]:
                raise TokenError("invalid_grant", "the member this sign-in was for is disabled")
            tokens = self._issue(db, row, now)
        self.audit.log(kind="oauth", event="token_issue", client_id=row["client_id"], member=row["member"])
        return tokens

    def _issue(self, db: Any, grant: dict[str, Any], now: float) -> OAuthToken:
        """A new access and refresh token for one grant (member + client + resource)."""
        access, refresh = _new_secret(ACCESS_PREFIX), _new_secret(REFRESH_PREFIX)
        for token, kind, ttl in ((access, "access", self.access_ttl), (refresh, "refresh", self.refresh_ttl)):
            db.insert_oauth_token(
                {
                    "token_sha256": hash_token(token),
                    "kind": kind,
                    "client_id": grant["client_id"],
                    "member": grant["member"],
                    "grant_id": grant["grant_id"],
                    "scope": SCOPE,
                    "resource": grant["resource"],
                    "created": now,
                    "expires": now + ttl,
                }
            )
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=self.access_ttl, scope=SCOPE,
            refresh_token=refresh,
        )

    def _token_row(self, token: Any, kind: str) -> dict[str, Any] | None:
        """A live token of ``kind`` whose member exists and is enabled, else None."""
        prefix = ACCESS_PREFIX if kind == "access" else REFRESH_PREFIX
        if not isinstance(token, str) or not token.startswith(prefix) or len(token) > TOKEN_MAX:
            return None
        with self.store.tx(write=False) as db:
            row = db.get_oauth_token(hash_token(token))
        if (
            row is None
            or row["kind"] != kind
            or row["expires"] <= self._now()
            or row["member_disabled"] != 0
            or row["resource"] != self.resource
        ):
            return None
        return row

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = await _run(self._token_row, refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scope"].split(),
            expires_at=self._sdk_time(row["expires"], self._now()),
            resource=row["resource"],
            subject=row["member"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        return await _run(self._exchange_refresh, client, refresh_token)

    def _exchange_refresh(self, client: OAuthClientInformationFull, refresh_token: RefreshToken) -> OAuthToken:
        now = self._now()
        digest = hash_token(refresh_token.token)
        with self.store.tx() as db:
            row = db.get_oauth_token(digest)
            if (
                row is None
                or row["kind"] != "refresh"
                or row["client_id"] != client.client_id
                or row["expires"] <= now
                or row["member_disabled"] != 0
                or not db.delete_oauth_token(digest)  # rotation: the old refresh token dies here
            ):
                raise TokenError("invalid_grant", "refresh token is invalid, expired or already used")
            tokens = self._issue(db, row, now)
        self.audit.log(kind="oauth", event="token_refresh", client_id=row["client_id"], member=row["member"])
        return tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await _run(self._token_row, token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scope"].split(),
            expires_at=self._sdk_time(row["expires"], self._now()),
            resource=row["resource"],
            subject=row["member"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await _run(self._revoke, token.token)

    def _revoke(self, token: str) -> None:
        """Revoke the whole grant: its access and refresh tokens (RFC 7009 §2.1)."""
        with self.store.tx() as db:
            row = db.get_oauth_token(hash_token(token))
            if row is None:
                return
            db.delete_oauth_grant(row["grant_id"])
        self.audit.log(kind="oauth", event="token_revoke", client_id=row["client_id"], member=row["member"])

    # -- the auth middleware's check -------------------------------------------------------

    def authenticate(self, token: Any) -> str | None:
        """Token → member name: an OAuth access token, or else a fixed member token."""
        if isinstance(token, str) and token.startswith(ACCESS_PREFIX):
            row = self._token_row(token, "access")
            return row["member"] if row is not None else None
        return self.skein.authenticate(token)

    # -- consent page --------------------------------------------------------------------------

    async def consent(self, request: Request) -> Response:
        if request.method == "GET":
            req = request.query_params.get("req", "")
            view = await _run(self._pending_view, req)
            outcome = _Outcome("form", view=view) if view else _Outcome("expired")
            return self._render(outcome, req)
        form = await request.form()
        req = _form_str(form, "req")
        if _form_str(form, "decision") == "deny":
            outcome = await _run(self._deny, req)
        else:
            token = _form_str(form, "token").strip()
            outcome = await _run(self._decide, req, token)
        return self._render(outcome, req)

    def _render(self, outcome: _Outcome, req: str) -> Response:
        if outcome.kind == "redirect":
            return RedirectResponse(outcome.url, status_code=303, headers=PAGE_HEADERS)
        if outcome.kind == "form":
            return self._page(400 if outcome.error else 200, "Connect an app to skein",
                              self._form_html(outcome.view, req, outcome.error),
                              redirect_uri=outcome.view["redirect_uri"])
        if outcome.kind == "locked":
            return self._page(400, "Sign-in stopped", "<p>Too many tokens didn't match, so this sign-in "
                              "was stopped. Start again from your app.</p>")
        if outcome.kind == "conflict":
            where = html.escape(_where(outcome.view["redirect_uri"]))
            return self._page(400, "Sign-in stopped", f"<p>This app is already tied to a different "
                              f"redirect address, so skein won't send a sign-in to {where}. Start again "
                              "from the app, or ask the owner to check <code>skein oauth client list</code>.</p>")
        return self._page(400, "Sign-in expired", "<p>This sign-in request has expired or was already "
                          "used. Start again from your app.</p>")

    def _form_html(self, view: dict[str, Any], req: str, error: str) -> str:
        e = html.escape
        name = view["client_name"] or "An app"
        if view["source"] == OAUTH_CLI:
            who = f"<strong>{e(name)}</strong> (added by the owner of this skein)"
        else:
            who = f"An app calling itself <strong>{e(name)}</strong>"
        alert = f'<p class="error" role="alert">{e(error)}</p>' if error else ""
        return f"""<p>{who} wants to use skein as one of its members.</p>
<p>Paste the skein token of the member it should act as. skein checks it once and does not keep
it; the app gets its own sign-in, which you can end with <code>skein member rotate</code>.</p>
<p>Approving sends you back to <strong>{e(_where(view["redirect_uri"]))}</strong>. Only continue if you
started this sign-in from that app just now.</p>
{alert}
<form method="post" action="{e(self.consent_path)}">
<input type="hidden" name="req" value="{e(req)}">
<label for="token">Member token</label>
<input id="token" name="token" type="password" autocomplete="off" autocapitalize="off" spellcheck="false">
<div class="buttons">
<button type="submit" name="decision" value="approve">Approve</button>
<button type="submit" name="decision" value="deny" class="secondary">Deny</button>
</div>
</form>
<p class="small">App id: <code>{e(view["client_id"])}</code></p>"""

    @staticmethod
    def _page(status: int, title: str, body: str, redirect_uri: str | None = None) -> HTMLResponse:
        # Browsers apply form-action to the redirect that follows the POST, so the form's
        # redirect target is listed too; without it Chrome blocks the hand-back to the app.
        form_action = "'self'"
        source = _csp_source(redirect_uri) if redirect_uri else None
        if source:
            form_action += f" {source}"
        csp = (
            f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{html.escape(title)}</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; background: #f6f6f4; color: #1d1d1b; }}
main {{ max-width: 34rem; margin: 3rem auto; padding: 0 1rem; }}
h1 {{ font-size: 1.4rem; }}
label {{ display: block; font-weight: 600; margin-top: 1rem; }}
input[type=password] {{ width: 100%; box-sizing: border-box; padding: .6rem; font: inherit; margin-top: .3rem; }}
.buttons {{ margin-top: 1rem; display: flex; gap: .5rem; }}
button {{ font: inherit; padding: .55rem 1.1rem; border: 1px solid #1d1d1b; background: #1d1d1b; color: #fff; border-radius: 4px; }}
button.secondary {{ background: transparent; color: #1d1d1b; }}
.error {{ color: #a1260d; font-weight: 600; }}
.small {{ font-size: .85rem; color: #5d5d58; }}
code {{ font-size: .9em; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #1b1b1a; color: #ececea; }}
  button {{ border-color: #ececea; background: #ececea; color: #1b1b1a; }}
  button.secondary {{ color: #ececea; }}
  .error {{ color: #ff9b85; }}
  .small {{ color: #a9a9a3; }}
}}
</style>
</head>
<body><main>
<h1>{html.escape(title)}</h1>
{body}
</main></body>
</html>
"""
        return HTMLResponse(page, status_code=status, headers={**PAGE_HEADERS, "Content-Security-Policy": csp})

    # -- metadata and routes ------------------------------------------------------------------

    def authorization_server_metadata(self) -> OAuthMetadata:
        metadata = build_metadata(
            AnyHttpUrl(self.issuer),
            None,
            ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE]),
            RevocationOptions(enabled=True),
        )
        metadata.token_endpoint_auth_methods_supported = list(AUTH_METHODS[1:]) + ["none"]
        metadata.revocation_endpoint_auth_methods_supported = list(AUTH_METHODS[1:]) + ["none"]
        return metadata

    def protected_resource_metadata(self) -> ProtectedResourceMetadata:
        return ProtectedResourceMetadata(
            resource=AnyHttpUrl(self.resource),
            authorization_servers=[AnyHttpUrl(self.issuer)],
            scopes_supported=[SCOPE],
            resource_name="skein",
        )

    def _metadata_paths(self) -> tuple[list[str], list[str]]:
        """(authorization server paths, protected resource paths): RFC 8414/9728
        path-inserted at the root, and appended to the base path."""
        b = self.base
        server = [
            f"/.well-known/oauth-authorization-server{b}",
            f"/.well-known/openid-configuration{b}",
            f"{b}/.well-known/oauth-authorization-server",
            f"{b}/.well-known/openid-configuration",
        ]
        resource = [
            f"/.well-known/oauth-protected-resource{b}/mcp",
            f"{b}/.well-known/oauth-protected-resource",
            f"{b}/.well-known/oauth-protected-resource/mcp",
        ]
        return list(dict.fromkeys(server)), list(dict.fromkeys(resource))

    def open_paths(self) -> frozenset[str]:
        """Paths the auth middleware lets through without a member token."""
        server, resource = self._metadata_paths()
        endpoints = [f"{self.base}/{p}" for p in ("authorize", "token", "register", "revoke")]
        return frozenset([*server, *resource, *endpoints, self.consent_path])

    def routes(self) -> list[Route]:
        b = self.base
        authenticator = HashedSecretAuthenticator(self)
        token = TokenHandler(self, authenticator)  # type: ignore[arg-type]
        revoke = RevocationHandler(self, authenticator)  # type: ignore[arg-type]
        register = RegistrationHandler(
            self,  # type: ignore[arg-type]
            options=ClientRegistrationOptions(enabled=True, default_scopes=[SCOPE]),
        )
        authorize = AuthorizationHandler(self)  # type: ignore[arg-type]
        as_meta = cors_middleware(MetadataHandler(self.authorization_server_metadata()).handle, ["GET", "OPTIONS"])
        pr_meta = cors_middleware(
            ProtectedResourceMetadataHandler(self.protected_resource_metadata()).handle, ["GET", "OPTIONS"]
        )
        server_paths, resource_paths = self._metadata_paths()
        return [
            *(Route(p, endpoint=as_meta, methods=["GET", "OPTIONS"]) for p in server_paths),
            *(Route(p, endpoint=pr_meta, methods=["GET", "OPTIONS"]) for p in resource_paths),
            Route(
                f"{b}/authorize",
                endpoint=self._audited(_limited(request_response(authorize.handle)), "authorize"),
                methods=["GET", "POST"],
            ),
            Route(
                f"{b}/token",
                endpoint=_cors(self._audited(_limited(request_response(token.handle)), "token"), ["POST", "OPTIONS"]),
                methods=["POST", "OPTIONS"],
            ),
            Route(
                f"{b}/register",
                endpoint=_cors(
                    self._rate_limited(self._audited(_limited(request_response(register.handle)), "register")),
                    ["POST", "OPTIONS"],
                ),
                methods=["POST", "OPTIONS"],
            ),
            Route(
                f"{b}/revoke",
                endpoint=_cors(self._audited(_limited(request_response(revoke.handle)), "revoke"), ["POST", "OPTIONS"]),
                methods=["POST", "OPTIONS"],
            ),
            Route(
                self.consent_path,
                endpoint=RequestBodyLimitMiddleware(request_response(self.consent), CONSENT_BODY_MAX),
                methods=["GET", "POST"],
            ),
        ]

    def _audited(self, app: ASGIApp, route: str) -> ASGIApp:
        return _AuditFailures(app, self.audit, route)

    def _rate_limited(self, app: ASGIApp) -> ASGIApp:
        return _RegisterRateLimit(app, self.audit, self.register_limiter)


class _AuditFailures:
    """Log a failed OAuth endpoint call: the route and status only (never the body)."""

    def __init__(self, app: ASGIApp, audit: Audit, route: str) -> None:
        self.app, self.audit, self.route = app, audit, route

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        status = 0

        async def watch(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, receive, watch)
        if status >= 400 and scope.get("method") != "OPTIONS":
            self.audit.log(kind="oauth", event=f"{self.route}_failed", status=status)


class _RegisterRateLimit:
    """Dynamic registration is open, so it is capped per window for the whole server."""

    def __init__(self, app: ASGIApp, audit: Audit, limiter: RateLimiter) -> None:
        self.app, self.audit, self.limiter = app, audit, limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("method") == "POST":
            ok, retry_after = self.limiter.allow()
            if not ok:
                self.audit.log(kind="oauth", event="register_limited", status=429)
                response = JSONResponse(
                    {"error": "temporarily_unavailable",
                     "error_description": "too many app registrations; try again shortly"},
                    status_code=429,
                    headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _limited(app: ASGIApp) -> ASGIApp:
    return RequestBodyLimitMiddleware(app, DEFAULT_MAX_REQUEST_BODY_SIZE)


def _cors(app: ASGIApp, methods: list[str]) -> ASGIApp:
    """CORS for endpoints a browser-based client calls directly (as the SDK's routes do)."""
    return CORSMiddleware(app=app, allow_origins="*", allow_methods=methods, allow_headers=[MCP_PROTOCOL_VERSION_HEADER])
