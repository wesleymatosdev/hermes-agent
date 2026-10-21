"""Control-socket ``steer-session`` verb: out-of-process steering into a live turn."""

from __future__ import annotations

import json

import pytest

from agent.prompt_builder import FYI_CONTEXT_LEAD
from gateway.control_socket import CONTROL_PROTOCOL_VERSION, GatewayControlServer
from gateway.run_busy import steer_session_verb


class _FakeAgent:
    def __init__(self, accept: bool = True):
        self.accept = accept
        self.steered: list[str] = []

    def steer(self, text: str) -> bool:
        if not self.accept:
            return False
        self.steered.append(text)
        return True


class _FakeRunner:
    """Just the runner surface the verb touches; ``_running_agent_items`` is attached by ``_runner``."""

    def __init__(self, sessions: dict[str, _FakeAgent], pending: dict[str, object] | None = None):
        self._sessions = dict(sessions)
        self._pending = pending or {}

    def _steer_running_agent(self, agent, text: str) -> bool:
        return bool(agent.steer(text))

    def _agent_has_active_subagents(self, agent) -> bool:
        return False


def _runner(sessions: dict[str, _FakeAgent], pending: dict[str, object] | None = None) -> _FakeRunner:
    runner = _FakeRunner(sessions, pending)
    # Real runners return one flat list of (key, agent) pairs, pending sentinels included.
    runner._running_agent_items = lambda: [  # type: ignore[method-assign]
        *(pair for pair in sessions.items()),
        *(pair for pair in (pending or {}).items()),
    ]
    return runner


def _handle(runner, params: dict) -> dict:
    return steer_session_verb(runner)(params)


class TestSteerSessionVerb:

    def test_exact_session_key_steers_that_agent_only(self):
        a, b = _FakeAgent(), _FakeAgent()
        runner = _runner({"telegram:111": a, "telegram:222": b})
        result = _handle(runner, {"session_key": "telegram:222", "text": "use staging creds"})
        assert result == {"steered": True, "session_key": "telegram:222", "fyi": False,
                          "subagents": False}
        assert a.steered == [] and b.steered == ["use staging creds"]

    def test_unique_fragment_matches_case_insensitively(self):
        agent = _FakeAgent()
        runner = _runner({"desktop:abc-123:main": agent})
        result = _handle(runner, {"session_key": "ABC-123", "text": "note"})
        assert result["steered"] is True and result["session_key"] == "desktop:abc-123:main"

    def test_ambiguous_fragment_lists_matches(self):
        runner = _runner({"telegram:111": _FakeAgent(), "telegram:222": _FakeAgent()})
        result = _handle(runner, {"session_key": "telegram", "text": "note"})
        assert result["steered"] is False and result["reason"] == "ambiguous"
        assert result["matches"] == ["telegram:111", "telegram:222"]

    def test_single_live_session_auto_targets(self):
        agent = _FakeAgent()
        runner = _runner({"discord:#ops": agent})
        assert _handle(runner, {"text": "note"})["session_key"] == "discord:#ops"

    def test_no_target_with_multiple_sessions_is_ambiguous(self):
        runner = _runner({"telegram:111": _FakeAgent(), "discord:#ops": _FakeAgent()})
        result = _handle(runner, {"text": "note"})
        assert result["reason"] == "ambiguous"
        assert result["matches"] == ["discord:#ops", "telegram:111"]
        assert result["live_sessions"] == ["discord:#ops", "telegram:111"]

    def test_pending_turn_without_steer_is_not_steerable(self):
        class _StartingTurn:
            pass  # the pending sentinel: a bare object with no steer method

        runner = _runner({}, pending={"telegram:111": _StartingTurn()})
        result = _handle(runner, {"session_key": "telegram:111", "text": "note"})
        assert result["steered"] is False and result["reason"] == "no_target"

    def test_fyi_flag_wraps_the_payload(self):
        agent = _FakeAgent()
        runner = _runner({"telegram:111": agent})
        _handle(runner, {"session_key": "telegram:111", "text": "creds rotated", "fyi": True})
        assert agent.steered[0].startswith(FYI_CONTEXT_LEAD)
        assert "creds rotated" in agent.steered[0]

    def test_plain_text_is_not_framed(self):
        agent = _FakeAgent()
        runner = _runner({"telegram:111": agent})
        _handle(runner, {"session_key": "telegram:111", "text": "creds rotated"})
        assert agent.steered == ["creds rotated"]

    def test_missing_text_raises_for_the_envelope(self):
        with pytest.raises(ValueError):
            _handle(_runner({}), {})

    def test_list_verb_returns_sessions_without_text(self):
        runner = _runner({"telegram:111": _FakeAgent()})
        result = _handle(runner, {"list": True})
        assert result["live_sessions"] == ["telegram:111"]


class TestSteerSessionEnvelope:
    """The verb rides the standard control-socket envelope (one JSON line in, one out)."""

    def test_envelope_round_trip(self, tmp_path):
        agent = _FakeAgent()
        runner = _runner({"telegram:111": agent})
        server = GatewayControlServer(home=tmp_path, verb_handlers={"steer-session": steer_session_verb(runner)})
        request = json.dumps({"verb": "steer-session", "id": 7, "protocol": CONTROL_PROTOCOL_VERSION,
                              "params": {"session_key": "telegram:111", "text": "hi"}}).encode()
        response = json.loads(server.handle_request_line(request))
        assert response["ok"] is True and response["id"] == 7
        assert response["result"]["steered"] is True
        assert agent.steered == ["hi"]

    def test_envelope_unknown_verb_is_a_refusal_not_an_error(self, tmp_path):
        server = GatewayControlServer(home=tmp_path)
        request = json.dumps({"verb": "steer-session", "id": 1, "protocol": CONTROL_PROTOCOL_VERSION,
                              "params": {}}).encode()
        response = json.loads(server.handle_request_line(request))
        assert response["ok"] is False
        assert "unknown verb" in response["error"]
