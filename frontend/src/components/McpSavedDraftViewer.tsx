import { useRef, useState } from 'react'
import { mcpDraftApi, type DraftValue } from '../api/mcpDraft'
import { useI18n } from '../i18n'
import { useBotScope } from '../lib/useBotScope'

export default function McpSavedDraftViewer({ roomId, actionId, revision, pending, terminal }: { roomId: number; actionId: string; revision: number; pending: boolean; terminal: boolean }) {
  const { t } = useI18n(), scope = useBotScope(), flight = useRef(false)
  const [values, setValues] = useState<Record<string, DraftValue> | null>(null)
  const [unavailable, setUnavailable] = useState(false), [error, setError] = useState(false), [busy, setBusy] = useState(false)
  const read = async () => {
    if (!scope.canUse() || flight.current) return
    flight.current = true; setBusy(true); setError(false)
    try {
      const data = await mcpDraftApi.read(roomId, actionId)
      if (!scope.canUse()) return
      const edits: unknown = data?.edits
      if (data?.version !== 1 || data.action_id !== actionId || data.revision !== revision || data.pending !== pending || typeof data.available !== 'boolean'
        || !edits || typeof edits !== 'object' || Array.isArray(edits) || Object.keys(edits).length > 128) throw new Error('saved draft changed')
      const safe: Record<string, DraftValue> = Object.create(null)
      for (const [path, value] of Object.entries(edits)) {
        if (!path.startsWith('/') || path.length > 8192 || /~(?![01])/.test(path) || /[\u0000-\u001f\u007f]/.test(path)
          || !(typeof value === 'string' && value.length <= 16000 || Array.isArray(value) && value.length <= 100 && value.every(v => typeof v === 'string' && v.length <= 16000))) throw new Error('invalid saved draft')
        safe[path] = value as DraftValue
      }
      setValues(safe); setUnavailable(!data.available)
    } catch { if (scope.canUse()) { setValues(null); setError(true) } }
    finally { flight.current = false; if (scope.canUse()) setBusy(false) }
  }
  const label = (path: string) => {
    const last = (path.split('/').at(-1) || '').replace(/~1/g, '/').replace(/~0/g, '~')
    const key = last.toLowerCase()
    return ['to','recipient','recipients'].includes(key) ? t('mcpDraft.recipients') : key === 'subject' ? t('mcpDraft.subject') : key === 'body' ? t('mcpDraft.body') : key === 'text' ? t('mcpDraft.message') : last
  }
  return <section className="stack stack--tight">
    <button type="button" className="button button--secondary button--compact" disabled={busy} onClick={() => void read()}>{t('mcpSavedDraft.open')}</button>
    {error && <p role="alert">{t('mcpSavedDraft.unavailable')}</p>}
    {values && <><p className="section-copy">{t(terminal || unavailable ? 'mcpSavedDraft.ended' : 'mcpSavedDraft.readOnly')}</p>
      {Object.keys(values).length === 0 ? <p>{t('mcpSavedDraft.empty')}</p> : <dl>{Object.entries(values).map(([path, value]) => <div key={path}><dt>{label(path)}</dt><dd style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{Array.isArray(value) ? value.join('\n') : value}</dd><dd><details className="compact-details"><summary>{t('common.details')}</summary><code>{path}</code></details></dd></div>)}</dl>}</>}
  </section>
}
