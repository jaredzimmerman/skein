"""Tests for the `skein` command (PLAN.md C6). Calls main() directly."""

from __future__ import annotations

from pathlib import Path

import pytest

from skein.cli import main
from skein.service import Skein


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A config file pointing at a temp database. Isolated from any env config."""
    monkeypatch.delenv("SKEIN_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "skein.db"
    path = tmp_path / "skein.toml"
    path.write_text(f'[storage]\ndb = "{db.as_posix()}"\n', encoding="utf-8")
    return path


def run(cfg, capsys, *args):
    code = main(["--config", str(cfg), *args])
    out, err = capsys.readouterr()
    return code, out, err


def token_from(out: str) -> str:
    tokens = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("skein_")]
    assert len(tokens) == 1, out
    return tokens[0]


def service(cfg) -> Skein:
    from skein.config import load_config

    return Skein(load_config(str(cfg)).db)


def test_init_creates_db(cfg, capsys):
    code, out, err = run(cfg, capsys, "init")
    assert code == 0
    assert err == ""
    from skein.config import load_config

    assert Path(load_config(str(cfg)).db).exists()


def test_add_prints_token_and_warning(cfg, capsys):
    run(cfg, capsys, "init")
    code, out, err = run(cfg, capsys, "member", "add", "sam", "--display", "Sam")
    assert code == 0
    assert token_from(out).startswith("skein_")
    assert "not be shown again" in out


def test_duplicate_add_exits_1(cfg, capsys):
    run(cfg, capsys, "init")
    run(cfg, capsys, "member", "add", "sam")
    code, out, err = run(cfg, capsys, "member", "add", "sam")
    assert code == 1
    assert err.strip() != ""
    assert "Traceback" not in err


def test_list_shows_members(cfg, capsys):
    run(cfg, capsys, "init")
    run(cfg, capsys, "member", "add", "sam", "--display", "Sam")
    run(cfg, capsys, "member", "add", "muse", "--display", "Muse")
    code, out, err = run(cfg, capsys, "member", "list")
    assert code == 0
    assert "sam" in out and "Sam" in out
    assert "muse" in out and "Muse" in out


def test_rotate_changes_token_and_old_one_stops_working(cfg, capsys):
    run(cfg, capsys, "init")
    _, out, _ = run(cfg, capsys, "member", "add", "sam")
    old = token_from(out)
    assert service(cfg).authenticate(old) == "sam"

    code, out, err = run(cfg, capsys, "member", "rotate", "sam")
    assert code == 0
    new = token_from(out)
    assert new != old
    assert service(cfg).authenticate(old) is None
    assert service(cfg).authenticate(new) == "sam"


def test_disable_and_enable(cfg, capsys):
    run(cfg, capsys, "init")
    _, out, _ = run(cfg, capsys, "member", "add", "sam")
    tok = token_from(out)

    assert run(cfg, capsys, "member", "disable", "sam")[0] == 0
    assert service(cfg).authenticate(tok) is None
    _, out, _ = run(cfg, capsys, "member", "list")
    row = next(ln for ln in out.splitlines() if " sam " in ln)
    assert " yes " in row

    assert run(cfg, capsys, "member", "enable", "sam")[0] == 0
    _, out, _ = run(cfg, capsys, "member", "list")
    row = next(ln for ln in out.splitlines() if " sam " in ln)
    assert " no " in row
    assert service(cfg).authenticate(tok) == "sam"


def test_point_set_and_show(cfg, capsys):
    run(cfg, capsys, "init")
    run(cfg, capsys, "member", "add", "sam")
    code, out, err = run(cfg, capsys, "point", "set", "sam")
    assert code == 0
    code, out, err = run(cfg, capsys, "point", "show")
    assert code == 0
    assert out.strip() == "sam"


def test_point_set_unknown_member_exits_1(cfg, capsys):
    run(cfg, capsys, "init")
    code, out, err = run(cfg, capsys, "point", "set", "ghost")
    assert code == 1
    assert err.strip() != ""
    assert service(cfg).get_point() is None


def test_status_contains_point(cfg, capsys):
    run(cfg, capsys, "init")
    run(cfg, capsys, "member", "add", "sam")
    run(cfg, capsys, "point", "set", "sam")
    code, out, err = run(cfg, capsys, "status")
    assert code == 0
    assert "sam" in out
    assert "point" in out.lower()


def test_bad_config_exits_1(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SKEIN_CONFIG", raising=False)
    bad = tmp_path / "bad.toml"
    bad.write_text("[nope]\nx = 1\n", encoding="utf-8")
    code, out, err = run(bad, capsys, "status")
    assert code == 1
    assert "Traceback" not in err


@pytest.mark.parametrize("server", ["", "[server]\nurl_tokens = true\n"])
def test_serve_runs_uvicorn_without_access_log(tmp_path, capsys, monkeypatch, server):
    """uvicorn's access log prints query strings (and so ?token=); skein's audit log is the record."""
    import uvicorn

    monkeypatch.delenv("SKEIN_CONFIG", raising=False)
    path = tmp_path / "serve.toml"
    path.write_text(server + f'[storage]\ndb = "{(tmp_path / "skein.db").as_posix()}"\n', encoding="utf-8")
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen["app"] = app
        seen.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    code, out, err = run(path, capsys, "serve")
    assert code == 0
    assert seen["access_log"] is False
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 8771)
    assert callable(seen["app"])


def test_missing_explicit_config_is_one_line_error(tmp_path, capsys):
    from skein.cli import main
    missing = tmp_path / "nope.toml"
    assert main(["--config", str(missing), "status"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("skein: config file not found")
    assert "Traceback" not in err
