"""Per-message select+delete: the shared ``delete_messages`` mutation.

The ONE transcript soft-delete behind the TUI's /select (and reusable by other
frontends): rewind's guards (idle session, generation fence, receipt retry),
exactly the named active rows, and tool-call pairs that delete as a unit so the
next request is never an orphaned tool_result or a dangling tool_calls block.
"""
import pytest
from hermes_state import SessionDB
import hermes_state_runtime as rt


def _epoch(db):
    return rt.begin_runtime_epoch(db, instance_id='owner')


def _mutate(db, epoch, sid, ids, request_id='del', **overrides):
    args = dict(epoch=epoch, principal_id='human', session_id=sid, request_id=request_id,
                expected_revision=0, expected_generation=0, operation='delete_messages',
                payload={'message_ids': ids})
    args.update(overrides)
    return rt.mutate_runtime_session(db, **args)


def _active(db, sid):
    return [m['content'] for m in db.get_messages(sid)]


def test_delete_messages_removes_only_the_named_rows_and_fences_the_generation(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        db.append_messages_batch('s', [
            {'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'reply'},
            {'role': 'user', 'content': 'again'}, {'role': 'assistant', 'content': 'final'}])
        ids = [m['id'] for m in db.get_messages('s')]
        epoch = _epoch(db)
        receipt = _mutate(db, epoch, 's', [ids[1], ids[2]])
        assert receipt['deleted_ids'] == [ids[1], ids[2]]
        assert receipt['deleted_count'] == 2
        assert receipt['new_head_id'] == ids[3]
        assert _active(db, 's') == ['first', 'final']
        assert db.get_session('s')['runtime_generation'] == 1
        # Exact retry replays the receipt; a stale revision/generation refuses.
        assert _mutate(db, epoch, 's', [ids[1], ids[2]]) == receipt
        with pytest.raises(rt.RuntimeStoreError, match='stale_generation'):
            _mutate(db, epoch, 's', [ids[3]], request_id='del2',
                    expected_revision=1, expected_generation=0)


def test_delete_messages_refuses_unknown_rows_and_empty_selection(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        db.append_message('s', role='user', content='keep')
        row_id = db.get_messages('s')[0]['id']
        epoch = _epoch(db)
        # Same refusal contract as a rewind: an unknown/empty target is a ValueError and
        # nothing changes (the receipt transaction never commits).
        with pytest.raises(ValueError):
            _mutate(db, epoch, 's', [row_id + 999])
        with pytest.raises(ValueError):
            _mutate(db, epoch, 's', [])
        assert _active(db, 's') == ['keep']
        assert db.get_session('s')['runtime_generation'] == 0


def test_delete_messages_refuses_queued_work(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        db.append_message('s', role='user', content='keep')
        row_id = db.get_messages('s')[0]['id']
        epoch = _epoch(db)
        rt.admit_session_input(db, epoch=epoch, principal_id='human', session_id='s',
                               request_id='input', payload={'text': 'queued'})
        with pytest.raises(rt.RuntimeStoreError, match='session_busy'):
            _mutate(db, epoch, 's', [row_id])
        assert _active(db, 's') == ['keep']


def test_delete_messages_takes_tool_call_pairs_as_a_unit(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        first = db.append_message('s', role='user', content='run it')
        call = db.append_message('s', role='assistant', content=None,
                                 tool_calls=[{'id': 'call_1', 'type': 'function',
                                              'function': {'name': 'terminal', 'arguments': '{}'}}])
        result = db.append_message('s', role='tool', content='ok', tool_name='terminal',
                                   tool_call_id='call_1')
        final = db.append_message('s', role='assistant', content='done')
        epoch = _epoch(db)
        # Selecting only the tool result pulls its call; selecting the call pulls the result.
        receipt = _mutate(db, epoch, 's', [result])
        assert receipt['deleted_ids'] == [result]
        assert receipt['new_head_id'] == final
        rows = db._conn.execute(
            'SELECT id, active FROM messages WHERE session_id = ? ORDER BY id', ('s',)).fetchall()
        assert {int(r['id']): r['active'] for r in rows} == {
            first: 1, call: 0, result: 0, final: 1}
        assert _active(db, 's') == ['run it', 'done']


def test_delete_messages_keeps_sibling_results_of_a_surviving_call(tmp_path):
    """Only the selected call's results go: another turn's tool pairing stays intact."""
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        db.append_message('s', role='user', content='one')
        keep_call = db.append_message('s', role='assistant', content=None,
                                      tool_calls=[{'id': 'call_keep', 'type': 'function',
                                                   'function': {'name': 'terminal', 'arguments': '{}'}}])
        db.append_message('s', role='tool', content='kept', tool_name='terminal', tool_call_id='call_keep')
        db.append_message('s', role='user', content='two')
        drop_call = db.append_message('s', role='assistant', content=None,
                                      tool_calls=[{'id': 'call_drop', 'type': 'function',
                                                   'function': {'name': 'files', 'arguments': '{}'}}])
        drop_result = db.append_message('s', role='tool', content='dropped', tool_name='files',
                                        tool_call_id='call_drop')
        epoch = _epoch(db)
        _mutate(db, epoch, 's', [drop_call])
        rows = db._conn.execute(
            'SELECT id, active FROM messages WHERE session_id = ? ORDER BY id', ('s',)).fetchall()
        state = {int(r['id']): r['active'] for r in rows}
        assert state[drop_result] == 0 and state[drop_call] == 0
        assert state[keep_call] == 1
        assert len([1 for active in state.values() if active]) == 4
