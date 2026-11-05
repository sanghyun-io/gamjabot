import { useEffect, useRef, useState } from 'react'
import { mcpDraftApi, type ActionDraft, type DraftReceipt, type DraftValue } from '../api/mcpDraft'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'

type Pending = { key: string; operation: 'save' | 'apply'; revision: number; review: string }
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
const valid = (d: ActionDraft, action: string, review: string) => {
  if (!d || d.version !== 1 || d.action_id !== action || d.review_id !== review || !Number.isSafeInteger(d.revision) || d.revision < 0 || typeof d.available !== 'boolean' || typeof d.pending !== 'boolean' || !Array.isArray(d.fields) || !d.values || !d.edits || typeof d.values !== 'object' || typeof d.edits !== 'object') return false
  const paths = new Set<string>()
  for (const f of d.fields) {
    if (!f || typeof f.path !== 'string' || !f.path.startsWith('/') || paths.has(f.path) || typeof f.label !== 'string' || typeof f.role !== 'string' || !['string', 'string_array'].includes(f.type) || !Number.isSafeInteger(f.max) || f.max < 0 || (f.type === 'string_array' && (!Number.isSafeInteger(f.max_items) || (f.max_items ?? -1) < 0))) return false
    paths.add(f.path)
    for (const v of [d.values[f.path], d.edits[f.path]]) if (v !== undefined && !(f.type === 'string' ? typeof v === 'string' : Array.isArray(v) && v.every(x => typeof x === 'string'))) return false
  }
  return Object.keys(d.values).every(k => paths.has(k)) && Object.keys(d.edits).every(k => paths.has(k))
}

