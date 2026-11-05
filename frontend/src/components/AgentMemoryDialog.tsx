import { useId, useLayoutEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { botMemoriesApi, type BotMemoryCategory, type BotMemoryDelete, type BotMemoryEdit, type BotMemoryEntry, type BotMemorySource, type BotMemoryWrite } from '../api/botMemories'
import { useBotScope } from '../lib/useBotScope'
import { useDialogFocus } from '../lib/useDialogFocus'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { botMemoryWorkspaceKey, readBotMemoryWorkspace, subscribeBotMemoryWorkspace, updateBotMemoryWorkspace, type BotMemoryOperation } from '../lib/botMemoryWorkspace'
import { canonicalEventId } from '../lib/peerReply'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import ServerTime from './ServerTime'
import MemorySharing, { MemoryVisibility } from './MemorySharing'

function sourceOf(row: BotMemoryEntry): BotMemorySource | null {
  if (row.source_kind === 'manual') return { kind: 'manual' }
  if (row.source_kind === 'event' && row.source_room_id && row.source_seq && canonicalEventId(row.source_event_id)) return { kind: 'event', room_id: row.source_room_id, event_id: row.source_event_id, seq: row.source_seq }
  if (row.source_kind === 'link' && row.source_url) return { kind: 'link', url: row.source_url }
  return null
}
function sourceUrl(value: string | null) {
  try { const parsed = new URL(value || ''); return ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : null } catch { return null }
}

export default function AgentMemoryDialog({ agentId, name, onClose }: { agentId: number; name: string; onClose: () => void }) {
  const { t, formatNumber } = useI18n(), scope = useBotScope(), cache = useQueryClient(), title = useId(), help = useId()
  const root = useRef<HTMLDivElement>(null), backdrop = useRef<HTMLDivElement>(null)
  const [oldestFirst, setOldestFirst] = useState(false)
  const [removeTarget, setRemoveTarget] = useState<{ row: BotMemoryEntry; scopeRevision: number } | null>(null)
  const key = botMemoryWorkspaceKey(scope.identity, agentId)
  const workspace = useSyncExternalStore(subscribeBotMemoryWorkspace, () => readBotMemoryWorkspace(key))
  const { editor, operation, notice } = workspace
  const queryKey = ['bot-memories', scope.ownerId, scope.epoch, agentId]
  const query = useQuery({ queryKey, retry: false, gcTime: 0, queryFn: async () => {
    const value = await botMemoriesApi.list(agentId)
    if (!scope.canUse()) throw new Error('Bot memory view changed')
    return value
  } })
  const locked = !!operation, fresh = query.isSuccess && !query.isFetching && !query.isError
  const currentEntry = editor?.memoryId ? query.data?.items.find(row => row.memory_id === editor.memoryId) : null
  const changed = !!editor && !!query.data && (editor.scopeRevision !== query.data.revision
    || editor.memoryId !== null && currentEntry?.revision !== editor.entryRevision)
  const size = Array.from(editor?.content || '').length
  const ime = useImeFormGuard(`${key}:${editor?.id || ''}`)
  useDialogFocus(root, onClose)
  useLayoutEffect(() => {
    const overflow = document.body.style.overflow
    const background = [...document.body.children].filter(node => node !== backdrop.current)
      .map(node => ({ node, inert: node.hasAttribute('inert'), hidden: node.getAttribute('aria-hidden') }))
    for (const { node } of background) { node.setAttribute('inert', ''); node.setAttribute('aria-hidden', 'true') }
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = overflow
      for (const item of background) {
        if (!item.inert) item.node.removeAttribute('inert')
        if (item.hidden === null) item.node.removeAttribute('aria-hidden'); else item.node.setAttribute('aria-hidden', item.hidden)
      }
    }
  }, [])
  const read = async () => {
    if (!scope.canUse() || operation?.phase === 'sending') return
    const result = await query.refetch({ cancelRefetch: false })
    if (scope.canUse() && result.isSuccess && readBotMemoryWorkspace(key).operation?.phase === 'rejected') updateBotMemoryWorkspace(key, value => ({ ...value, operation: null }))
  }
  const edit = (row?: BotMemoryEntry) => {
    if (!scope.canUse() || !fresh || locked || editor || removeTarget) return
    const source = row ? sourceOf(row) : { kind: 'manual' as const }
    if (!source) return
    updateBotMemoryWorkspace(key, value => value.operation || value.editor ? value : ({ ...value, notice: null, editor: {
      id: crypto.randomUUID(), memoryId: row?.memory_id || null, category: row?.category || 'fact', content: row?.content || '', source,
      scopeRevision: query.data!.revision, entryRevision: row?.revision || null,
    } }))
  }
  const perform = async (request: BotMemoryOperation) => {
    try {
      if (request.kind === 'add') await botMemoriesApi.add(agentId, request.body as BotMemoryWrite, request.key)
      else if (request.kind === 'edit') await botMemoriesApi.edit(agentId, request.memoryId!, request.body as BotMemoryEdit, request.key)
      else await botMemoriesApi.remove(agentId, request.memoryId!, request.body as BotMemoryDelete, request.key)
      updateBotMemoryWorkspace(key, value => value.operation?.key !== request.key ? value : ({ ...value, operation: null,
        editor: request.editorId === value.editor?.id ? null : value.editor, notice: request.kind === 'remove' ? 'removed' : 'saved' }))
      const auth = readAuthIdentity()
      if (auth === scope.identity) void cache.invalidateQueries({ queryKey })
    } catch (failure) {
      const response = (failure as { response?: { status?: number; data?: { detail?: { code?: string } } } }).response
      const rejected = !!response?.status && [400, 401, 403, 404, 409, 413, 415, 422, 429].includes(response.status)
      updateBotMemoryWorkspace(key, value => value.operation?.key !== request.key ? value : ({ ...value,
        operation: { ...request, phase: rejected ? 'rejected' : 'unknown', errorCode: response?.data?.detail?.code } }))
    }
  }
  const queue = (request: BotMemoryOperation) => {
    if (!scope.canUse()) return
    if (updateBotMemoryWorkspace(key, value => value.operation ? value : ({ ...value, operation: request, notice: null }))) void perform(request)
  }
  const save = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (ime.isSubmitBlocked(event) || !scope.canUse() || !editor || !fresh || changed || locked || !editor.content.trim() || size > 2000) return
    const body: BotMemoryWrite | BotMemoryEdit = { expected_scope_revision: editor.scopeRevision,
      category: editor.category, content: editor.content, source: Object.freeze({ ...editor.source }),
      ...(editor.memoryId ? { expected_revision: editor.entryRevision! } : {}) }
    queue({ key: crypto.randomUUID(), kind: editor.memoryId ? 'edit' : 'add', agentId, memoryId: editor.memoryId,
      body: Object.freeze(body), editorId: editor.id, content: editor.content, phase: 'sending' })
  }
  const remove = () => {
    if (!scope.canUse() || !removeTarget || !fresh || locked || removeTarget.scopeRevision !== query.data!.revision
      || !query.data!.items.some(row => row.memory_id === removeTarget.row.memory_id && row.revision === removeTarget.row.revision)) return
    queue({ key: crypto.randomUUID(), kind: 'remove', agentId, memoryId: removeTarget.row.memory_id, editorId: null,
      body: Object.freeze({ expected_scope_revision: removeTarget.scopeRevision, expected_revision: removeTarget.row.revision }), content: removeTarget.row.content, phase: 'sending' })
    setRemoveTarget(null)
  }
  const retry = () => {
    const original = readBotMemoryWorkspace(key).operation
    if (!scope.canUse() || original?.phase !== 'unknown') return
    const next = { ...original, phase: 'sending' as const }
    if (updateBotMemoryWorkspace(key, value => value.operation?.key === original.key && value.operation.phase === 'unknown' ? { ...value, operation: next } : value)) void perform(next)
  }
  const errorKey = operation?.errorCode === 'bot_memory_busy' ? 'botMemory.busy'
    : operation?.errorCode === 'bot_memory_revision_conflict' ? 'botMemory.changed'
      : ['bot_memory_context_too_large', 'bot_memory_too_many_entries'].includes(operation?.errorCode || '') ? 'botMemory.limit' : 'botMemory.rejected'
  return createPortal(<div ref={backdrop} className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack bot-memory-dialog">
    <header className="split"><h2 id={title} className="section-title">{t('botMemory.title')} · {name}</h2><button type="button" className="icon-button" aria-label={t('common.close')} onClick={onClose}><X size={20} aria-hidden="true" /></button></header>
    <p id={help} className="section-copy">{t('memorySharing.summary')}</p>
    <details className="compact-details"><summary>{t('botMemory.about')}</summary><p className="field-hint">{t('botMemory.authority')}</p><p className="field-hint">{t('botMemory.retention')}</p><p className="field-hint">{t('botMemory.draftSession')}</p></details>
    {query.isPending && <p role="status">{t('common.loading')}</p>}
    {query.isError && <p className="metadata" role="alert">{t('botMemory.readFailed')}{' '}<button type="button" className="text-link" disabled={query.isFetching} onClick={() => { void read() }}>{t('common.retry')}</button></p>}
    {operation && <section className="notice notice--warning stack stack--tight" aria-label={t('botMemory.request')}>
      <p role={operation.phase === 'sending' ? 'status' : 'alert'}>{t(operation.phase === 'sending' ? 'botMemory.sending' : operation.phase === 'unknown' ? 'botMemory.unknown' : errorKey)}</p>
      <details className="compact-details"><summary>{t('botMemory.requestBody')}</summary><p className="bot-memory-content">{operation.content}</p></details>
      {operation.phase === 'unknown' && <button type="button" className="button button--secondary button--compact" onClick={retry}>{t('botMemory.retryOriginal')}</button>}
      {operation.phase !== 'sending' && <button type="button" className="text-link" disabled={query.isFetching} onClick={() => { void read() }}>{t('botMemory.readAgain')}</button>}
    </section>}
    {notice && <p role="status" className="metadata">{t(notice === 'saved' ? 'botMemory.saved' : 'botMemory.removed')}</p>}
    {!editor && <div><button type="button" className="button button--secondary button--compact" disabled={!fresh || locked || !!removeTarget} onClick={() => edit()}>{t('botMemory.add')}</button></div>}
    {editor && <form {...ime.handlers} onSubmit={save} className="stack stack--tight" aria-busy={operation?.phase === 'sending'}>
      <label className="field">{t('botMemory.category')}<select value={editor.category} disabled={locked} onChange={event => { const category = event.target.value as BotMemoryCategory; updateBotMemoryWorkspace(key, value => !value.editor || value.operation ? value : ({ ...value, editor: { ...value.editor, category } })) }}>
        {(['fact', 'preference', 'role'] as const).map(category => <option key={category} value={category}>{t(`botMemory.category.${category}`)}</option>)}
      </select></label>
      <label className="field">{t('botMemory.content')}<textarea rows={4} value={editor.content} disabled={locked} aria-invalid={size > 2000 || undefined}
        onChange={event => { const content = event.target.value; updateBotMemoryWorkspace(key, value => !value.editor || value.operation ? value : ({ ...value, editor: { ...value.editor, content } })) }} /></label>
      {size > 1800 && <p className={size > 2000 ? 'metadata metadata--danger' : 'metadata'} role={size > 2000 ? 'alert' : undefined}>{t('botMemory.characterCount', { count: formatNumber(size) })}</p>}
      {editor.memoryId && <p className="field-hint">{t('botMemory.editEffect')}</p>}
      {changed && !operation && <p className="notice notice--warning" role="status">{t('botMemory.changedEditor')}</p>}
      <div className="page-actions"><button type="submit" className="button button--primary button--compact" disabled={!fresh || locked || changed || !editor.content.trim() || size > 2000}>{t('common.save')}</button>
        <button type="button" className="button button--ghost button--compact" disabled={locked} onClick={() => { if (scope.canUse()) updateBotMemoryWorkspace(key, value => value.operation ? value : ({ ...value, editor: null })) }}>{t('common.cancel')}</button></div>
    </form>}
    {!!query.data?.items.length && <button type="button" className="text-link" aria-pressed={oldestFirst} onClick={() => setOldestFirst(value => !value)}>{t(oldestFirst ? 'memoryImport.originalOrder' : 'memoryImport.oldest')}</button>}
    <div className="bot-memory-list">{(oldestFirst ? [...(query.data?.items || [])].sort((a,b) => {const left=Date.parse(a.updated_at),right=Date.parse(b.updated_at);return Number.isFinite(left) ? Number.isFinite(right) ? left-right : -1 : Number.isFinite(right) ? 1 : 0}) : query.data?.items)?.map(row => {
      const url = sourceUrl(row.source_url)
      const imported = row.source_snapshot?.memory_import as Record<string,unknown> | undefined
      const label = row.source_kind === 'event' && imported?.version === 1 && imported.agent_id === row.agent_id && typeof imported.content_sha256 === 'string' && /^[0-9a-f]{64}$/.test(imported.content_sha256) && typeof imported.source_label === 'string' && imported.source_label.trim() === imported.source_label && Array.from(imported.source_label).length <= 120 && imported.source_label.length > 0 && !/[\u0000-\u001f\u007f]/.test(imported.source_label) ? imported.source_label : null
      return <article key={row.memory_id} className="bot-memory-entry stack stack--tight">
        <header className="split"><strong className="metadata">{t(`botMemory.category.${row.category}`)}</strong><ServerTime value={row.updated_at} compact={!oldestFirst} /></header>
        {label && <p className="metadata">{t('memoryImport.origin',{source:label})}</p>}
        {Array.from(row.content).length > 240 ? <details className="compact-details"><summary>{Array.from(row.content).slice(0, 160).join('')}…</summary><p className="bot-memory-content">{row.content}</p></details> : <p className="bot-memory-content">{row.content}</p>}
        {query.data && <MemoryVisibility agentId={agentId} row={row} scopeRevision={query.data.revision} disabled={!fresh || locked || !!editor || !!removeTarget} refresh={async () => { const result = await query.refetch({ cancelRefetch: false }); if (!result.isSuccess) throw new Error('Memory read failed') }} />}
        <div className="page-actions" style={{ gap: 8, justifyContent: 'flex-start', flexWrap: 'wrap' }}>
          {row.source_kind === 'event' && row.source_room_id && row.source_seq && canonicalEventId(row.source_event_id) && <Link className="button button--ghost button--compact" to={`/chat/${row.source_room_id}?seq=${row.source_seq}&event=${row.source_event_id}`} onClick={onClose}>{t('botMemory.source')}</Link>}
          {row.source_kind === 'link' && url && <a className="button button--ghost button--compact" href={url} target="_blank" rel="noreferrer">{t('botMemory.source')}</a>}
          {row.source_kind === 'manual' && <span className="metadata" style={{ marginInlineEnd: 4 }}>{t('botMemory.manual')}</span>}
          <button type="button" className="button button--ghost button--compact" style={{ minHeight: 32 }} disabled={!fresh || locked || !!editor || !!removeTarget || !sourceOf(row)} onClick={() => edit(row)}>{t('common.edit')}</button>
          <button type="button" className="button button--ghost button--compact danger-link" style={{ minHeight: 32 }} disabled={!fresh || locked || !!editor || !!removeTarget} onClick={() => { if (scope.canUse() && query.data) setRemoveTarget({ row, scopeRevision: query.data.revision }) }}>{t('common.delete')}</button>
        </div>
        {removeTarget?.row.memory_id === row.memory_id && <div className="notice notice--warning stack stack--tight"><p>{t('botMemory.deleteEffect')}</p>
          <div className="page-actions"><button type="button" className="button button--danger button--compact" disabled={!fresh || locked || removeTarget.scopeRevision !== query.data?.revision || removeTarget.row.revision !== row.revision} onClick={remove}>{t('common.delete')}</button><button type="button" className="button button--ghost button--compact" onClick={() => setRemoveTarget(null)}>{t('common.cancel')}</button></div>
        </div>}
      </article>
    })}</div>
    {query.isSuccess && !query.data.items.length && !editor && <p className="section-copy">{t('botMemory.empty')}</p>}
    <MemorySharing agentId={agentId} onClose={onClose} />
  </div></div>, document.body)
}

// Delayed request settlement may update this login's workspace after the dialog closes.
const readAuthIdentity = () => { const state = useAuthStore.getState(); return `${state.user?.id || 0}:${state.epoch}` }
