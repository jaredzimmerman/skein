"""Tests for the TOML config loader (PLAN.md C3, T08 strict types)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from skein.config import Config, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "skein.example.toml"

FULL_TOML = """
[server]
host = "0.0.0.0"
port = 9000
base_path = "/skein"
public_hosts = ["skein.example.com", "other.example.com"]
allowed_origins = ["https://connector.example.com"]
url_tokens = true

[storage]
db = "/var/lib/skein/skein.db"
audit_log = "/var/log/skein/audit.jsonl"

[instance]
owner = "Sam"
answer_time = "within a day"
"""


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Run every test in an empty cwd with no SKEIN_CONFIG set."""
    monkeypatch.delenv("SKEIN_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(tmp_path: Path, body: str, name: str = "cfg.toml") -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_defaults_when_no_file_exists(tmp_path):
    cfg = load_config()
    assert cfg == Config()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8771
    assert cfg.base_path == ""
    assert cfg.public_hosts == ()
    assert cfg.allowed_origins == ()
    assert cfg.url_tokens is False
    assert cfg.db == "./skein.db"
    assert cfg.audit_log == ""
    assert cfg.owner == "the user"
    assert cfg.answer_time == "within about an hour"


def test_missing_explicit_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "nope.toml"))


def test_env_pointing_at_missing_file_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("SKEIN_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(FileNotFoundError):
        load_config()


def test_cwd_skein_toml_is_used_when_no_arg_or_env(tmp_path):
    (tmp_path / "skein.toml").write_text('[server]\nport = 9100\n', encoding="utf-8")
    assert load_config().port == 9100


def test_env_skein_config_is_used(monkeypatch, tmp_path):
    cfg_file = tmp_path / "env.toml"
    cfg_file.write_text('[instance]\nowner = "Sam"\n', encoding="utf-8")
    monkeypatch.setenv("SKEIN_CONFIG", str(cfg_file))
    assert load_config().owner == "Sam"


def test_explicit_path_wins_over_env(monkeypatch, tmp_path):
    env_file = tmp_path / "env.toml"
    env_file.write_text('[server]\nport = 9001\n', encoding="utf-8")
    arg_file = tmp_path / "arg.toml"
    arg_file.write_text('[server]\nport = 9002\n', encoding="utf-8")
    monkeypatch.setenv("SKEIN_CONFIG", str(env_file))
    assert load_config(str(arg_file)).port == 9002


def test_explicit_arg_missing_wins_over_existing_env(monkeypatch, tmp_path):
    env_file = tmp_path / "env.toml"
    env_file.write_text('[server]\nport = 9001\n', encoding="utf-8")
    monkeypatch.setenv("SKEIN_CONFIG", str(env_file))
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "typo.toml"))


def test_every_key_can_be_overridden(tmp_path):
    cfg = load_config(_write(tmp_path, FULL_TOML, "full.toml"))
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
    assert cfg.base_path == "/skein"
    assert cfg.public_hosts == ("skein.example.com", "other.example.com")
    assert cfg.allowed_origins == ("https://connector.example.com",)
    assert cfg.url_tokens is True
    assert cfg.db == "/var/lib/skein/skein.db"
    assert cfg.audit_log == "/var/log/skein/audit.jsonl"
    assert cfg.owner == "Sam"
    assert cfg.answer_time == "within a day"


def test_public_hosts_is_a_tuple(tmp_path):
    path = _write(tmp_path, '[server]\npublic_hosts = ["a.example.com"]\n', "hosts.toml")
    assert isinstance(load_config(path).public_hosts, tuple)


def test_allowed_origins_is_a_tuple(tmp_path):
    path = _write(tmp_path, '[server]\nallowed_origins = ["https://a.example.com"]\n', "o.toml")
    assert load_config(path).allowed_origins == ("https://a.example.com",)


def test_allowed_origins_default_is_empty_tuple(tmp_path):
    path = _write(tmp_path, "[server]\nport = 9999\n", "p.toml")
    assert load_config(path).allowed_origins == ()


def test_config_is_frozen(tmp_path):
    cfg = load_config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.port = 1  # type: ignore[misc]


def test_partial_file_keeps_other_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, "[server]\nport = 9999\n", "partial.toml"))
    assert cfg.port == 9999
    assert cfg.host == "127.0.0.1"
    assert cfg.db == "./skein.db"


def test_unknown_section_raises(tmp_path):
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, "[bogus]\nx = 1\n", "bad.toml"))


