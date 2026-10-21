"""Transaction-scoped rewind shared by storage and canonical mutations."""
import json

from hermes_state_messages import _ACTIVE_IDS_SQL, _SET_COUNTERS_SQL, _placeholders


def _tool_pairing_expansion(rows, selected):
    """A deleted row must keep the transcript structurally valid: an assistant row's tool calls
    take their active tool results with them, and a deleted tool result takes the call that made
    it (and that call's sibling results) — the next request would otherwise be an orphaned
    tool_result / a tool_calls block without its responses. Fixed-point over the pairing maps."""
    by_id = {int(row['id']): row for row in rows}
    results_of_call = {}
    assistant_of_call = {}
    calls_of_assistant = {}
    for row in rows:
        row_id = int(row['id'])
        if row['role'] == 'tool' and row['tool_call_id']:
            results_of_call.setdefault(row['tool_call_id'], set()).add(row_id)
            continue
        call_ids = _row_tool_call_ids(row)
        if call_ids:
            calls_of_assistant[row_id] = call_ids
            for call_id in call_ids:
                assistant_of_call.setdefault(call_id, row_id)

    expanded = set(selected)
    frontier = list(selected)
    while frontier:
        row = by_id.get(frontier.pop())
        if row is None:
            continue
        if row['role'] == 'tool' and row['tool_call_id']:
            # The result pulls the assistant row that made its call…
            targets = [assistant_of_call[row['tool_call_id']]] \
                if row['tool_call_id'] in assistant_of_call else []
        else:
            # …and any row (the call itself included) pulls that call's active results.
            targets = []
            for call_id in calls_of_assistant.get(int(row['id']), []) + _row_tool_call_ids(row):
                targets.extend(results_of_call.get(call_id, ()))
                if call_id in assistant_of_call:
                    targets.append(assistant_of_call[call_id])
        for mate in targets:
            if mate not in expanded:
                expanded.add(mate)
                frontier.append(mate)
    return expanded


def _row_tool_call_ids(row):
    if not row['tool_calls']:
        return []
    try:
        parsed = json.loads(row['tool_calls'])
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [call['id'] for call in parsed if isinstance(call, dict) and isinstance(call.get('id'), str)]


def rewind_in_transaction(self, conn, session_id, target_message_id, *,
                          preserve_compaction_handoff=False, expected_active_ids=None,
                          expected_target_content=None):
    self._check_transcript_write_guards(
        conn, session_id, None, reject_active_turn_lease=True, reject_active_compression_lock=True)
    if expected_active_ids is not None:
        active_rows = conn.execute(_ACTIVE_IDS_SQL, (session_id,)).fetchall()
        if [int(r[0]) for r in active_rows] != expected_active_ids:
            raise RuntimeError("active transcript changed before the rewind could be persisted")
    row = conn.execute(
        "SELECT * FROM messages WHERE id = ? AND session_id = ?", (target_message_id, session_id)).fetchone()
    if row is None:
        raise ValueError(f"message {target_message_id} not found in session {session_id}")
    target_row = dict(row)
    if target_row.get("role") != "user":
        raise ValueError(
            f"rewind target must be a 'user' message (got role={target_row.get('role')!r}, id={target_message_id})")
    replacement_message_id = replacement = None
    if preserve_compaction_handoff or expected_target_content is not None:
        replacement = self._split_rewind_target(target_row, expected_target_content, preserve_compaction_handoff)
    ids = [r[0] for r in conn.execute("SELECT id FROM messages WHERE session_id = ? AND id >= ? AND active = 1",
                                     (session_id, target_message_id)).fetchall()]
    if ids:
        conn.execute(f"UPDATE messages SET active = 0 WHERE id IN ({_placeholders(ids)})", ids)
    if replacement is not None:
        self._insert_message_rows(conn, session_id, [replacement])
        replacement_message_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "UPDATE sessions SET rewind_count = COALESCE(rewind_count, 0) + 1 WHERE id = ?", (session_id,))
    message_count, tool_call_count = self._active_transcript_counts(conn, session_id)
    conn.execute(f"{_SET_COUNTERS_SQL} WHERE id = ?", (message_count, tool_call_count, session_id))
    head_id = conn.execute(
        "SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 1", (session_id,)).fetchone()[0]
    return target_row, ids, head_id, replacement_message_id


def delete_messages_in_transaction(self, conn, session_id, message_ids):
    """Soft-delete specific active transcript rows (the ONE implementation behind the TUI's
    message selection delete; other frontends reuse the shared ``delete_messages`` mutation).
    Same guards as a rewind: the session must be idle, and every named row must still be an
    active row of this session's PHYSICAL transcript, else ``ValueError`` and nothing changes.
    Tool-call pairs delete as a unit (see _tool_pairing_expansion). Returns
    ``(deleted_ids, new_head_id)``."""
    self._check_transcript_write_guards(
        conn, session_id, None, reject_active_turn_lease=True, reject_active_compression_lock=True)
    wanted = sorted(set(message_ids))
    if not wanted:
        raise ValueError('no message ids given')
    placeholders = _placeholders(wanted)
    active = {int(r[0]) for r in conn.execute(
        f"SELECT id FROM messages WHERE session_id = ? AND id IN ({placeholders}) AND active = 1",
        (session_id, *wanted)).fetchall()}
    missing = [mid for mid in wanted if mid not in active]
    if missing:
        raise ValueError(f"message(s) not active in session {session_id}: {missing[:5]}")
    rows = conn.execute(
        "SELECT id, role, tool_call_id, tool_calls FROM messages WHERE session_id = ? AND active = 1",
        (session_id,)).fetchall()
    ids = sorted(_tool_pairing_expansion(rows, active))
    for start in range(0, len(ids), 400):
        part = ids[start:start + 400]
        conn.execute(f"UPDATE messages SET active = 0 WHERE id IN ({_placeholders(part)})", part)
    message_count, tool_call_count = self._active_transcript_counts(conn, session_id)
    conn.execute(f"{_SET_COUNTERS_SQL} WHERE id = ?", (message_count, tool_call_count, session_id))
    head_id = conn.execute(
        "SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 1", (session_id,)).fetchone()[0]
    return wanted, head_id
