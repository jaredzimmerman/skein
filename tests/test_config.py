"""Tests for the TOML config loader (PLAN.md C3)."""

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


def test_defaults_when_no_file_exists(tmp_path):
    cfg = load_config()
    assert cfg == Config()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8771
    assert cfg.base_path == ""
    assert cfg.public_hosts == ()
    assert cfg.url_tokens is False
    assert cfg.db == "./skein.db"
    assert cfg.audit_log == ""
    assert cfg.owner == "the user"
    assert cfg.answer_time == "within about an hour"


def test_missing_explicit_path_gives_defaults(tmp_path):
    assert load_config(str(tmp_path / "nope.toml")) == Config()


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


def test_env_pointing_at_missing_file_gives_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("SKEIN_CONFIG", str(tmp_path / "missing.toml"))
    assert load_config() == Config()


def test_every_key_can_be_overridden(tmp_path):
    path = tmp_path / "full.toml"
    path.write_text(FULL_TOML, encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
    assert cfg.base_path == "/skein"
    assert cfg.public_hosts == ("skein.example.com", "other.example.com")
    assert cfg.url_tokens is True
    assert cfg.db == "/var/lib/skein/skein.db"
    assert cfg.audit_log == "/var/log/skein/audit.jsonl"
    assert cfg.owner == "Sam"
    assert cfg.answer_time == "within a day"


def test_public_hosts_is_a_tuple(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text('[server]\npublic_hosts = ["a.example.com"]\n', encoding="utf-8")
    assert isinstance(load_config(str(path)).public_hosts, tuple)


def test_config_is_frozen(tmp_path):
    cfg = load_config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.port = 1  # type: ignore[misc]


def test_partial_file_keeps_other_defaults(tmp_path):
    path = tmp_path / "partial.toml"
    path.write_text('[server]\nport = 9999\n', encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.port == 9999
    assert cfg.host == "127.0.0.1"
    assert cfg.db == "./skein.db"


def test_unknown_section_raises(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('[bogus]\nx = 1\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(path))


def test_unknown_key_in_known_section_raises(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('[server]\nprot = 9000\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(path))


def test_unknown_top_level_key_raises(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('stray = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(path))


@pytest.mark.parametrize("bad", ["skein", "/skein/", "/", "//skein", "skein/"])
def test_bad_base_path_raises(tmp_path, bad):
    path = tmp_path / "bp.toml"
    path.write_text(f'[server]\nbase_path = "{bad}"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(path))


@pytest.mark.parametrize("good", ["", "/skein", "/a/b"])
def test_good_base_path_accepted(tmp_path, good):
    path = tmp_path / "bp.toml"
    path.write_text(f'[server]\nbase_path = "{good}"\n', encoding="utf-8")
    assert load_config(str(path)).base_path == good


def test_example_file_loads_to_defaults():
    assert EXAMPLE.is_file()
    assert load_config(str(EXAMPLE)) == Config()