def test_unknown_key_in_known_section_raises(tmp_path):
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, "[server]\nprot = 9000\n", "bad.toml"))


def test_unknown_top_level_key_raises(tmp_path):
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, 'stray = "x"\n', "bad.toml"))


# --- base_path -------------------------------------------------------------

@pytest.mark.parametrize("bad", ["skein", "/skein/", "/", "skein/", "//", "//skein"])
def test_bad_base_path_raises(tmp_path, bad):
    path = _write(tmp_path, f'[server]\nbase_path = "{bad}"\n', "bp.toml")
    with pytest.raises(ValueError):
        load_config(path)


def test_base_path_double_slash_names_the_key(tmp_path):
    path = _write(tmp_path, '[server]\nbase_path = "//skein"\n', "bp.toml")
    with pytest.raises(ValueError, match="base_path"):
        load_config(path)


@pytest.mark.parametrize("good", ["", "/skein", "/a/b"])
def test_good_base_path_accepted(tmp_path, good):
    path = _write(tmp_path, f'[server]\nbase_path = "{good}"\n', "bp.toml")
    assert load_config(path).base_path == good


# --- url_tokens: bool only -------------------------------------------------

def test_url_tokens_string_false_raises(tmp_path):
    path = _write(tmp_path, '[server]\nurl_tokens = "false"\n', "ut.toml")
    with pytest.raises(ValueError, match="url_tokens"):
        load_config(path)


@pytest.mark.parametrize("bad", ['"true"', "0", "1", "[]"])
def test_url_tokens_non_bool_raises(tmp_path, bad):
    path = _write(tmp_path, f"[server]\nurl_tokens = {bad}\n", "ut.toml")
    with pytest.raises(ValueError, match="url_tokens"):
        load_config(path)


@pytest.mark.parametrize("good, expected", [("true", True), ("false", False)])
def test_url_tokens_bool_accepted(tmp_path, good, expected):
    path = _write(tmp_path, f"[server]\nurl_tokens = {good}\n", "ut.toml")
    assert load_config(path).url_tokens is expected


# --- port: int in 1..65535, not bool --------------------------------------

@pytest.mark.parametrize("bad", ['"9000"', "true", "9000.0", "[9000]"])
def test_port_wrong_type_raises(tmp_path, bad):
    path = _write(tmp_path, f"[server]\nport = {bad}\n", "port.toml")
    with pytest.raises(ValueError, match="port"):
        load_config(path)


@pytest.mark.parametrize("bad", [0, -1, 65536, 100000])
def test_port_out_of_range_raises(tmp_path, bad):
    path = _write(tmp_path, f"[server]\nport = {bad}\n", "port.toml")
    with pytest.raises(ValueError, match="port"):
        load_config(path)


@pytest.mark.parametrize("good", [1, 8771, 65535])
def test_port_in_range_accepted(tmp_path, good):
    path = _write(tmp_path, f"[server]\nport = {good}\n", "port.toml")
    assert load_config(path).port == good


# --- str keys --------------------------------------------------------------

STR_KEYS = [
    ("server", "host"),
    ("server", "base_path"),
    ("storage", "db"),
    ("storage", "audit_log"),
    ("instance", "owner"),
    ("instance", "answer_time"),
]


@pytest.mark.parametrize("section, key", STR_KEYS)
@pytest.mark.parametrize("bad", ["5", "true", "[\"x\"]"])
def test_str_keys_reject_non_strings(tmp_path, section, key, bad):
    path = _write(tmp_path, f"[{section}]\n{key} = {bad}\n", "s.toml")
    with pytest.raises(ValueError, match=key):
        load_config(path)


# --- list keys -------------------------------------------------------------

@pytest.mark.parametrize("key", ["public_hosts", "allowed_origins"])
@pytest.mark.parametrize("bad", ['"a.example.com"', "[1]", '["ok", 2]', "true"])
def test_list_keys_reject_wrong_shape(tmp_path, key, bad):
    path = _write(tmp_path, f"[server]\n{key} = {bad}\n", "l.toml")
    with pytest.raises(ValueError, match=key):
        load_config(path)


# --- example file ----------------------------------------------------------

def test_example_file_loads_to_defaults():
    assert EXAMPLE.is_file()
    assert load_config(str(EXAMPLE)) == Config()


def test_example_file_documents_allowed_origins():
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "allowed_origins = []" in text
    lines = text.splitlines()
    idx = lines.index("allowed_origins = []")
    comment = "\n".join(lines[max(0, idx - 4):idx])
    assert "Origin" in comment
    assert "403" in comment
