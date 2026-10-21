"""CLI subcommand: ``hermes steer`` — inject guidance into a RUNNING session's turn from the shell.

The out-of-process counterpart of the in-session ``/steer`` and ``/fyi`` commands: it talks to the
local gateway's control socket (``$HERMES_HOME/gateway.sock``), which owns the live session agents.
Delivery is identical to ``/steer`` — the text lands as its own user message right after the
current tool batch, no interrupt — and ``--fyi`` frames it as context-only so the agent folds it
into the work in progress instead of redirecting. Requires a running gateway/serve backend; the
session must have a live (not just starting) turn.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn, Optional

_USAGE_EXIT = 2
_FAILURE_EXIT = 1
_SUCCESS_EXIT = 0


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _read_message_body(positional: Optional[str], file_path: Optional[str]) -> Optional[str]:
    """Positional arg, then ``--file PATH`` (``-`` = stdin), then piped stdin. None when empty."""
    if positional:
        return positional
    if file_path:
        if file_path == "-":
            return sys.stdin.read()
        try:
            return Path(file_path).read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            print(f"hermes steer: {file_path} is not a text file.", file=sys.stderr)
            return None
        except OSError as exc:
            print(f"hermes steer: cannot read {file_path}: {exc}", file=sys.stderr)
            return None
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return None


def _fail(msg: str, exit_code: int) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(exit_code)


def _emit(result: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
        return
    if result.get("steered"):
        target = result.get("session_key") or "?"
        suffix = " and its active subagent(s)" if result.get("subagents") else ""
        kind = "FYI (context-only)" if result.get("fyi") else "Steer"
        print(f"⏩ {kind} delivered into the live turn of {target}{suffix} — arrives after the "
              f"current tool call; the agent keeps working.")
    elif result.get("reason") == "list":
        sessions = result.get("live_sessions") or []
        if sessions:
            print("Live sessions with a steerable turn:")
            for key in sessions:
                print(f"  {key}")
        else:
            print("No live sessions with a steerable turn right now.")
    else:
        reason = result.get("reason")
        sessions = result.get("live_sessions") or []
        if reason == "ambiguous":
            print("Ambiguous --session fragment; matches:", file=sys.stderr)
        elif reason == "no_target":
            print("No matching live session. Live sessions with a steerable turn:", file=sys.stderr)
        else:
            print(f"Steer not accepted (reason: {reason}).", file=sys.stderr)
        for key in sessions:
            print(f"  {key}", file=sys.stderr)


def cmd_steer(args: argparse.Namespace) -> None:
    """Entry point wired into the top-level argparse dispatcher."""
    from gateway.control_socket import request_gateway_control

    params: dict[str, Any] = {}
    if getattr(args, "list_sessions", False):
        params["list"] = True
    else:
        text = _read_message_body(getattr(args, "message", None), getattr(args, "file", None))
        if text is None or not text.strip():
            _fail(
                "hermes steer: no steer text provided. Pass it as a positional argument, "
                "use --file PATH, or pipe it via stdin.",
                _USAGE_EXIT)
        params["text"] = text.strip()
        if getattr(args, "session", None):
            params["session_key"] = args.session.strip()
        if getattr(args, "fyi", False):
            params["fyi"] = True

    response = request_gateway_control(_hermes_home(), "steer-session", params=params,
                                       timeout=getattr(args, "timeout", None) or 5.0)
    if response is None:
        _fail(
            f"hermes steer: no gateway answered at {_hermes_home() / 'gateway.sock'}. "
            "Steering reaches sessions whose turn runs inside the running gateway/serve backend — "
            "start it (hermes gateway start / hermes serve) and retry.",
            _FAILURE_EXIT)
    if response.get("ok") is not True:
        _fail(f"hermes steer: gateway refused: {response.get('error')}", _FAILURE_EXIT)

    raw_result = response.get("result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    _emit(result, json_mode=getattr(args, "json", False))
    if result.get("steered") is not True and result.get("reason") != "list":
        sys.exit(_FAILURE_EXIT)


def register_steer_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "steer",
        help="Steer a running session's turn from the shell (no interrupt; --fyi for context-only)")
    parser.add_argument(
        "message", nargs="?",
        help="Steer text. If omitted, read from --file or piped stdin.")
    parser.add_argument(
        "-s", "--session", dest="session",
        help="Target session key, or a unique fragment of it (e.g. telegram, desktop). "
             "Omit when exactly one live session is steerable.")
    parser.add_argument(
        "--fyi", action="store_true",
        help="Context-only delivery (/fyi semantics): the agent folds it into the work in "
             "progress and finishes, instead of treating it as a redirect.")
    parser.add_argument(
        "-f", "--file", help="Read steer text from PATH ('-' = stdin).")
    parser.add_argument(
        "--list", dest="list_sessions", action="store_true",
        help="List live sessions with a steerable turn, then exit.")
    parser.add_argument(
        "--json", action="store_true", help="Emit the raw control-socket result as JSON.")
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="Control-socket round-trip timeout in seconds.")
    parser.set_defaults(func=cmd_steer)
