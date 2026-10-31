"""ACP ``_session/steering`` extension — mid-turn guidance over the wire.

Contract (AO ``chatdriver/acp`` is the client): the capability is advertised in
the ``initialize`` response ``_meta`` as ``{"steering": {"supported": true}}``;
the client then calls ``_session/steering`` with
``{"sessionId", "prompt": [<content blocks>], "_meta": {"steering": {"idleBehavior": "promptRequired"}}}``
and reads ``{"outcome": "injected" | "promptRequired" | "startedNewTurn", "reason"?}``.

The injection path is the same one the TUI/CLI ``/steer`` uses: the text is
buffered on the running agent (``AIAgent.steer``) and drained into the active
turn's next tool result as its own user row. ``startedNewTurn`` is never
returned — this handler never starts a turn; when there is nothing to inject
into, the client is told ``promptRequired`` so it can redeliver the text as a
follow-up prompt.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from acp.exceptions import RequestError

logger = logging.getLogger(__name__)

# The wire method is ``_session/steering``; the SDK strips the leading ``_``
# before handing the request to ``HermesACPAgent.ext_method``.
STEERING_METHOD = "session/steering"


def steering_capability_meta() -> dict[str, Any]:
    """``_meta`` payload advertising the steering extension in ``initialize``."""
    return {"steering": {"supported": True}}


def prompt_text(payload: dict[str, Any]) -> str:
    """Plain text of the steering prompt's content blocks (``{"type": "text", "text": ...}``)."""
    blocks = payload.get("prompt")
    if not isinstance(blocks, list):
        return ""
    parts = [
        block["text"] for block in blocks
        if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip()
    ]
    return "\n".join(parts).strip()


def handle_steering_request(agent_server: Any, payload: dict[str, Any]) -> dict[str, str]:
    """Serve one ``_session/steering`` request for ``agent_server``'s sessions.

    Outcomes: ``injected`` — the text is buffered on the agent for the active
    turn; ``promptRequired`` — nothing was injected (session idle, empty
    prompt, or the turn settled in the race window) and the client should
    redeliver the text as a follow-up prompt. Unknown sessions and malformed
    params are JSON-RPC errors, never silent outcomes.
    """
    session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise RequestError.invalid_params({"reason": "_session/steering requires sessionId"})

    text = prompt_text(payload)
    if not text:
        return {"outcome": "promptRequired", "reason": "steering prompt carried no text"}

    state = agent_server.session_manager.get_session(session_id)
    if state is None:
        raise RequestError.invalid_params({"reason": f"unknown session {session_id}"})

    with state.runtime_lock:
        running = state.is_running
    steer = getattr(state.agent, "steer", None)
    if not running or not callable(steer):
        return {
            "outcome": "promptRequired",
            "reason": "session idle — no active turn" if not running
            else "agent does not accept mid-turn guidance",
        }

    # Ticket the submission so a lost race can be taken back out of the buffer
    # (withdraw_steer) instead of leaking into some later turn detached.
    ticket = f"acp-steering-{uuid.uuid4()}"
    if not steer(text, ticket=ticket):
        return {"outcome": "promptRequired", "reason": "agent rejected the steering text"}

    # The turn can finish between the is_running check and the buffer write;
    # then the text would sit unconsumed in _pending_steer. Take it back and
    # let the client redeliver, unless a drain already handed it to the turn.
    with state.runtime_lock:
        still_running = state.is_running
    if not still_running:
        withdraw = getattr(state.agent, "withdraw_steer", None)
        if callable(withdraw) and withdraw(ticket):
            return {
                "outcome": "promptRequired",
                "reason": "the active turn settled before the guidance could be injected",
            }
    return {"outcome": "injected"}