export default function McpDraftEditor({ roomId, actionId, reviewId, scopeKey, onBlocked, onApplied }: { roomId: number; actionId: string; reviewId: string; scopeKey: string; onBlocked: (blocked: boolean) => void; onApplied: () => void }) {
  const { t } = useI18n()
  const owner = useAuthStore(s => s.user?.id ?? null), epoch = useAuthStore(s => s.epoch)
  const storageKey = `mcp-draft-command:${owner}:${roomId}:${actionId}`
  const currentScope = useRef(scopeKey); currentScope.current = scopeKey
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const current = () => alive.current && currentScope.current === scopeKey && useAuthStore.getState().epoch === epoch && useAuthStore.getState().user?.id === owner
  const [draft, setDraft] = useState<ActionDraft | null>(null)
  const [form, setForm] = useState<Record<string, DraftValue>>({})
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('')
  const [pending, setPending] = useState<Pending | null>(() => { try { const value = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); return value && typeof value.key === 'string' && ['save','apply'].includes(value.operation) && Number.isSafeInteger(value.revision) && typeof value.review === 'string' ? value : null } catch { return null } })
  const lock = useRef(false)
  const load = async (review = reviewId, revision?: number) => {
    const value = await mcpDraftApi.read(roomId, actionId)
    if (!current()) return
    if (!valid(value, actionId, review) || (revision !== undefined && value.revision !== revision)) throw new Error('draft_changed')
    setDraft(value); setForm({ ...value.edits }); setError('')
  }
  useEffect(() => { let active = true; onBlocked(true); setDraft(null); setOpen(false); setError('')
    if (!pending) void mcpDraftApi.read(roomId, actionId).then(value => { if (!active || !current()) return; if (!valid(value, actionId, reviewId)) throw new Error('draft_changed'); setDraft(value); setForm({ ...value.edits }) }).catch(() => { if (active && current()) setError('unavailable') })
    return () => { active = false }
  }, [scopeKey])
  const dirty = !!draft && !same(form, draft.edits)
  useEffect(() => { onBlocked(!draft || open || busy || !!pending || !!error || dirty || draft.pending) }, [draft, open, busy, pending, error, dirty, scopeKey])
  const receipt = async (p: Pending, value: DraftReceipt) => {
    if (!current()) return
    if (!value || value.version !== 1 || value.operation !== p.operation || value.state !== (p.operation === 'save' ? 'saved' : 'applied') || value.command_key !== p.key || value.action_id !== actionId || !Number.isSafeInteger(value.revision) || value.revision !== p.revision + 1 || typeof value.review_id !== 'string' || (p.operation === 'save' ? value.review_id !== p.review : value.review_id === p.review)) throw new Error('receipt_changed')
    if (p.operation === 'apply') { sessionStorage.removeItem(storageKey); setPending(null); setDraft(null); setOpen(false); setError('refresh'); onApplied(); return }
    await load(p.review, value.revision)
    if (!current()) return
    sessionStorage.removeItem(storageKey); setPending(null); setOpen(false)
  }
  const run = async (operation: 'save' | 'apply', edits?: Record<string, DraftValue>) => {
    if (!draft || typeof draft.review_id !== 'string' || !current() || pending || lock.current) return
    const p: Pending = { key: crypto.randomUUID(), operation, revision: draft.revision, review: draft.review_id }
    onBlocked(true)
    try { sessionStorage.setItem(storageKey, JSON.stringify(p)) } catch { setDraft(null); setError('unavailable'); return }
    lock.current = true; setBusy(true); setError(''); setPending(p)
    try { const result = operation === 'save' ? await mcpDraftApi.save(roomId, actionId, p.revision, p.review, edits || {}, p.key) : await mcpDraftApi.apply(roomId, actionId, p.revision, p.review, p.key); await receipt(p, result) }
    catch (cause) { if (current()) { const status = (cause as { response?: { status?: number } }).response?.status; if (status === 409) { sessionStorage.removeItem(storageKey); setPending(null); setDraft(null); setError('conflict') } else if (status === 422) { sessionStorage.removeItem(storageKey); setPending(null); setError('invalid') } else setError('unknown') } } finally { lock.current = false; if (current()) setBusy(false) }
  }
  const recover = async () => {
    if (!pending || lock.current || !current()) return
    lock.current = true; setBusy(true)
    try { await receipt(pending, await mcpDraftApi.command(roomId, actionId, pending.key)) } catch { if (current()) setError('unknown') } finally { lock.current = false; if (current()) setBusy(false) }
  }
  const set = (path: string, value: DraftValue) => { onBlocked(true); setError(''); setForm(old => ({ ...old, [path]: value })) }
  const values = draft ? { ...draft.values, ...form } : {}
  const fieldLabel = (label: string) => { const key = label.toLowerCase(); return ['to','recipient','recipients'].includes(key) ? t('mcpDraft.recipients') : key === 'subject' ? t('mcpDraft.subject') : key === 'body' ? t('mcpDraft.body') : key === 'text' ? t('mcpDraft.message') : label }
  return <section className="stack stack--tight" aria-label={t('mcpDraft.title')}>
    {error && <p role="alert">{t(error === 'unknown' ? 'mcpDraft.unknown' : error === 'refresh' ? 'mcpDraft.refresh' : error === 'conflict' ? 'mcpDraft.conflict' : error === 'invalid' ? 'mcpDraft.invalid' : 'mcpDraft.unavailable')}</p>}
    {busy && <p role="status">{t('mcpDraft.saving')}</p>}
    {pending && !busy && !error && <p role="status">{t('mcpDraft.unknown')}</p>}
    {error === 'conflict' && !pending && <button type="button" className="button button--secondary button--compact" onClick={onApplied}>{t('mcpDraft.check')}</button>}
    {pending && <button type="button" className="button button--secondary button--compact" disabled={busy} onClick={recover}>{t('mcpDraft.check')}</button>}
    {draft && !pending && <>
      {!draft.available || !draft.fields.length ? <p className="section-copy">{t('mcpDraft.noFields')}</p> : <>
        {!open && <div className="stack stack--tight">{draft.fields.map(f => <div key={f.path}><strong>{fieldLabel(f.label)}</strong><div style={{whiteSpace:'pre-wrap', overflowWrap:'anywhere'}}>{Array.isArray(values[f.path]) ? (values[f.path] as string[]).join(', ') : values[f.path] || '—'}</div></div>)}</div>}
        {!open && <button type="button" className="button button--secondary button--compact" disabled={busy} onClick={() => { onBlocked(true); setOpen(true) }}>{t('mcpDraft.edit')}</button>}
        {open && <div className="stack stack--tight">{draft.fields.map(f => <fieldset key={f.path} className="field"><legend>{fieldLabel(f.label)}</legend>{f.type === 'string_array' ? <span className="stack stack--tight">{(Array.isArray(values[f.path]) ? values[f.path] as string[] : []).map((v, index, list) => <span key={index} className="page-actions"><input aria-label={fieldLabel(f.label)} maxLength={f.max} value={v} onChange={e => set(f.path, list.map((x, i) => i === index ? e.target.value : x))} /><button type="button" className="button button--secondary button--compact" onClick={() => set(f.path, list.filter((_, i) => i !== index))}>{t('mcpDraft.remove')}</button></span>)}<button type="button" className="button button--secondary button--compact" disabled={(Array.isArray(values[f.path]) ? (values[f.path] as string[]).length : 0) >= (f.max_items || 0)} onClick={() => set(f.path, [...(Array.isArray(values[f.path]) ? values[f.path] as string[] : []), ''])}>{t('mcpDraft.add')}</button></span> : f.role === 'body' || /^(body|text|content|message)$/i.test(f.label) ? <textarea aria-label={fieldLabel(f.label)} maxLength={f.max} value={typeof values[f.path] === 'string' ? values[f.path] as string : ''} onChange={e => set(f.path, e.target.value)} /> : <input aria-label={fieldLabel(f.label)} maxLength={f.max} value={typeof values[f.path] === 'string' ? values[f.path] as string : ''} onChange={e => set(f.path, e.target.value)} />}</fieldset>)}<p className="field-hint">{t('mcpDraft.arrayHint')}</p><div className="page-actions"><button type="button" className="button button--secondary" disabled={busy || !dirty} onClick={() => void run('save', form)}>{t('mcpDraft.save')}</button><button type="button" className="button button--secondary" disabled={busy} onClick={() => { setForm({ ...draft.edits }); setOpen(false) }}>{t('mcpDraft.cancel')}</button></div></div>}
      </>}
      {!open && draft.pending && <div className="stack stack--tight"><p>{t('mcpDraft.saved')}</p><div className="page-actions"><button type="button" className="button button--primary" disabled={busy} onClick={() => void run('apply')}>{t('mcpDraft.apply')}</button><button type="button" className="button button--secondary" disabled={busy} onClick={() => void run('save', {})}>{t('mcpDraft.original')}</button></div></div>}
    </>}
  </section>
}
