"""Wire contract for the ACP ``_session/steering`` extension.

Mirrors the client side in agent-orchestrator's ``chatdriver/acp``: the
capability is advertised in the initialize response ``_meta`` as
``{"steering": {"supported": true}}``, and steering requests carry
``{"sessionId", "prompt": [<content blocks>]}`` and read
``{"outcome": "injected" | "promptRequired" | "startedNewTurn"}``.
Every test goes through the real SDK agent router, so the extension-name
stripping (``_session/steering`` → ``session/steering``) is under test too.
"""

import pytest

from acp.agent.router import build_agent_router
from acp.exceptions import RequestError
from acp.schema import InitializeResponse
from acp_adapter.gateway_server import GatewayACPAgent
from acp_adapter.server import HermesACPAgent
from acp_adapter.session import SessionManager
from hermes_cli.gateway_client import GatewayRPCError

STEER_PARAMS_META = {"steering": {"idleBehavior": "promptRequired"}}


def steer_payload(session_id: str, text: str) -> dict:
    return {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": text}],
        "_meta": STEER_PARAMS_META,
    }


class RecordingAgent:
    """The injection surface the handler relies on: steer() buffers, withdraw_steer() undoes."""

    def __init__(self, state=None):
        self.buffered: list[tuple[str, object]] = []
        self._state = state

    def steer(self, text: str, *, ticket=None) -> bool:
        self.buffered.append((text, ticket))
        return True

    def withdraw_steer(self, ticket) -> bool:
        for index, (_, buffered_ticket) in enumerate(self.buffered):
            if buffered_ticket == ticket:
                del self.buffered[index]
                return True
        return False


class TurnEndingAgent(RecordingAgent):
    """A steer that consumes itself: buffering it ends the turn (the loss side of the race)."""

    def steer(self, text: str, *, ticket=None) -> bool:
        with self._state.runtime_lock:
            self._state.is_running = False
        return super().steer(text, ticket=ticket)


@pytest.fixture()
def server():
    return HermesACPAgent(session_manager=SessionManager(agent_factory=lambda: object()))


@pytest.fixture()
def router(server):
    return build_agent_router(server)


async def _new_session_with(server, agent) -> tuple[str, object]:
    resp = await server.new_session(cwd="/tmp")
    state = server.session_manager.get_session(resp.session_id)
    assert state is not None
    state.agent = agent
    return resp.session_id, state


# ---------------------------------------------------------------------------
# capability advertisement
# ---------------------------------------------------------------------------


class TestSteeringAdvertisement:
    @pytest.mark.asyncio
    async def test_initialize_advertises_steering_capability(self, server):
        resp = await server.initialize(protocol_version=1)

        assert isinstance(resp, InitializeResponse)
        assert resp.field_meta == {"steering": {"supported": True}}

    @pytest.mark.asyncio
    async def test_initialize_wire_payload_carries_meta_alias(self, server):
        resp = await server.initialize(protocol_version=1)

        wire = resp.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
        assert wire["_meta"] == {"steering": {"supported": True}}


# ---------------------------------------------------------------------------
# extension dispatch
# ---------------------------------------------------------------------------


class TestExtensionDispatch:
    @pytest.mark.asyncio
    async def test_unknown_extension_method_still_returns_32601(self, router):
        with pytest.raises(RequestError) as excinfo:
            await router("_other/extension", {}, False)

        assert excinfo.value.code == -32601

    @pytest.mark.asyncio
    async def test_unknown_session_is_a_json_rpc_error(self, server, router):
        await server.new_session(cwd="/tmp")

        with pytest.raises(RequestError) as excinfo:
            await router("_session/steering", steer_payload("no-such-session", "hello"), False)

        assert excinfo.value.code == -32602


# ---------------------------------------------------------------------------
# steering outcomes
# ---------------------------------------------------------------------------


