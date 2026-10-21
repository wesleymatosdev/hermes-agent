import { Box, Text, useInput } from '@hermes/ink'
import { useEffect, useState } from 'react'

import {
  deleteMessagesPayload,
  moveSelection,
  type SelectableRow,
  selectableRows,
  toggleSelection
} from '../app/messageSelection.js'
import { patchOverlayState } from '../app/overlayStore.js'
import type { MessageSelectorReq } from '../app/slash/canonicalMessageSelection.js'
import { t } from '../i18n/runtime.js'
import type { Theme } from '../theme.js'

import { windowItems } from './overlayControls.js'

const VISIBLE = 12

const KIND_MARK: Record<SelectableRow['kind'], string> = {
  assistant: '●',
  other: '·',
  tool: '⚙',
  user: '▸'
}

/**
 * The /select panel: keyboard multi-select over the session's stored transcript
 * rows, a destructive confirm, then the shared `delete_messages` mutation. The
 * committed repaint and the CAS tuple live in canonicalMessageSelection.ts; this
 * component only renders the rows and routes keys, like every floating panel.
 */
export function MessageSelector({
  gw,
  maxWidth,
  req,
  t: theme,
  onCancel
}: {
  gw: { request: (method: string, params?: Record<string, unknown>) => Promise<unknown> }
  maxWidth: number
  req: MessageSelectorReq
  t: Theme
  onCancel: () => void
}) {
  const [rows, setRows] = useState<SelectableRow[]>([])
  const [selected, setSelected] = useState<ReadonlySet<number>>(() => new Set())
  const [sel, setSel] = useState(0)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true

    gw.request('session.resume')
      .then(raw => {
        if (!alive) {
          return
        }

        const snapshot = (raw ?? {}) as { stored_messages?: Array<Record<string, unknown>>; messages?: Array<Record<string, unknown>> }
        const offered = selectableRows(snapshot.stored_messages ?? snapshot.messages ?? [])

        setRows(offered)
        setSel(Math.max(0, offered.length - 1))
      })
      .catch((error: unknown) => {
        if (alive) {
          setErr(error instanceof Error ? error.message : String(error))
        }
      })

    return () => {
      alive = false
    }
  }, [gw])

  const commit = () => {
    const payload = deleteMessagesPayload(selected)

    if (!payload || busy) {
      return
    }

    setBusy(true)
    setErr('')
    req
      .onCommit(payload.message_ids)
      .then(() => onCancel())
      .catch((error: unknown) => {
        setBusy(false)
        setErr(error instanceof Error ? error.message : String(error))
      })
  }

  useInput((ch, key) => {
    const lower = ch.toLowerCase()

    if (key.escape || (key.ctrl && lower === 'c') || lower === 'q') {
      return onCancel()
    }

    if (busy) {
      return
    }

    if (key.upArrow || lower === 'k') {
      return setSel(prev => moveSelection(prev, -1, rows.length))
    }

    if (key.downArrow || lower === 'j') {
      return setSel(prev => moveSelection(prev, 1, rows.length))
    }

    if (ch === ' ' && rows[sel]) {
      return setSelected(prev => toggleSelection(prev, rows[sel]!.rowId))
    }

    if (key.return) {
      // The destructive confirm is the shared prompt (it takes the keyboard from
      // this panel and closes itself); Enter here only opens it.
      return patchOverlayState({
        confirm: {
          cancelLabel: t('canonical.selector.cancel'),
          confirmLabel: t('canonical.selector.delete'),
          danger: true,
          detail: t('canonical.selector.confirmDetail'),
          onConfirm: commit,
          title: t('canonical.selector.confirmTitle', String(selected.size))
        }
      })
    }
  })

  const { items, offset } = windowItems(rows, sel, VISIBLE)

  return (
    <Box borderStyle="round" flexDirection="column" maxWidth={maxWidth} paddingX={1}>
      <Text bold color={theme.color.text}>
        {t('canonical.selector.title')}{' '}
        <Text color={theme.color.muted}>{t('canonical.selector.hint')}</Text>
      </Text>

      {err ? <Text color={theme.color.error}>{err}</Text> : null}

      {!rows.length && !err ? <Text color={theme.color.muted}>{t('canonical.selector.empty')}</Text> : null}

      {items.map((row, index) => {
        const active = offset + index === sel
        const checked = selected.has(row.rowId)

        return (
          <Box key={row.rowId}>
            <Text
              backgroundColor={active ? theme.color.border : undefined}
              color={checked ? theme.color.error : active ? theme.color.text : theme.color.muted}
              wrap="truncate-end"
            >
              {checked ? '[x] ' : '[ ] '}
              {KIND_MARK[row.kind]} {row.preview}
            </Text>
          </Box>
        )
      })}

      <Text color={selected.size ? theme.color.error : theme.color.muted}>
        {selected.size
          ? t('canonical.selector.confirmHint', String(selected.size))
          : t('canonical.selector.noneSelected')}
      </Text>
    </Box>
  )
}
