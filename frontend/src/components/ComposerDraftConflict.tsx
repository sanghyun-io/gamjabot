import { useEffect, useId, useRef, useState } from 'react'
import type { RoomDraft } from '../api/drafts'
import { useI18n } from '../i18n'
import { reviewTimestamp, reviewTimeOptions } from '../lib/reviewTime'

type Choice = 'saved' | 'mine'
export default function ComposerDraftConflict({ saved, content, editId, canSave, canUse, onApply, onKeep }: {
  saved: RoomDraft; content: string; editId: string; canSave: boolean; canUse: () => boolean
  onApply: (choice: Choice, saved: RoomDraft, editId: string) => Promise<void>; onKeep: () => void
}) {
  const { t, formatDateTime } = useI18n(), id = useId(), root = useRef<HTMLElement>(null)
  const alive = useRef(false), lock = useRef(false)
  const [choice, setChoice] = useState<{ kind: Choice; saved: RoomDraft; editId: string } | null>(null)
  const [busy, setBusy] = useState(false), [failed, setFailed] = useState(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  // A selection belongs to the exact received draft and input edit, not just its text.
  const selected = choice?.saved === saved && choice.editId === editId ? choice.kind : null
  const usable = () => canUse() && document.visibilityState === 'visible' && document.hasFocus()
    && !!root.current?.isConnected && !!root.current.getClientRects().length
    && !root.current.closest('[hidden], [inert], [aria-hidden="true"]')
  const apply = async () => {
    if (!selected || lock.current || !usable() || selected === 'mine' && !canSave) return
    lock.current = true; setBusy(true); setFailed(false)
    try { await onApply(selected, saved, editId) }
    catch { if (alive.current && canUse()) { setChoice(null); setFailed(true) } }
    finally { lock.current = false; if (alive.current) setBusy(false) }
  }
  const stamp = reviewTimestamp(saved.updated_at)
  return <section ref={root} className="notice notice--warning draft-conflict stack stack--tight" aria-label={t('chat.draftChoice.title')}>
    <p role="alert">{t('chat.draft.conflict')}</p>
    <h3 className="section-title">{t('chat.draftChoice.title')}</h3>
    <p id={`${id}-help`} className="field-hint">{t('chat.draftChoice.help')}</p>
    <p className="metadata">{t('chat.draftChoice.current', { count: Array.from(content).length })}</p>
    <p id={`${id}-received`} className="metadata">{t('chat.draftChoice.meta', { version: saved.version, count: Array.from(saved.content).length })}{stamp && <> · <time dateTime={stamp}>{formatDateTime(stamp, reviewTimeOptions)}</time></>}</p>
    <details className="compact-details">
      <summary>{t('chat.draftChoice.received')}</summary>
      <textarea aria-label={t('chat.draftChoice.received')} aria-describedby={`${id}-received ${id}-help`} readOnly spellCheck={false} rows={5} value={saved.content} style={{ width: '100%', maxHeight: 180 }} />
    </details>
    <fieldset disabled={busy} className="stack stack--tight" aria-describedby={`${id}-help`}>
      <legend>{t('chat.draftChoice.title')}</legend>
      <label><input type="radio" name={`${id}-choice`} checked={selected === 'saved'} onChange={() => { if (usable()) { setChoice({ kind: 'saved', saved, editId }); setFailed(false) } }} /> {t('chat.draft.loadSaved')}</label>
      <label><input type="radio" name={`${id}-choice`} checked={selected === 'mine'} disabled={!canSave} onChange={() => { if (usable() && canSave) { setChoice({ kind: 'mine', saved, editId }); setFailed(false) } }} /> {t('chat.draft.saveMine')}</label>
      <div className="inline-actions">
        <button type="button" className="button button--primary button--compact" disabled={!selected || selected === 'mine' && !canSave} onClick={() => { void apply() }}>{t(busy ? 'common.saving' : 'chat.draftChoice.apply')}</button>
        <button type="button" className="button button--secondary button--compact" onClick={() => { if (usable()) { setChoice(null); onKeep() } }}>{t('chat.draftChoice.keep')}</button>
      </div>
    </fieldset>
    {failed && <p role="alert">{t('chat.draftChoice.failure')}</p>}
  </section>
}
