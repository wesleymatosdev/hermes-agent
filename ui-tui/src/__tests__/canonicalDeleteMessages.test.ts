import { beforeEach, expect, it, vi } from 'vitest'

import { createSlashHandler } from '../app/createSlashHandler.js'
import { deleteMessagesPayload } from '../app/messageSelection.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'

// /select commits the shared per-row soft-delete (`session.mutate
// operation=delete_messages`) with the snapshot CAS tuple, repaints the
// transcript from the fresh snapshot a committed receipt implies, and clears
// the overlay — never the legacy slash.exec RPC.

const flush = () => new Promise(resolve => setImmediate(resolve))

const stored = [
  { role: 'user', content: 'first', row_id: 11 },
  { role: 'assistant', content: 'a1', row_id: 12 },
  { role: 'user', content: 'second', row_id: 13 }
]

function harness() {
  const request = vi.fn(async (method: string, params: any) => {
    if (method === 'session.resume') {
      return {
        session_id: 'owner',
        revision: 9,
        execution_generation: 3,
        messages: stored,
        stored_messages: stored,
        info: { model: 'fake-model' }
      }
    }

    if (method === 'session.mutate' && params.operation === 'delete_messages') {
      return {
        session_id: 'owner',
        operation: 'delete_messages',
        revision: 10,
        execution_generation: 4,
        deleted_ids: params.payload.message_ids,
        deleted_count: params.payload.message_ids.length,
        new_head_id: 13
      }
    }

    throw new Error(`unknown_method: ${method}`)
  })

  const sys = vi.fn()
  const setHistoryItems = vi.fn()

  const ctx = {
    slashFlightRef: { current: 0 },
    gateway: { gw: { isCanonical: true, request }, rpc: request },
    transcript: { page: vi.fn(), panel: vi.fn(), send: vi.fn(), sys, setHistoryItems, trimLastExchange: (items: unknown[]) => items },
    local: { getHistoryItems: () => [], getLastUserMsg: () => '', maybeWarn: vi.fn() },
    session: { resumeById: vi.fn() },
    composer: { enqueue: vi.fn() }
  }

  return { request, setHistoryItems, slash: createSlashHandler(ctx as any), sys }
}

beforeEach(() => {
  resetUiState()
  resetOverlayState()
  patchUiState({ sid: 'owner', info: { model: 'm', skills: {}, tools: {}, execution_epoch: '1', execution_generation: 3 } as any })
})

it('/select opens the overlay; the commit sends the fenced mutation and repaints', async () => {
  const { request, setHistoryItems, slash, sys } = harness()

  slash('/select')
  await flush()

  const req = getOverlayState().messageSelector

  expect(req?.onCommit).toBeTypeOf('function')

  // Exactly what the panel sends: deleteMessagesPayload of the user's toggles.
  await req!.onCommit(deleteMessagesPayload(new Set([12, 11]))!.message_ids)
  await flush()

  const mutations = request.mock.calls.filter(([method, params]) => method === 'session.mutate' && params.operation === 'delete_messages')

  expect(mutations).toHaveLength(1)
  expect(mutations[0][1]).toMatchObject({
    expected_revision: 9,
    expected_generation: 3,
    // Ascending row ids, whatever order the selection held.
    payload: { message_ids: [11, 12] }
  })
  // The committed receipt repaints from the fresh snapshot, like a compress.
  expect(setHistoryItems).toHaveBeenCalled()
  expect(sys).toHaveBeenCalledWith(expect.stringContaining('deleted 2'))
  expect(getOverlayState().messageSelector).toBeNull()
})

it('a structured refusal keeps the overlay open with nothing repainted', async () => {
  const { request, setHistoryItems, slash, sys } = harness()

  slash('/select')
  await flush()

  const req = getOverlayState().messageSelector!

  request.mockImplementationOnce(async () => {
    throw Object.assign(new Error('session_busy'), { data: { reason: 'session_busy' } })
  })

  await expect(req.onCommit([11])).rejects.toThrow('session_busy')

  expect(setHistoryItems).not.toHaveBeenCalled()
  expect(sys).not.toHaveBeenCalledWith(expect.stringContaining('deleted'))
  // The commit refused: the selector stays up so the user can retry or cancel.
  expect(getOverlayState().messageSelector).not.toBeNull()
})
