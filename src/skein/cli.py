"""The `skein` command: manage members, the point, and the server (PLAN.md C6).

argparse and stdlib only. ``main(argv)`` returns the exit code so tests can call
it directly. ``serve`` imports the app and uvicorn lazily.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from skein.config import Config, load_config
from skein.errors import SkeinError
from skein.service import Skein

TOKEN_WARNING = "Warning: this token will not be shown again. Store it now."


def _open(cfg: Config) -> Skein:
    return Skein(cfg.db)


def _fmt_members(members: list[dict[str, Any]], point: str | None) -> str:
    if not members:
        return "No members yet. Add one with: skein member add NAME"
    lines = [f"  {'NAME':<16} {'DISPLAY':<16} {'DISABLED':<9} {'LAST SEEN':<22} OPEN"]
    for m in members:
        marker = "*" if m["name"] == point else " "
        lines.append(
            f"{marker} {m['name']:<16} {m['display']:<16} "
            f"{'yes' if m['disabled'] else 'no':<9} "
            f"{m['last_seen'] or 'never':<22} {m['open_tasks']}"
        )
    lines.append("(* = point)")
    return "\n".join(lines)


def _cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    _open(cfg)
    print(f"Initialized database at {cfg.db}")
    return 0


def _cmd_member_add(cfg: Config, args: argparse.Namespace) -> int:
    result = _open(cfg).add_member(args.name, args.display or "")
    print(f"Added member {result['name']}.")
    print(result["token"])
    print(TOKEN_WARNING)
    return 0


def _cmd_member_list(cfg: Config, args: argparse.Namespace) -> int:
    svc = _open(cfg)
    print(_fmt_members(svc.list_members(), svc.get_point()))
    return 0


def _cmd_member_rotate(cfg: Config, args: argparse.Namespace) -> int:
    result = _open(cfg).rotate_token(args.name)
    print(f"Rotated token for {result['name']}. The old token no longer works.")
    print(result["token"])
    print(TOKEN_WARNING)
    return 0


def _cmd_member_toggle(cfg: Config, args: argparse.Namespace) -> int:
    disabled = args.member_cmd == "disable"
    result = _open(cfg).set_disabled(args.name, disabled)
    print(f"{result['name']} {'disabled' if result['disabled'] else 'enabled'}.")
    return 0


def _cmd_point_set(cfg: Config, args: argparse.Namespace) -> int:
    result = _open(cfg).set_point(args.name)
    print(f"Point set to {result['point']}.")
    return 0


def _cmd_point_show(cfg: Config, args: argparse.Namespace) -> int:
    point = _open(cfg).get_point()
    print(point if point else "No point set.")
    return 0


def _cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    s = _open(cfg).status()
    print(f"Point: {s['point'] or 'none set'}")
    print(f"Open tasks: {s['open_tasks']}")
    print(f"Open asks: {s['open_asks']}")
    print(_fmt_members(s["members"], s["point"]))
    return 0


def _cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    import uvicorn

    from skein.app import create_app

    # No uvicorn access log, ever: it prints request lines with their query string, which
    # can carry a ?token=. skein's own audit log is the record of requests.
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, access_log=False)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skein", description="Manage the skein queue server.")
    parser.add_argument("--config", metavar="PATH", help="config file (default: $SKEIN_CONFIG or ./skein.toml)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("init", help="create the database").set_defaults(func=_cmd_init)

    member = sub.add_parser("member", help="manage members").add_subparsers(
        dest="member_cmd", required=True, metavar="ACTION"
    )
    add = member.add_parser("add", help="add a member and print its token")
    add.add_argument("name")
    add.add_argument("--display", default="", help="display name")
    add.set_defaults(func=_cmd_member_add)
    member.add_parser("list", help="list members").set_defaults(func=_cmd_member_list)
    rotate = member.add_parser("rotate", help="replace a member's token")
    rotate.add_argument("name")
    rotate.set_defaults(func=_cmd_member_rotate)
    for action in ("disable", "enable"):
        p = member.add_parser(action, help=f"{action} a member")
        p.add_argument("name")
        p.set_defaults(func=_cmd_member_toggle)

    point = sub.add_parser("point", help="manage the point").add_subparsers(
        dest="point_cmd", required=True, metavar="ACTION"
    )
    pset = point.add_parser("set", help="make a member the point")
    pset.add_argument("name")
    pset.set_defaults(func=_cmd_point_set)
    point.add_parser("show", help="show the point").set_defaults(func=_cmd_point_show)

    sub.add_parser("status", help="show the point, members and open counts").set_defaults(func=_cmd_status)
    sub.add_parser("serve", help="run the server").set_defaults(func=_cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        return args.func(cfg, args)
    except SkeinError as exc:
        print(f"skein: {exc.message}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"skein: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
