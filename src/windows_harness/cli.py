"""One Python execution surface for Windows apps, files, and PowerShell."""

from __future__ import annotations

import argparse
import code
import json
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from .capture import HarnessError
from .windows import Windows


def _namespace() -> dict[str, Any]:
    return {
        "__name__": "__windows_harness__",
        "win": Windows(),
        "Path": Path,
        "subprocess": subprocess,
    }


def _execute(source: str) -> int:
    if not source.strip():
        print("No Python code received on stdin", file=sys.stderr)
        return 2
    namespace = _namespace()
    exec(compile(source, "<windows-harness>", "exec"), namespace, namespace)  # noqa: S102
    return 0


def _tame_stdio() -> None:
    """Agents pipe UTF-8 both ways; a lone invalid byte or a cp936 console
    must not mangle program source or Chinese control names."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _skill_text() -> str:
    bundled = resources.files("windows_harness").joinpath("SKILL.md")
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    checkout = Path(__file__).resolve().parents[2] / "skills/windows-harness/SKILL.md"
    return checkout.read_text(encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="windows-harness",
        description="Execute Python with Windows app control and filesystem access.",
        epilog=(
            "Typical usage:\n  windows-harness <<'PY'\n  print(win.see('Notepad'))\n  PY"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="check the interactive desktop and runtime")
    subparsers.add_parser("apps", help="list processes that own windows")
    subparsers.add_parser("repl", help="start a persistent interactive Python session")
    subparsers.add_parser("skill", help="print the Windows Harness skill")
    see = subparsers.add_parser("see", help="capture a bounded application window")
    see.add_argument("app")
    see.add_argument("--max-width", type=int, default=1280)
    see.add_argument("--max-height", type=int, default=1280)
    see.add_argument("--no-pointer", action="store_true")
    state = subparsers.add_parser("state", help="print an application's UIA state as JSON")
    state.add_argument("app")
    state.add_argument("--screenshot", action="store_true")
    state.add_argument("--max-depth", type=int, default=12)
    state.add_argument("--max-nodes", type=int, default=1500)
    return parser


def main(argv: list[str] | None = None) -> int:
    _tame_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            print(json.dumps(Windows().doctor(), indent=2))
            return 0
        if args.command == "apps":
            print(json.dumps(Windows().list_apps(), indent=2))
            return 0
        if args.command == "skill":
            print(_skill_text(), end="")
            return 0
        if args.command == "repl":
            code.interact(
                banner=(
                    "windows-harness: win.see/key/type/click/ax/script, "
                    "Path, and subprocess are ready"
                ),
                local=_namespace(),
                exitmsg="",
            )
            return 0
        if args.command == "see":
            result = Windows().see(
                args.app,
                max_width=args.max_width,
                max_height=args.max_height,
                show_pointer=not args.no_pointer,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command == "state":
            state = Windows().get_app_state(
                args.app,
                screenshot=args.screenshot,
                max_depth=args.max_depth,
                max_nodes=args.max_nodes,
            )
            print(json.dumps(state, indent=2, ensure_ascii=False, default=str))
            return 0
        if args.command is None:
            if sys.stdin.isatty():
                parser.print_help()
                return 2
            return _execute(sys.stdin.read())
        parser.error(f"unknown command: {args.command}")
    except (HarnessError, RuntimeError) as exc:
        print(f"windows-harness: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