class TestSteeringOutcomes:
    @pytest.mark.asyncio
    async def test_steer_during_active_turn_returns_injected_and_reaches_agent(self, server, router):
        agent = RecordingAgent()
        session_id, state = await _new_session_with(server, agent)
        with state.runtime_lock:
            state.is_running = True

        result = await router("_session/steering", steer_payload(session_id, "Skip the build, run the tests"), False)

        assert result == {"outcome": "injected"}
        assert [text for text, _ in agent.buffered] == ["Skip the build, run the tests"]

    @pytest.mark.asyncio
    async def test_steer_when_idle_returns_prompt_required_without_buffering(self, server, router):
        agent = RecordingAgent()
        session_id, _state = await _new_session_with(server, agent)

        result = await router("_session/steering", steer_payload(session_id, "Too late"), False)

        assert result["outcome"] == "promptRequired"
        assert agent.buffered == []

    @pytest.mark.asyncio
    async def test_lost_race_takes_the_text_back_and_returns_prompt_required(self, server):
        # steer() ends the turn while buffering it — the loss side of the race.
        agent = TurnEndingAgent()
        session_id, state = await _new_session_with(server, agent)
        agent._state = state
        with state.runtime_lock:
            state.is_running = True
        fresh_router = build_agent_router(server)

        result = await fresh_router("_session/steering", steer_payload(session_id, "Turn is ending"), False)

        assert result["outcome"] == "promptRequired"
        assert agent.buffered == []  # the text was withdrawn, not left detached

    @pytest.mark.asyncio
    async def test_textless_prompt_returns_prompt_required(self, server, router):
        session_id, _state = await _new_session_with(server, RecordingAgent())

        result = await router(
            "_session/steering",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "   "}], "_meta": STEER_PARAMS_META},
            False,
        )

        assert result["outcome"] == "promptRequired"


# ---------------------------------------------------------------------------
# gateway-backed server (the class `hermes acp` actually serves)
# ---------------------------------------------------------------------------


class StubGatewayClient:
    def __init__(self, steer_result=None, steer_error=None):
        self.steer_result = steer_result or {}
        self.steer_error = steer_error
        self.calls = []

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if self.steer_error is not None:
            raise self.steer_error
        return self.steer_result


def gateway_agent(steer_result=None, steer_error=None, generation=7, aliases=None) -> GatewayACPAgent:
    agent = object.__new__(GatewayACPAgent)
    agent._aliases = dict(aliases or {})
    agent._snapshots = {"gw-1": {"execution_generation": generation}}
    stub = StubGatewayClient(steer_result=steer_result, steer_error=steer_error)
    agent._client_result = stub

    async def _client():
        return stub

    agent._client = _client
    return agent


def steer_params(text="Skip the build", session_id="gw-1") -> dict:
    return {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": text}],
        "_meta": STEER_PARAMS_META,
    }


class TestGatewaySteering:
    @pytest.mark.asyncio
    async def test_gateway_initialize_advertises_steering(self):
        agent = GatewayACPAgent()

        resp = await agent.initialize()
        wire = resp.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)

        assert wire["_meta"] == {"steering": {"supported": True}}

    @pytest.mark.asyncio
    async def test_gateway_steer_sends_session_steer_and_returns_injected(self):
        agent = gateway_agent(steer_result={"status": "queued", "text": "x", "execution_generation": 7})

        result = await agent._steer_session(steer_params())

        assert result == {"outcome": "injected"}
        method, params = agent._client_result.calls[0]
        assert method == "session.steer"
        assert params["session_id"] == "gw-1"
        assert params["text"] == "Skip the build"
        assert params["execution_generation"] == 7

    @pytest.mark.asyncio
    async def test_gateway_rejected_steer_maps_to_prompt_required(self):
        agent = gateway_agent(steer_result={"status": "rejected", "delivered": False,
                                            "text": "x", "execution_generation": 7})

        result = await agent._steer_session(steer_params())

        assert result["outcome"] == "promptRequired"

    @pytest.mark.asyncio
    async def test_gateway_lost_race_maps_to_prompt_required(self):
        agent = gateway_agent(steer_error=GatewayRPCError("stale_generation"))

        result = await agent._steer_session(steer_params())

        assert result["outcome"] == "promptRequired"

    @pytest.mark.asyncio
    async def test_gateway_unknown_session_is_a_json_rpc_error(self):
        agent = gateway_agent()

        with pytest.raises(RequestError) as excinfo:
            await agent._steer_session(steer_params(session_id="nope"))

        assert excinfo.value.code == -32602

    @pytest.mark.asyncio
    async def test_gateway_steer_resolves_alias_to_the_gateway_session(self):
        agent = gateway_agent(steer_result={"status": "queued"}, aliases={"held-9": "gw-1"})

        result = await agent._steer_session(steer_params(session_id="held-9"))

        assert result == {"outcome": "injected"}
        assert agent._client_result.calls[0][1]["session_id"] == "gw-1"
