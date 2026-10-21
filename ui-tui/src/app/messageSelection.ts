// Message selection state machine behind the TUI's /select overlay: the pure,
// testable half of per-message delete (`session.mutate operation=delete_messages`,
// `hermes_state_mutations.py::_delete_messages`). The overlay component
// (components/messageSelector.tsx) renders it; the canonical plumbing (CAS
// tuple, repaint) lives in app/slash/canonicalMessageSelection.ts.

/** Transcript rows that name real conversation content, not machinery. */
export interface SelectableRow {
  kind: 'assistant' | 'other' | 'tool' | 'user'
  preview: string
  rowId: number
}

/** A compaction carrier row is lineage, not content: deleting it orphans the
 *  summary chain (`agent/context_compressor.py`), so the selector never offers it. */
const COMPACTION_MARKERS = ['[CONTEXT COMPACTION', '[CONTEXT SUMMARY]:', '[END OF PRIOR CONTEXT']

const rowText = (content: unknown): string => {
  if (typeof content === 'string') {
    return content
  }

  if (Array.isArray(content)) {
    return content
      .map(part => (typeof part === 'string' ? part : typeof part?.text === 'string' ? part.text : ''))
      .join(' ')
  }

  return ''
}

const isSelectable = (row: Record<string, unknown>): boolean =>
  Number.isSafeInteger(row.row_id) &&
  (row.role === 'user' || row.role === 'assistant' || row.role === 'tool') &&
  // A display-only machinery row (internal notifications, producer cards, failed-turn
  // notices) is gateway bookkeeping; `steer` is a human-authored turn and stays selectable.
  (!row.display_kind || row.display_kind === 'steer') &&
  !COMPACTION_MARKERS.some(marker => rowText(row.content).includes(marker))

export const previewText = (row: Record<string, unknown>, max = 96): string => {
  const text = rowText(row.content).replaceAll(/\s+/g, ' ').trim()

  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

/** The rows /select offers, in transcript order (stored rows carry `row_id`). */
export function selectableRows(stored: Array<Record<string, unknown>>): SelectableRow[] {
  return stored
    .filter(isSelectable)
    .map(row => ({
      kind: row.role === 'user' ? 'user' : row.role === 'assistant' ? 'assistant' : 'tool',
      preview: previewText(row),
      rowId: row.row_id as number
    }))
}

export const toggleSelection = (selected: ReadonlySet<number>, rowId: number): Set<number> => {
  const next = new Set(selected)

  if (next.has(rowId)) {
    next.delete(rowId)
  } else {
    next.add(rowId)
  }

  return next
}

export const moveSelection = (index: number, delta: -1 | 1, count: number): number =>
  Math.max(0, Math.min(count - 1, index + delta))

/** The wire payload: ascending row ids (the store's canonical order), never empty. */
export const deleteMessagesPayload = (selected: ReadonlySet<number>): null | { message_ids: number[] } => {
  const ids = [...selected].filter(id => Number.isSafeInteger(id) && id > 0).sort((a, b) => a - b)

  return ids.length ? { message_ids: ids } : null
}
