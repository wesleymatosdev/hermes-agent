// Per-message delete (the TUI's /select) over the shared canonical mutation:
// `session.mutate operation=delete_messages` (`hermes_state_mutations.py`).
// The selection state machine lives in app/messageSelection.ts; this module is
// the canonical plumbing — CAS tuple, destructive confirm, and the same
// fresh-snapshot transcript repaint a compress applies.

import { introMsg, toTranscriptMessages } from '../../domain/messages.js'
import { t } from '../../i18n/runtime.js'
import { asRpcResult } from '../../lib/rpc.js'
import { patchOverlayState } from '../overlayStore.js'

import { mutateCanonical } from './canonicalSessionControls.js'
import type { SlashRunCtx } from './types.js'

/** Opened by /select; the overlay collects rows, this callback commits them. */
export interface MessageSelectorReq {
  onCommit: (messageIds: number[]) => Promise<void>
}

export function openMessageSelector(ctx: SlashRunCtx) {
  const sid = ctx.sid

  if (!sid) {
    return ctx.transcript.sys(t('slashCmd.core.status.noActiveSession'))
  }

  patchOverlayState({
    messageSelector: {
      onCommit: async (messageIds: number[]) => {
        const gw = ctx.gateway.gw

        // The receipt transaction re-validates every row against the live transcript; a
        // selection the session already outgrew refuses without archiving anything.
        const mutation = await mutateCanonical(gw, sid, 'delete_messages', { message_ids: messageIds }, ctx.stale)

        if (!mutation?.result || ctx.stale()) {
          return
        }

        // Same repaint a committed compress applies: the fresh snapshot, not a local guess.
        const fresh = asRpcResult<{ info?: Record<string, unknown>; messages?: Array<Record<string, unknown>> }>(
          await gw.request('session.resume', { session_id: sid })
        )

        if (ctx.stale()) {
          return
        }

        if (!fresh || !Array.isArray(fresh.messages)) {
          throw new Error(t('session.common.invalidResponse', 'session.resume'))
        }

        const info = { ...ctx.ui.info, ...fresh.info }

        ctx.transcript.setHistoryItems([introMsg(info), ...toTranscriptMessages(fresh.messages)])
        patchOverlayState({ messageSelector: null })

        const deleted = Number(mutation.result.deleted_count ?? 0)

        ctx.transcript.sys(
          t(deleted === 1 ? 'canonical.selector.deletedOne' : 'canonical.selector.deletedOther', String(deleted))
        )
      }
    }
  })
}
