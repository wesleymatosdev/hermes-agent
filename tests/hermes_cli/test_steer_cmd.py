"""``hermes steer`` CLI subcommand: arg parsing, control-socket params, and output routing."""

from __future__ import annotations

import json

import pytest

from hermes_cli.steer_cmd import cmd_steer, register_steer_subparser


@pytest.fixture
def parser():
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(required=True)
    register_steer_subparser(sub)
    return p


def _capture(monkeypatch, response):
    calls: list[tuple] = []

    def fake_request(home, verb, *, params=None, timeout=None):
        calls.append((home, verb, params, timeout))
        return response

    monkeypatch.setattr("gateway.control_socket.request_gateway_control", fake_request)
    return calls


def test_plain_steer_sends_session_and_text(parser, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = _capture(monkeypatch, {"ok": True, "result": {
        "steered": True, "session_key": "telegram:111", "fyi": False, "subagents": False}})
    args = parser.parse_args(["steer", "--session", "telegram:111", "use staging creds"])
    cmd_steer(args)
    home, verb, params, timeout = calls[0]
    assert verb == "steer-session" and home == tmp_path and timeout == 5.0
    assert params == {"text": "use staging creds", "session_key": "telegram:111"}
    assert "telegram:111" in capsys.readouterr().out


def test_fyi_flag_requests_framing(parser, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = _capture(monkeypatch, {"ok": True, "result": {
        "steered": True, "session_key": "s", "fyi": True, "subagents": True}})
    args = parser.parse_args(["steer", "--fyi", "context here"])
    cmd_steer(args)
    assert calls[0][2]["fyi"] is True
    # Framing happens gateway-side; the wire only carries the flag.
    assert "FYI_CONTEXT_LEAD" not in json.dumps(calls[0][2])
    assert "subagent(s)" in capsys.readouterr().out


def test_list_mode_sends_list_flag(parser, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    calls = _capture(monkeypatch, {"ok": True, "result": {
        "steered": False, "reason": "list", "live_sessions": ["telegram:111"]}})
    args = parser.parse_args(["steer", "--list"])
    cmd_steer(args)
    assert calls[0][2] == {"list": True}
    assert "telegram:111" in capsys.readouterr().out


def test_no_gateway_answers_exits_nonzero(parser, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.control_socket.request_gateway_control",
                        lambda *a, **k: None)
    args = parser.parse_args(["steer", "hello"])
    with pytest.raises(SystemExit) as exc:
        cmd_steer(args)
    assert exc.value.code == 1
    assert "no gateway answered" in capsys.readouterr().err


def test_no_text_is_a_usage_error(parser, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("sys.stdin", type("_T", (), {"isatty": lambda self: True})())
    args = parser.parse_args(["steer"])
    with pytest.raises(SystemExit) as exc:
        cmd_steer(args)
    assert exc.value.code == 2


def test_ambiguous_result_lists_matches_and_exits_nonzero(parser, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _capture(monkeypatch, {"ok": True, "result": {
        "steered": False, "reason": "ambiguous", "matches": ["telegram:111", "telegram:222"],
        "live_sessions": ["telegram:111", "telegram:222"]}})
    args = parser.parse_args(["steer", "-s", "telegram", "note"])
    with pytest.raises(SystemExit) as exc:
        cmd_steer(args)
    assert exc.value.code == 1
    assert "Ambiguous" in capsys.readouterr().err


def test_json_mode_prints_raw_result(parser, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    payload = {"steered": True, "session_key": "s", "fyi": False, "subagents": False}
    _capture(monkeypatch, {"ok": True, "result": payload})
    args = parser.parse_args(["steer", "--json", "note"])
    cmd_steer(args)
    assert json.loads(capsys.readouterr().out) == payload
