"""One Python execution surface for Windows apps, files, and PowerShell."""

from __future__ import annotations

import argparse
import code
import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from .capture import HarnessError
from .delivery import scripts_dir
from .windows import Windows


def _namespace() -> dict[str, Any]:
    return {
        "__name__": "__windows_harness__",
        "win": Windows(),
        "Path": Path,
        "subprocess": subprocess,
    }


def _execute(source: str) -> int:
    source = source.lstrip(chr(0xFEFF))  # a piped BOM must not reach compile()
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
    """Return the bundled SKILL.md (single source of truth; ships in the wheel)."""
    bundled = resources.files("windows_harness").joinpath("SKILL.md")
    if not bundled.is_file():
        raise FileNotFoundError(
            "Bundled SKILL.md resource is missing; the windows-harness package is "
            "broken. Reinstall with `uv tool install --reinstall windows-harness` "
            "or `pip install --force-reinstall windows-harness`."
        )
    return bundled.read_text(encoding="utf-8")


def _install_skill(target: str | None = None) -> int:
    """Write SKILL.md and agents metadata into the user's skills directory.

    Writes the open ``~/.agents/skills`` location, then mirrors into
    ``~/.codex/skills`` (Codex), ``~/.claude/skills`` (Claude Code), and
    ``~/.cursor/skills`` (Cursor) so every agent discovers the skill. A custom
    ``target`` installs only to that one directory.
    """
    skill_name = "windows-harness"
    agents_home = Path.home() / ".agents" / "skills" / skill_name
    codex_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ) / "skills" / skill_name
    claude_home = Path.home() / ".claude" / "skills" / skill_name
    cursor_home = Path.home() / ".cursor" / "skills" / skill_name

    if target:
        destinations = [Path(target).expanduser()]
    else:
        destinations = [agents_home, codex_home, claude_home, cursor_home]

    files: list[tuple[str, str]] = [
        ("SKILL.md", _skill_text()),
        ("agents/openai.yaml", _agent_meta_text()),
    ]
    for dest in destinations:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "agents").mkdir(exist_ok=True)
        for rel, text in files:
            (dest / rel).write_text(text, encoding="utf-8")
        print(f"installed skill -> {dest}")
    return 0


def _agent_meta_text() -> str:
    """Return the bundled agents/openai.yaml (ships in the wheel)."""
    bundled = resources.files("windows_harness").joinpath("agents/openai.yaml")
    if not bundled.is_file():
        raise FileNotFoundError(
            "Bundled agents/openai.yaml resource is missing; the windows-harness "
            "package is broken. Reinstall with `uv tool install --reinstall "
            "windows-harness` or `pip install --force-reinstall windows-harness`."
        )
    return bundled.read_text(encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="windows-harness",
        description="Execute Python with Windows app control and filesystem access.",
        epilog=(
            "Typical usage (identical in PowerShell, cmd, and bash):\n"
            "  windows-harness see 'Notepad'\n"
            "  windows-harness apps\n"
            "  windows-harness run task.py        # win, Path, subprocess preloaded\n"
            "  windows-harness exec \"print(len(win.list_apps()))\"\n"
            "\n"
            "Note: '<<' heredocs are bash-only; write a .py file and use run instead."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="check the interactive desktop and runtime")
    apps_cmd = subparsers.add_parser(
        "apps", help="list app processes that own windows (compact JSON)"
    )
    apps_cmd.add_argument(
        "--all", action="store_true",
        help="include system plumbing (IME helpers, tool windows, untitled frames)",
    )
    subparsers.add_parser("repl", help="start a persistent interactive Python session")
    execute = subparsers.add_parser(
        "exec", help="execute one Python snippet with win ready (argv-only)"
    )
    execute.add_argument("code", help="Python source; win, Path, subprocess are preloaded")
    runner = subparsers.add_parser(
        "run", help="execute a Python script file with win ready"
    )
    runner.add_argument("script", help="path to a .py file; win, Path, subprocess are preloaded")
    subparsers.add_parser("skill", help="print the Windows Harness skill")
    install = subparsers.add_parser("install-skill", help="install the skill into your agent skills directory")
    install.add_argument("--target", help="custom skills directory (e.g. ~/.claude/skills/windows-harness)")
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
            # Compact JSON: one line per process keeps the inventory inside
            # agent output limits; indent=2 tripled the token cost.
            print(json.dumps(Windows().list_apps(include_system=args.all), ensure_ascii=False))
            return 0
        if args.command == "install-skill":
            return _install_skill(getattr(args, "target", None))
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
        if args.command == "exec":
            return _execute(args.code)
        if args.command == "run":
            # utf-8-sig: a BOM left by PowerShell editors must not crash the run.
            script = Path(args.script).expanduser()
            if not script.is_file():
                # Agent-written task scripts live in the harness scripts dir
                # by convention; resolve bare filenames against it.
                candidate = scripts_dir() / args.script
                if candidate.is_file():
                    script = candidate
            try:
                source = script.read_text(encoding="utf-8-sig")
            except OSError as exc:
                print(f"windows-harness: cannot read {script}: {exc}", file=sys.stderr)
                return 1
            return _execute(source)
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




