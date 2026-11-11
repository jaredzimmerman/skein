"""TOML configuration loader for skein (PLAN.md C3).

Path order: explicit argument, then the SKEIN_CONFIG environment variable, then
./skein.toml. Only the implicit ./skein.toml may be missing (then all defaults
apply). A missing explicit path raises FileNotFoundError so typos fail loudly.
Unknown sections or keys raise ValueError, and every value is type-checked
against its key so a wrong type never silently changes behaviour.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

ENV_VAR = "SKEIN_CONFIG"
DEFAULT_FILE = "./skein.toml"


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8771
    base_path: str = ""
    public_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    url_tokens: bool = False
    db: str = "./skein.db"
    audit_log: str = ""
    owner: str = "the user"
    answer_time: str = "within about an hour"
    public_url: str = ""
    oauth_enabled: bool = False
    oauth_access_token_ttl: int = 3600
    oauth_refresh_token_ttl: int = 2592000

    def __post_init__(self) -> None:
        # Cross-key checks run however the Config is built (loader or code).
        _validate_public_url(self.public_url, self.base_path)
        if self.oauth_enabled and not self.public_url:
            raise ValueError(
                'oauth.enabled = true needs server.public_url (e.g. "https://skein.example.com/skein")'
            )


# TOML section -> {TOML key: Config field name}
_SCHEMA: dict[str, dict[str, str]] = {
    "server": {
        "host": "host",
        "port": "port",
        "base_path": "base_path",
        "public_hosts": "public_hosts",
        "allowed_origins": "allowed_origins",
        "url_tokens": "url_tokens",
        "public_url": "public_url",
    },
    "storage": {
        "db": "db",
        "audit_log": "audit_log",
    },
    "instance": {
        "owner": "owner",
        "answer_time": "answer_time",
    },
    "oauth": {
        "enabled": "oauth_enabled",
        "access_token_ttl": "oauth_access_token_ttl",
        "refresh_token_ttl": "oauth_refresh_token_ttl",
    },
}

BOOL_KEYS = frozenset({"url_tokens", "enabled"})
TTL_KEYS = frozenset({"access_token_ttl", "refresh_token_ttl"})
TTL_MIN = 60
TTL_MAX = 365 * 24 * 3600
# Hosts that may use plain http in public_url (the MCP SDK's issuer rule).
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _validate_base_path(value: str) -> str:
    if value == "":
        return value
    if not value.startswith("/") or value.endswith("/") or value == "/":
        raise ValueError(
            f'server.base_path must be "" or start with "/" and not end with "/": {value!r}'
        )
    if value.startswith("//"):
        raise ValueError(f'server.base_path must not start with "//": {value!r}')
    return value


def _validate_public_url(value: str, base_path: str) -> str:
    """``public_url`` is "" or the absolute URL clients use, ending in ``base_path``."""
    if value == "":
        return value
    example = '(e.g. "https://skein.example.com/skein")'
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        raise ValueError(f"server.public_url is not a URL {example}: {value!r}") from None
    if parts.scheme not in ("https", "http") or not host:
        raise ValueError(f"server.public_url must be an http(s) URL {example}: {value!r}")
    if parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ValueError(f"server.public_url must use https (http only for localhost): {value!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"server.public_url must not contain credentials: {value!r}")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise ValueError(f"server.public_url must not have a query or fragment: {value!r}")
    if value.endswith("/"):
        raise ValueError(f'server.public_url must not end with "/": {value!r}')
    if parts.path != base_path:
        raise ValueError(
            f"server.public_url must end with server.base_path ({base_path or 'empty'}); "
            f"its path is {parts.path or 'empty'}: {value!r}"
        )
    return value


def _check_value(section: str, key: str, value: Any) -> Any:
    """Type-check one value by its key. Raises ValueError naming section.key."""
    where = f"{section}.{key}"

    if key == "port":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where} must be an integer, got {value!r}")
        if not 1 <= value <= 65535:
            raise ValueError(f"{where} must be between 1 and 65535, got {value!r}")
        return value

    if key in BOOL_KEYS:
        if not isinstance(value, bool):
            raise ValueError(f"{where} must be true or false, got {value!r}")
        return value

    if key in TTL_KEYS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where} must be a whole number of seconds, got {value!r}")
        if not TTL_MIN <= value <= TTL_MAX:
            raise ValueError(f"{where} must be between {TTL_MIN} and {TTL_MAX} seconds, got {value!r}")
        return value

    if key in ("public_hosts", "allowed_origins"):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{where} must be a list of strings, got {value!r}")
        return tuple(value)

    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string, got {value!r}")
    return value


def _build(data: dict[str, Any]) -> Config:
    unknown_top = set(data) - set(_SCHEMA)
    if unknown_top:
        raise ValueError(f"unknown config section(s): {sorted(unknown_top)}")

    values: dict[str, Any] = {}
    for section, keys in _SCHEMA.items():
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise ValueError(f"config section [{section}] must be a table")
        unknown_keys = set(table) - set(keys)
        if unknown_keys:
            raise ValueError(f"unknown key(s) in [{section}]: {sorted(unknown_keys)}")
        for toml_key, field_name in keys.items():
            if toml_key in table:
                values[field_name] = _check_value(section, toml_key, table[toml_key])

    if "base_path" in values:
        values["base_path"] = _validate_base_path(values["base_path"])

    return Config(**values)


def load_config(path: str | None = None) -> Config:
    """Load configuration from TOML.

    `path` wins over the SKEIN_CONFIG environment variable, which wins over
    ./skein.toml. An explicit path (argument or SKEIN_CONFIG) that does not
    exist raises FileNotFoundError. Only the implicit ./skein.toml may be
    missing, in which case the defaults are returned.
    """
    if path is None:
        env_path = os.environ.get(ENV_VAR, "")
        if not env_path:
            if not os.path.isfile(DEFAULT_FILE):
                return _build({})
            env_path = DEFAULT_FILE
        path = env_path

    if not os.path.isfile(path):
        raise FileNotFoundError(f"config file not found: {path}")

    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return _build(data)
