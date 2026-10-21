"""_session/steering delivers guidance into the ACP session's own running turn —
or rejects so Agent Orchestrator falls back to a normal send. Nothing here starts a turn."""
from unittest.mock import AsyncMock

import pytest
from acp.agent.router import build_agent_router

from acp_adapter.gateway_server import GatewayACPAgent
from hermes_cli.gateway_client import GatewayClientError

STEER = {'sessionId': 's', 'prompt': 'skip the demo, ship the fix'}


def live_agent():
    """A held session whose prompt is admitted and running under generation 8."""
    agent = GatewayACPAgent()
    agent._snapshots['s'] = {'execution_generation': 8}
    agent._admissions['s'] = 'ours'
    agent._gateway = AsyncMock()
    agent._conn = AsyncMock()
    return agent


async def steer_via_rpc(agent, params=None):
    async def rpc(method, **call):
        if method == 'prompt.receipt':
            return {'admission_id': 'ours', 'status': 'started', 'execution_generation': 8}
        if method == 'session.steer':
            return {'status': call.get('status', 'queued'), 'text': call['text'],
                    'execution_generation': call['execution_generation']}
        return {}

    agent._gateway.rpc.side_effect = rpc
    return await agent.ext_method('session/steering', params or STEER)


@pytest.mark.asyncio
async def test_steering_is_delivered_to_the_running_generation_we_own():
    agent = live_agent()
    result = await steer_via_rpc(agent)
    assert result == {'outcome': 'delivered'}
    calls = {call.args[0]: call.kwargs for call in agent._gateway.rpc.await_args_list}
    assert calls['session.steer'] == {'session_id': 's', 'text': STEER['prompt'],
                                      'execution_generation': 8}


@pytest.mark.asyncio
async def test_steering_without_our_admission_is_idle_and_never_calls_the_owner():
    agent = live_agent()
    agent._admissions.clear()
    result = await agent.ext_method('session/steering', STEER)
    assert result == {'outcome': 'rejected', 'reason': 'idle'}
    agent._gateway.rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_steering_a_settled_turn_reads_as_idle():
    agent = live_agent()

    async def rpc(method, **params):
        if method == 'prompt.receipt':
            # The turn finished between the admission and the steer.
            return {'admission_id': 'ours', 'status': 'terminal', 'execution_generation': 8}
        return {}

    agent._gateway.rpc.side_effect = rpc
    result = await agent.ext_method('session/steering', STEER)
    assert result == {'outcome': 'rejected', 'reason': 'idle'}


@pytest.mark.asyncio
async def test_stale_generation_between_receipt_and_steer_reads_as_idle():
    agent = live_agent()

    async def rpc(method, **params):
        if method == 'prompt.receipt':
            return {'admission_id': 'ours', 'status': 'started', 'execution_generation': 8}
        if method == 'session.steer':
            raise GatewayClientError('stale_generation')
        return {}

    agent._gateway.rpc.side_effect = rpc
    result = await agent.ext_method('session/steering', STEER)
    assert result == {'outcome': 'rejected', 'reason': 'idle'}


@pytest.mark.asyncio
async def test_unknown_or_restored_session_is_rejected():
    agent = live_agent()
    result = await agent.ext_method('session/steering', {'sessionId': 'ghost', 'prompt': 'hi'})
    assert result == {'outcome': 'rejected', 'reason': 'unknown session'}
    agent._gateway.rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_refusal_is_rejected_steering():
    agent = live_agent()

    async def rpc(method, **params):
        if method == 'prompt.receipt':
            return {'admission_id': 'ours', 'status': 'started', 'execution_generation': 8}
        if method == 'session.steer':
            return {'status': 'rejected'}
        return {}

    agent._gateway.rpc.side_effect = rpc
    result = await agent.ext_method('session/steering', STEER)
    assert result == {'outcome': 'rejected', 'reason': 'steer rejected'}


@pytest.mark.asyncio
async def test_alias_routes_to_the_logical_session():
    agent = live_agent()
    agent._aliases['held-1'] = 's'
    result = await steer_via_rpc(agent, {'sessionId': 'held-1', 'prompt': 'go'})
    assert result == {'outcome': 'delivered'}
    calls = {call.args[0]: call.kwargs for call in agent._gateway.rpc.await_args_list}
    assert calls['session.steer']['session_id'] == 's'


@pytest.mark.asyncio
async def test_router_dispatches_the_underscore_method_to_the_handler():
    """The registration receipt: the SDK router's extension route answers `_session/steering`
    (its request table can never see underscore methods), and unknown extensions stay
    method-not-found."""
    agent = live_agent()

    async def rpc(method, **params):
        if method == 'prompt.receipt':
            return {'admission_id': 'ours', 'status': 'started', 'execution_generation': 8}
        if method == 'session.steer':
            return {'status': 'queued'}
        return {}

    agent._gateway.rpc.side_effect = rpc
    router = build_agent_router(agent)
    result = await router('_session/steering', STEER, False)
    assert result == {'outcome': 'delivered'}
    with pytest.raises(Exception, match='Method not found'):
        await router('_session/definitely_not_a_method', {}, False)
