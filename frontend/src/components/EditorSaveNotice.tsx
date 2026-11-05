import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'

export type EditorSavePhase = 'idle' | 'saving' | 'rejected' | 'unknown'
export type SavedItem = { id: number; name: string; description?: string | null }
export function editorSaveFailure(error: unknown): EditorSavePhase {
  const status = (error as { response?: { status?: number } })?.response?.status
  return status && [400, 401, 403, 404, 409, 413, 415, 422, 429].includes(status) ? 'rejected' : 'unknown'
}

export default function EditorSaveNotice<TItem extends SavedItem = SavedItem>({ phase, identity, onInspect, renderItem }: {
  phase: EditorSavePhase; identity: string; onInspect: () => Promise<TItem[]>; renderItem?: (item: TItem) => ReactNode
}) {
  const { t } = useI18n()
  const [reading, setReading] = useState(false), [failed, setFailed] = useState(false)
  const [items, setItems] = useState<TItem[] | null>(null)
  const generation = useRef(0), pending = useRef(false)
  const sameOwner = () => `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}` === identity
  useEffect(() => {
    generation.current += 1; pending.current = false; setReading(false); setFailed(false); setItems(null)
    return () => { generation.current += 1 }
  }, [phase, identity])
  const inspect = async () => {
    if (pending.current || !sameOwner()) return
    const run = ++generation.current
    pending.current = true; setReading(true); setFailed(false)
    try {
      const value = await onInspect()
      if (run === generation.current && sameOwner()) setItems(value)
    } catch { if (run === generation.current && sameOwner()) setFailed(true) }
    finally { if (run === generation.current && sameOwner()) { pending.current = false; setReading(false) } }
  }
  if (phase === 'idle') return null
  return <div className={`notice ${phase === 'saving' ? 'notice--info' : 'notice--warning'} stack stack--tight`}>
    <p role={phase === 'saving' ? 'status' : 'alert'}>{t(phase === 'saving' ? 'editor.saving' : phase === 'rejected' ? 'editor.rejected' : 'editor.unknown')}</p>
    {phase === 'unknown' && <>
      <button type="button" className="button button--secondary button--compact" disabled={reading} onClick={() => void inspect()}>{t(reading ? 'common.loading' : 'editor.inspect')}</button>
      {failed && <p role="alert">{t('editor.inspectFailure')}</p>}
      {items !== null && <div className="stack stack--tight"><p role="status">{t('editor.inspectHelp')}</p>
        {(reading || failed) && <p role="status" className="metadata">{t('editor.previousObservation')}</p>}

        {items.length ? <ul className="stack stack--tight">{items.map((item) => <li key={item.id}>{renderItem ? renderItem(item) : <strong>#{item.id} · {item.name}</strong>}{item.description && <p>{item.description}</p>}</li>)}</ul> : <p>{t('editor.noMatch')}</p>}
      </div>}
    </>}
  </div>
}
