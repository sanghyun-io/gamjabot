import {useEffect, useRef, useState} from 'react'
import {useI18n} from '../i18n'

export default function EditorDraftNotice({restored, busy, revision, onDiscard}: {restored: boolean; busy: boolean; revision: string; onDiscard: () => void}) {
  const {t} = useI18n()
  const [review, setReview] = useState<string | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const armed = useRef<string | null>(null)
  const last = useRef({revision, busy, restored})
  if (last.current.revision !== revision || last.current.busy !== busy || last.current.restored !== restored) {
    armed.current = null
    last.current = {revision, busy, restored}
  }
  useEffect(() => {setReview(null)}, [revision, busy, restored])
  if (!restored) return null
  const reviewing = !busy && review === revision && armed.current === revision
  const cancel = () => {armed.current = null; setReview(null); trigger.current?.focus({preventScroll: true})}
  return <div className="notice notice--info stack stack--tight"><p role="status">{t('editor.draftRestored')}</p>
    <button ref={trigger} type="button" className="button button--secondary button--compact" disabled={busy} aria-expanded={reviewing} onClick={() => {if (!busy) {armed.current = revision; setReview(revision)}}}>{t('editor.discardDraft')}</button>
    {reviewing && <div className="notice notice--warning stack stack--tight">
      <p role="status">{t('editor.discardReview.help')}</p>
      <div className="page-actions">
        <button type="button" className="button button--secondary button--compact" onClick={cancel}>{t('editor.discardReview.keep')}</button>
        <button type="button" className="button button--danger button--compact" onClick={() => {
          if (busy || armed.current !== revision || review !== revision) return
          cancel(); onDiscard()
        }}>{t('editor.discardReview.confirm')}</button>
      </div>
    </div>}
  </div>
}
