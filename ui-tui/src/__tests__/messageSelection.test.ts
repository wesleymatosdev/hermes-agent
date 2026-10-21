import { describe, expect, it } from 'vitest'

import {
  deleteMessagesPayload,
  moveSelection,
  previewText,
  selectableRows,
  toggleSelection
} from '../app/messageSelection.js'

const rows = [
  { role: 'user', content: 'first', row_id: 11 },
  { role: 'assistant', content: 'a1', row_id: 12 },
  { role: 'user', content: '[CONTEXT COMPACTION] summary', row_id: 14 },
  { role: 'user', content: 'typed later', display_kind: 'steer', row_id: 15 },
  { role: 'internal', content: 'machinery', row_id: 16, display_kind: 'internal_notification' },
  { role: 'user', content: 'no row id' }
]

describe('selectableRows', () => {
  it('offers human-authored rows in transcript order and skips machinery', () => {
    expect(selectableRows(rows as any).map(row => row.rowId)).toEqual([11, 12, 15])
  })

  it('classifies kinds for the panel markers', () => {
    const offered = selectableRows(rows as any)

    expect(offered.map(row => row.kind)).toEqual(['user', 'assistant', 'user'])
  })

  it('flattens content parts into the preview', () => {
    expect(previewText({ content: [{ type: 'text', text: 'hello ' }, { type: 'text', text: 'world' }] })).toBe(
      'hello world'
    )
  })

  it('caps the preview with an ellipsis', () => {
    expect(previewText({ content: 'x'.repeat(200) })).toHaveLength(96)
    expect(previewText({ content: 'x'.repeat(200) })!.endsWith('…')).toBe(true)
  })
})

describe('selection state machine', () => {
  it('toggles rows without mutating the previous set', () => {
    const first = toggleSelection(new Set<number>(), 11)

    expect(first.has(11)).toBe(true)

    const second = toggleSelection(first, 11)

    expect(second.has(11)).toBe(false)
    expect(first.has(11)).toBe(true)
  })

  it('clamps the cursor at both ends', () => {
    expect(moveSelection(0, -1, 3)).toBe(0)
    expect(moveSelection(2, 1, 3)).toBe(2)
    expect(moveSelection(1, 1, 3)).toBe(2)
  })

  it('builds an ascending payload and refuses an empty selection', () => {
    expect(deleteMessagesPayload(new Set([13, 11]))).toEqual({ message_ids: [11, 13] })
    expect(deleteMessagesPayload(new Set())).toBeNull()
    expect(deleteMessagesPayload(new Set([0, -3]))).toBeNull()
  })
})
