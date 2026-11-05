import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { botsApi } from '../api/bots'
import { eventsApi } from '../api/events'
import { memoryImportApi, type MemoryImportRequest } from '../api/memoryImport'
import { readImportWorkspace, subscribeImportWorkspace, updateImportWorkspace, type ImportOperation } from '../lib/memoryImportWorkspace'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { useBotScope } from '../lib/useBotScope'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'

const hash = async (value: string) => Array.from(
  new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))),
  byte => byte.toString(16).padStart(2, '0'),
).join('')

export default function MemoryImport() {
  const { t } = useI18n()
  const scope = useBotScope(), navigate = useNavigate()
  const workspace = useSyncExternalStore(subscribeImportWorkspace, () => readImportWorkspace(scope.identity))
  const [copy, setCopy] = useState<'copied' | 'failed' | null>(null), [reading, setReading] = useState(false)
  const promptRef = useRef<HTMLTextAreaElement>(null), mounted = useRef(false), lock = useRef(false), copyLock = useRef(false)
  const generation = useRef(directoryViewGeneration()).current
  const op = workspace.operation, ime = useImeFormGuard(`${scope.identity}:${op?.key || 'draft'}`)
  const sameOwner = () => {
    const auth = useAuthStore.getState()
    return !!auth.token && auth.user?.id === scope.ownerId && auth.epoch === scope.epoch && directoryViewGeneration() === generation
  }
  const canUse = () => mounted.current && scope.canUse() && sameOwner()
  const set = (fn: Parameters<typeof updateImportWorkspace>[1]) => updateImportWorkspace(scope.identity, fn)
  const primary = useQuery({
    queryKey: ['memory-import-primary', scope.identity], retry: false,
    queryFn: async () => {
      const value = await botsApi.primary()
      if (!scope.canUse()) throw Error('Owner changed')
      return value
    },
  })
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      set(value => value.operation?.phase === 'sending'
        ? { ...value, operation: { ...value.operation, phase: 'unknown' } } : value)
    }
  }, [scope.identity])

  const prompt = t('memoryImport.prompt')
  const validPrimary = primary.isSuccess && !primary.isFetching && primary.data.availability === 'available'
    && !!primary.data.agent_id && primary.data.agent?.id === primary.data.agent_id
    && primary.data.agent.is_active && !!primary.data.conversation?.active

  const perform = async (original: ImportOperation) => {
    let accepted = false
    try {
      const receipt = await memoryImportApi.submit(original.body, original.key)
      accepted = true
      if (!sameOwner()) return
      if (receipt.room_id !== original.roomId) throw Error('Room changed')
      const event = (await eventsApi.list(receipt.room_id, receipt.seq - 1, 1))[0]
      const info = event?.payload.memory_import as Record<string, unknown> | undefined
      const digest = await hash(original.body.content)
      if (!sameOwner() || !event || event.room_id !== receipt.room_id || event.event_id !== receipt.event_id
        || event.seq !== receipt.seq || event.actor_type !== 'user' || event.actor_id !== scope.ownerId
        || event.kind !== 'message' || !info || info.version !== 1 || info.agent_id !== original.body.expected_agent_id
        || info.source_label !== original.body.source_label || info.content_sha256 !== digest) throw Error('Event mismatch')
      const updated = set(value => value.operation?.key === original.key
        ? { ...value, operation: { ...original, phase: 'submitted', receipt } } : value)
      if (updated && canUse()) {
        try { navigate(receipt.href) } catch { /* The submitted state keeps the exact link available. */ }
      }
    } catch (error) {
      if (!sameOwner()) return
      const status = (error as { response?: { status?: number } }).response?.status
      set(value => value.operation?.key !== original.key ? value : {
        ...value, operation: { ...original, phase: accepted ? 'unknown' : status === 409 ? 'conflict'
          : status && [400, 401, 403, 404, 413, 422].includes(status) ? 'rejected' : 'unknown' },
      })
    }
  }
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (ime.isSubmitBlocked(event) || !canUse() || lock.current || op || !validPrimary) return
    const source = workspace.source.trim(), content = workspace.content.trim()
    if (!source || Array.from(source).length > 120 || !content || Array.from(content).length > 12000) return
    const selected = primary.data!
    const body: MemoryImportRequest = { expected_primary_revision: selected.revision,
      expected_agent_id: selected.agent_id!, source_label: source, content }
    const request: ImportOperation = { key: crypto.randomUUID(), body: Object.freeze(body),
      roomId: selected.conversation!.room_id, agentName: selected.agent!.name, phase: 'sending' }
    lock.current = true
    if (set(value => value.operation ? value : { ...value, operation: request })) {
      void perform(request).finally(() => { lock.current = false })
    } else lock.current = false
  }
  const retry = () => {
    if (!canUse() || lock.current || op?.phase !== 'unknown') return
    lock.current = true
    const original = { ...op, phase: 'sending' as const }
    if (set(value => value.operation?.key === op.key && value.operation.phase === 'unknown'
      ? { ...value, operation: original } : value)) {
      void perform(original).finally(() => { lock.current = false })
    } else lock.current = false
  }
  const reread = async () => {
    if (!canUse() || reading) return
    setReading(true)
    try {
      const value = await primary.refetch({ cancelRefetch: false })
      if (canUse() && value.isSuccess) set(current => current.operation?.phase === 'conflict' || current.operation?.phase === 'rejected'
        ? { ...current, operation: null } : current)
    } finally { if (canUse()) setReading(false) }
  }
  const copyPrompt = async () => {
    if (!canUse() || copyLock.current) return
    copyLock.current = true; setCopy(null)
    try {
      if (!navigator.clipboard?.writeText) throw Error('Unavailable')
      await navigator.clipboard.writeText(prompt)
      if (canUse()) setCopy('copied')
    } catch { if (canUse()) setCopy('failed') }
    finally { copyLock.current = false }
  }

  return <main className="page-frame page-frame--narrow page-content stack">
    <header className="page-heading"><div><h1 className="page-title">{t('memoryImport.title')}</h1><p className="section-copy">{t('memoryImport.intro')}</p></div></header>
    <section className="stack stack--tight">
      <h2 className="section-title">{t('memoryImport.promptTitle')}</h2>
      <p className="metadata">{t('memoryImport.promptHelp')}</p>
      <textarea ref={promptRef} className="field" rows={4} value={prompt} readOnly aria-label={t('memoryImport.promptTitle')}/>
      <div className="page-actions">
        <button type="button" className="button button--secondary button--compact" onClick={() => void copyPrompt()}>{t('memoryImport.copy')}</button>
        <button type="button" className="text-link" onClick={() => { if (canUse()) { promptRef.current?.focus(); promptRef.current?.select() } }}>{t('memoryImport.select')}</button>
      </div>
      {copy && <p role="status">{t(copy === 'copied' ? 'memoryImport.copied' : 'memoryImport.copyFailed')}</p>}
    </section>
    {primary.isPending && <p role="status">{t('common.loading')}</p>}
    {primary.isError && <p role="alert">{t('memoryImport.readFailed')} <button type="button" className="text-link" onClick={() => void reread()}>{t('common.retry')}</button></p>}
    {primary.isSuccess && !validPrimary && !primary.isFetching && <p role="status">{t('memoryImport.noPrimary')} <Link to="/agents">{t('memoryImport.manage')}</Link></p>}
    {op ? <p>{t('memoryImport.target', { name: op.agentName })}</p> : validPrimary && <p>{t('memoryImport.target', { name: primary.data!.agent!.name })}</p>}
    <form {...ime.handlers} className="stack stack--tight" onSubmit={submit}>
      <label className="field">{t('memoryImport.source')}<input value={workspace.source} disabled={!!op}
        onChange={event => { const source = event.target.value; set(value => value.operation ? value : { ...value, source }) }}/></label>
      <label className="field">{t('memoryImport.answer')}<textarea rows={8} value={workspace.content} disabled={!!op}
        onChange={event => { const content = event.target.value; set(value => value.operation ? value : { ...value, content }) }}/></label>
      <p className="metadata">{t('memoryImport.unverified')}</p>
      <p className="metadata">{t('memoryImport.limits', { source: Array.from(workspace.source.trim()).length, content: Array.from(workspace.content.trim()).length })}</p>
      {!op && <button className="button button--primary" type="submit" disabled={!validPrimary || !workspace.source.trim()
        || Array.from(workspace.source.trim()).length > 120 || !workspace.content.trim()
        || Array.from(workspace.content.trim()).length > 12000}>{t('memoryImport.submit')}</button>}
    </form>
    {op && <section className="stack stack--tight" aria-live="polite">
      <p role={['unknown', 'conflict', 'rejected'].includes(op.phase) ? 'alert' : 'status'}>{t(`memoryImport.${op.phase}`)}</p>
      {op.phase === 'unknown' && <button type="button" className="button button--secondary button--compact" onClick={retry}>{t('memoryImport.retryOriginal')}</button>}
      {['conflict', 'rejected'].includes(op.phase) && <button type="button" className="text-link" disabled={reading} onClick={() => void reread()}>{t('memoryImport.readAgain')}</button>}
      {op.phase === 'submitted' && op.receipt && <Link className="button button--primary button--compact" to={op.receipt.href}
        onClick={event => { if (!canUse()) event.preventDefault() }}>{t('memoryImport.openChat')}</Link>}
      {op.phase === 'submitted' && <button type="button" className="text-link" onClick={() => {
        if (canUse()) set(() => ({ source: '', content: '', operation: null }))
      }}>{t('memoryImport.another')}</button>}
    </section>}
  </main>
}
