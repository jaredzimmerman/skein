"""TOML configuration loader for skein (PLAN.md C3).

Path order: explicit argument, then the SKEIN_CONFIG environment variable, then
./skein.toml. A missing file means all defaults. Unknown sections or keys raise
ValueError so typos fail loudly instead of being ignored.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from typing import Any

ENV_VAR = "SKEIN_CONFIG"
DEFAULT_FILE = "./skein.toml"


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8771
    base_path: str = ""
    public_hosts: tuple[str, ...] = ()
    url_tokens: bool = False
    db: str = "./skein.db"
    audit_log: str = ""
    owner: str = "the user"
    answer_time: str = "within about an hour"


# TOML section -> {TOML key: Config field name}
_SCHEMA: dict[str, dict[str, str]] = {
    "server": {
        "host": "host",
        "port": "port",
        "base_path": "base_path",
        "public_hosts": "public_hosts",
        "url_tokens": "url_tokens",
    },
    "storage": {
        "db": "db",
        "audit_log": "audit_log",
    },
    "instance": {
        "owner": "owner",
        "answer_time": "answer_time",
    },
}


def _validate_base_path(value: str) -> str:
    if value == "":
        return value
    if not value.startswith("/") or value.endswith("/") or value == "/":
        raise ValueError(
            f'server.base_path must be "" or start with "/" and not end with "/": {value!r}'
        )
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
                values[field_name] = table[toml_key]

    if "public_hosts" in values:
        hosts = values["public_hosts"]
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise ValueError("server.public_hosts must be a list of strings")
        values["public_hosts"] = tuple(hosts)

    if "base_path" in values:
        values["base_path"] = _validate_base_path(values["base_path"])

    return Config(**values)


def load_config(path: str | None = None) -> Config:
    """Load configuration from TOML.

    `path` wins over the SKEIN_CONFIG environment variable, which wins over
    ./skein.toml. A missing file yields the defaults.
    """
    if path is None:
        env_path = os.environ.get(ENV_VAR, "")
        path = env_path or DEFAULT_FILE

    if not os.path.isfile(path):
        return _build({})

    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return _build(data)
