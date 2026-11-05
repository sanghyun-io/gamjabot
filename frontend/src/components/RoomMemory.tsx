import type { KeyboardEvent } from 'react'
import { useViewNavigationIntent } from '../lib/useViewNavigationIntent'
import DirectorySearch, { directoryMatcher, type DirectorySearchHandle } from './DirectorySearch'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { roomMemoriesApi, type MemoryCategory, type MemoryCommand, type MemoryEntry, type MemorySource, type MemoryWrite, type MemoryList } from '../api/roomMemories'
import type { Agent } from '../api/agents'
import type { RoomEvent } from '../api/events'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { reviewTimestamp, reviewTimeOptions } from '../lib/reviewTime'
import { resultHttpUrl } from '../lib/resultSelection'
import { assistantVisibleMarkdown } from '../lib/assistantContent'
import { editorTextLength } from '../lib/editorValidation'

import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { createMemoryEditor, memoryWorkspaceLocked, readMemoryWorkspace, snapshotMemory, subscribeMemoryWorkspaces,
  updateMemoryWorkspace, type MemoryDraft, type MemoryFilters, type MemoryEditor as Editor, type MemoryEditorBase,
  type MemoryOperation, type MemoryRequest, type MemoryWorkspace } from '../lib/memoryEditorWorkspace'

interface Props { panelId?: string; labelledBy?: string; roomId: number; ownerId: number; authEpoch: number; active: boolean; visible: boolean; events: RoomEvent[]; agents: Agent[]; onChanged?: () => void; onNavigate: (seq: number, id: string) => void }
const errorKeys: Record<string, MessageKey> = { memory_room_busy: 'memory.busy', memory_revision_conflict: 'memory.conflict', memory_idempotency_conflict: 'memory.conflict', memory_context_too_large: 'memory.budget', memory_too_many_entries: 'memory.budget', memory_read_only: 'chat.closedReadOnly' }

function MemoryContent({ content }: { content: string }) {
  const { t } = useI18n()
  const points = Array.from(content)
  const body = <p className="memory-content" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{content}</p>
  if (points.length <= 240 && content.split(/\r\n|\r|\n/).length <= 4) return body
  const preview = points.slice(0, 160).join('').replace(/\s+/g, ' ').trim() + (points.length > 160 ? '…' : '')
  return <details className="compact-details" style={{ overflowWrap: 'anywhere' }}>
    <summary><strong>{t('memory.content.full')}</strong><span style={{ display: 'block', fontWeight: 400 }}>{preview}</span></summary>
    {body}
  </details>
}

function blockRepeatedLauncherEnter(event: KeyboardEvent<HTMLButtonElement>) {
  if (event.key === 'Enter' && event.repeat && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
    && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); event.stopPropagation() }
}

export default function RoomMemory(props: Props) {
  return <MemoryView key={`${props.ownerId}:${props.authEpoch}:${props.roomId}`} {...props} />
}
function MemoryView({ roomId, ownerId, authEpoch, active, visible, events, agents, onChanged, onNavigate, panelId, labelledBy }: Props) {
  const { t, formatDateTime } = useI18n(), cache = useQueryClient()
  const [mount] = useState(() => crypto.randomUUID()), [generation] = useState(directoryViewGeneration)
  const alive = useRef(false), notified = useRef('')
  const memoryFinder = useRef<DirectorySearchHandle>(null)
  const memoryRoot = useRef<HTMLElement>(null), cancelNavigation = useViewNavigationIntent()
  const editorLauncher = useRef<{ editorId: string; origin: HTMLButtonElement } | null>(null)
  const [cancelReturn, setCancelReturn] = useState<{ token: string; target: HTMLButtonElement | null; cancel: HTMLButtonElement } | null>(null)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const identity = `${ownerId}:${authEpoch}`
  const sameOwner = () => { const auth = useAuthStore.getState(); return !!auth.token && auth.epoch === authEpoch && auth.user?.id === ownerId && directoryViewGeneration() === generation }
  const workspace = useSyncExternalStore(subscribeMemoryWorkspaces, () => readMemoryWorkspace(identity, roomId))
  const { editor, request, notice, conflicted, filters } = workspace
  const sourceEvents = useMemo(() => events.filter(event => event.room_id === roomId && (event.kind === 'message' || event.kind === 'summary')), [events, roomId])
  const pending = request?.status === 'sending', retry = request?.status === 'unknown' ? request : null
  const edit = (update: (previous: MemoryWorkspace) => MemoryWorkspace) => sameOwner() && updateMemoryWorkspace(identity, roomId, generation, update)
  const prefix = ['room-memory', ownerId, authEpoch, roomId]
  const list = useQuery({ queryKey: prefix, enabled: visible, retry: false, gcTime: 0, queryFn: async () => {
    if (!sameOwner()) throw new Error('Memory scope changed')
    const value = await roomMemoriesApi.list(roomId)
    if (!sameOwner() || value.room_id !== roomId || value.items.some(item => item.room_id !== roomId)) throw new Error('Memory scope changed')
    return value
  } })
  const change = events.filter(event => event.room_id === roomId && event.kind === 'activity' && event.payload.room_memory).at(-1)?.event_id
  useEffect(() => { if (visible) void cache.invalidateQueries({ queryKey: prefix }) }, [change, visible, ownerId, authEpoch, roomId, cache])
  useEffect(() => {
    if (!workspace.changeId || workspace.changeId === notified.current || !alive.current || !sameOwner()) return
    notified.current = workspace.changeId; onChanged?.()
  }, [workspace.changeId, onChanged])
  const freshRead = () => { const state = cache.getQueryState<MemoryList>(prefix); return active && alive.current && sameOwner()
    && state?.status === 'success' && state.fetchStatus === 'idle' && !state.isInvalidated && state.data?.room_id === roomId }
  const open = (value: MemoryEditorBase, origin: HTMLButtonElement) => {
    if (!freshRead() || !origin.isConnected || !memoryRoot.current?.contains(origin)) return
    const created = createMemoryEditor(value, agents.find(agent => agent.is_active && list.data?.agents.some(member => member.agent_id === agent.id && member.is_active))?.id)
    if (edit(previous => previous.editor || memoryWorkspaceLocked(previous) ? previous : ({ ...previous,
      editor: created, notice: null, conflicted: false }))) {
      cancelNavigation.cancel(); setCancelReturn(null)
      editorLauncher.current = { editorId: created.id, origin }
    }
  }
  const cancelEditor = (origin: HTMLButtonElement) => {
    const boundary = memoryRoot.current, modal = document.querySelector('dialog[open], [aria-modal="true"]')
    if (!editor || !alive.current || !sameOwner() || !visible || document.visibilityState !== 'visible' || !document.hasFocus()
      || !boundary?.isConnected || !origin.isConnected || !boundary.contains(origin) || !origin.getClientRects().length || origin.matches(':disabled')
      || origin.closest('[hidden], [inert], [aria-hidden="true"]') || modal && !modal.contains(boundary)) return
    const token = crypto.randomUUID(), launch = editorLauncher.current
    cancelNavigation.begin(token, boundary)
    if (!edit(previous => previous.editor?.id !== editor.id || memoryWorkspaceLocked(previous) ? previous
      : { ...previous, editor: null, notice: null, conflicted: false })) { cancelNavigation.cancel(); return }
    setCancelReturn({ token, target: launch?.editorId === editor.id ? launch.origin : null, cancel: origin })
  }
  useEffect(() => {
    if (!editor) editorLauncher.current = null
    if (!cancelReturn) return
    setCancelReturn(null)
    const boundary = memoryRoot.current, target = cancelReturn.target
    const modal = document.querySelector('dialog[open], [aria-modal="true"]')
    if (editor || !alive.current || !sameOwner() || !visible || !cancelNavigation.current(cancelReturn.token)
      || !boundary?.isConnected || !boundary.getClientRects().length || modal && !modal.contains(boundary)
      || document.activeElement !== document.body && document.activeElement !== cancelReturn.cancel) { cancelNavigation.cancel(); return }
    // Wait for the cancelled editor to unmount and its launch action to become
    // enabled. Never replay this focus intent after another view/action wins.
    if (target?.isConnected && boundary.contains(target) && target.getClientRects().length && !target.matches(':disabled')
      && !target.closest('[hidden], [inert], [aria-hidden="true"]')) {
      target.focus({ preventScroll: true })
      if (document.activeElement === target) {
        target.scrollIntoView({ block: 'nearest', inline: 'nearest' }); cancelNavigation.cancel(); return
      }
    }
    if (memoryFinder.current?.focus()) {
      const field = document.activeElement
      if (field instanceof HTMLElement && boundary.contains(field)) field.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    }
    cancelNavigation.cancel()
  }, [cancelReturn, editor?.id, visible])
  const execute = (intent: MemoryOperation, key: string) => {
    if (intent.operation === 'add') return roomMemoriesApi.add(roomId, intent.body, key)
    if (intent.operation === 'edit') return roomMemoriesApi.edit(roomId, intent.memoryId, intent.body, key)
    if (intent.operation === 'remove') return roomMemoriesApi.remove(roomId, intent.memoryId, intent.body, key)
    return roomMemoriesApi.legacy(roomId, intent.agentId, intent.body, key)
  }
  const matchesReceipt = (value: MemoryCommand, intent: MemoryOperation) => {
    if (!value || value.room_id !== roomId || value.revision !== intent.body.expected_room_revision + 1
      || typeof value.replayed !== 'boolean' || !Number.isSafeInteger(value.activity_seq) || value.activity_seq <= 0 || typeof value.activity_event_id !== 'string' || !value.activity_event_id) return false
    if (intent.operation === 'legacy' && intent.body.operation === 'disable') return value.memory_id === null && value.entry_revision === null
    if (typeof value.memory_id !== 'string' || !value.memory_id || !Number.isSafeInteger(value.entry_revision) || Number(value.entry_revision) <= 0) return false
    if (intent.operation === 'edit' || intent.operation === 'remove') return value.memory_id === intent.memoryId && value.entry_revision === intent.body.expected_revision + 1
    return value.entry_revision === 1
  }
  const during = (key: string, update: (previous: MemoryWorkspace) => MemoryWorkspace) => edit(previous => previous.request?.key === key && previous.request.status === 'sending' ? update(previous) : previous)
  const send = async (command: MemoryRequest, expected: MemoryWorkspace) => {
    if (!active || !alive.current || !sameOwner() || !['sending', 'unknown'].includes(command.status)) return
    if (command.status === 'unknown' && (expected.request?.key !== command.key || expected.request.status !== 'unknown')) return
    if (command.status === 'sending' && (expected.editor?.id !== command.editorId || memoryWorkspaceLocked(expected))) return
    if (!edit(previous => previous !== expected || previous.request?.status === 'sending'
      || previous.request?.status === 'unknown' && previous.request.key !== command.key ? previous
      : ({ ...previous, request: Object.freeze({ ...command, status: 'sending' }), notice: null }))) return
    try {
      const value = await execute(command.intent, command.key)
      if (!matchesReceipt(value, command.intent)) {
        during(command.key, previous => ({ ...previous, request: Object.freeze({ ...command, status: 'unknown', mismatch: true }) })); return
      }
      during(command.key, previous => ({ ...previous, editor: previous.editor?.id === command.editorId ? null : previous.editor,
        request: Object.freeze({ ...command, status: 'accepted', mismatch: false, receipt: snapshotMemory(value) }), conflicted: false, notice: null, changeId: value.activity_event_id }))
      if (sameOwner()) void cache.invalidateQueries({ queryKey: prefix })
    } catch (error) {
      const response = (error as { response?: { status?: number; data?: { detail?: { code?: string } } } }).response
      if (response?.status && response.status >= 400 && response.status < 500 && command.status === 'unknown') {
        during(command.key, previous => ({ ...previous, request: Object.freeze({ ...command, status: 'unknown' }), notice: 'memory.retryStillUnknown' }))
      } else if (response?.status && response.status >= 400 && response.status < 500) {
        const code = response.data?.detail?.code || ''
        during(command.key, previous => ({ ...previous, request: Object.freeze({ ...command, status: 'rejected' }),
          notice: errorKeys[code] || (response.status === 409 ? 'memory.conflict' : 'memory.rejected'),
          conflicted: response.status === 409 && code !== 'memory_room_busy' && code !== 'memory_read_only' }))
        if (sameOwner()) void cache.invalidateQueries({ queryKey: prefix })
      } else during(command.key, previous => ({ ...previous, request: Object.freeze({ ...command, status: 'unknown' }) }))
    }
  }
  const begin = (intent: MemoryOperation) => {
    const snapshot = readMemoryWorkspace(identity, roomId)
    if (!freshRead() || !editor || snapshot.editor !== editor || memoryWorkspaceLocked(snapshot) || snapshot.conflicted) return
    const target = 'target_agent_id' in intent.body ? intent.body.target_agent_id : intent.operation === 'legacy' ? intent.agentId : editor.entry?.target_agent_id
    void send(snapshotMemory({ key: crypto.randomUUID(), editorId: editor.id, intent, mode: editor.mode,
      beforeContent: editor.entry?.content || null, agentName: agents.find(agent => agent.id === target)?.name || null, ...(target === undefined ? {} : { targetAgentId: target }), status: 'sending' as const }), snapshot)
  }
  const data = list.data, locked = memoryWorkspaceLocked(workspace), readReady = list.isSuccess && !list.isFetching
  const canOpen = active && readReady && !!data && !locked && !editor
  const name = (id: number | null) => {
    if (id === null) return t('results.unrecorded')
    const label = agents.find((a) => a.id === id)?.name
    return label ? `${label} · Agent #${id}` : t('members.fallbackName', { id: String(id) })
  }
  const changeFilters = (patch: Partial<MemoryFilters>) => edit(previous => ({ ...previous, filters: Object.freeze({ ...previous.filters, ...patch }) }))
  const matches = directoryMatcher(filters.query)
  const shown = (data?.items || []).filter(entry => (filters.category === 'all' || entry.category === filters.category)
    && (filters.scope === 'all' || entry.scope === filters.scope) && matches([entry.content, entry.source_url, entry.source_snapshot.name,
      entry.target_agent_id ? name(entry.target_agent_id) : t('memory.room'), entry.category, t(`memory.category.${entry.category}`)]))
  const filtered = !!filters.query || filters.category !== 'all' || filters.scope !== 'all'
  const time = (value: string) => { const stamp = reviewTimestamp(value); return stamp ? <time dateTime={stamp}>{formatDateTime(stamp, reviewTimeOptions)}</time> : t('results.unrecorded') }
  const current = editor?.entry && data?.items.find((entry) => entry.memory_id === editor.entry!.memory_id)
  const changed = !!editor && !!data && (editor.roomRevision !== data.revision || !!editor.entry && current?.revision !== editor.entry.revision)
  return <section ref={memoryRoot} onPointerDownCapture={() => cancelNavigation.cancel()} onKeyDownCapture={() => cancelNavigation.cancel()} id={panelId} role={panelId ? 'tabpanel' : undefined} aria-labelledby={labelledBy} className="room-results room-memory" hidden={!visible} aria-label={t('memory.tab')}>
    <div className="page-actions"><h2 className="section-title">{t('memory.tab')}</h2><button type="button" className="button button--secondary button--compact" disabled={list.isFetching || pending} onClick={() => { void list.refetch({ cancelRefetch: false }) }}>{t('memory.refresh')}</button>
      <button type="button" className="button button--primary button--compact" disabled={!canOpen} onKeyDown={blockRepeatedLauncherEnter} onClick={event => open({ mode: 'add', roomRevision: data!.revision }, event.currentTarget)}>{t('memory.add')}</button></div>
    <details className="compact-details"><summary>{t('memory.about')}</summary><p>{t('memory.help')}</p><p>{t('memory.limitHelp')}</p><p>{t('memory.privacy')}</p><p>{t('memory.sessionWorkspace')}</p><p>{t('memory.filters.help')}</p><p>{t('memory.content.scope')}</p></details>
    {!active && <p>{t('chat.closedReadOnly')}</p>}
    {list.isPending && <p role="status">{t('common.loading')}</p>}{list.isError && <p role="alert">{t('memory.unavailable')}</p>}
    {list.isError && data && <p className="metadata">{t('memory.retainedList')}</p>}
    {editor && <p className="notice notice--info">{t('memory.finishEditor')}</p>}
    {request && <section className="notice notice--info stack stack--tight" aria-label={t('memory.sessionRequest')}>
      <strong>{t('memory.sessionRequest')} · {t(request.mode === 'remove' ? 'memory.delete' : request.mode === 'disable' ? 'memory.disable' : request.mode === 'replace' ? 'memory.replace' : request.mode === 'add' ? 'memory.add' : 'memory.edit')}</strong>
      <p role={request.status === 'unknown' || request.status === 'rejected' ? 'alert' : 'status'}>{t(request.status === 'sending' ? 'memory.requestSending' : request.status === 'accepted' ? 'memory.saved' : request.status === 'unknown' ? 'memory.unknown' : 'memory.rejected')}</p>
      {notice && <p role="alert">{t(notice)}</p>}
      <details><summary>{t('memory.originalRequest')}</summary><OriginalMemoryRequest request={request} /></details>
      {request.mismatch && <p>{t('memory.replyMismatch')}</p>}
      {retry && <><p>{t('memory.sameRequestHelp')}</p><button type="button" className="button button--secondary" disabled={!active || pending} onClick={() => { void send(retry, readMemoryWorkspace(identity, roomId)) }}>{t('memory.retry')}</button></>}
    </section>}
    <section className="stack stack--tight" aria-label={t('memory.filters.title')}>
      <DirectorySearch compact ref={memoryFinder} value={filters.query} onChange={query => changeFilters({ query })} hint={t('memory.filters.placeholder')}
        shown={shown.length} total={data?.items.length || 0} loading={list.isFetching} known={!!data} controls={`memory-list-${mount}`} />
      <div className="form-grid form-grid--two">
        <label className="field">{t('memory.filters.category')}<select value={filters.category} onChange={event => changeFilters({ category: event.target.value as MemoryFilters['category'] })}>
          <option value="all">{t('memory.filters.allCategories')}</option>{(['fact', 'decision', 'requirement'] as const).map(category => <option key={category} value={category}>{t(`memory.category.${category}`)}</option>)}
        </select></label>
        <label className="field">{t('memory.filters.scope')}<select value={filters.scope} onChange={event => changeFilters({ scope: event.target.value as MemoryFilters['scope'] })}>
          <option value="all">{t('memory.filters.allScopes')}</option><option value="room">{t('memory.room')}</option><option value="agent">{t('memory.filters.agentScope')}</option>
        </select></label>
      </div>
      {filtered && <button type="button" className="button button--ghost button--compact" onClick={() => { if (alive.current && sameOwner() && visible) { changeFilters({ query: '', category: 'all', scope: 'all' }); memoryFinder.current?.focus() } }}>{t('memory.filters.reset')}</button>}
    </section>
    {data && <>
      {data.items.length > 0 && !shown.length && <p role="status">{t('memory.filters.noMatches')}</p>}
      {data.items.length > 0 && !shown.length && (filters.category !== 'all' || filters.scope !== 'all') && <div className="stack stack--tight">
        <div><button type="button" className="button button--secondary button--compact" onClick={() => {
          if (!alive.current || !sameOwner() || !visible) return
          changeFilters({ category: 'all', scope: 'all' })
          // The recovery action disappears; keep keyboard focus on the stable query.
          memoryFinder.current?.focus()
        }}>{t('memory.filters.widenKeepingQuery')}</button></div>
        <p className="field-hint">{t('memory.filters.widenHelp')}</p>
      </div>}
      {!data.items.length && <p>{t(list.isError ? 'memory.retainedEmpty' : 'memory.empty')}</p>}
      <ul id={`memory-list-${mount}`} className="result-list">{shown.map((entry) => <li className="result-detail" key={entry.memory_id}>
        <p><strong>{t(`memory.category.${entry.category}`)}</strong> · {entry.scope === 'room' ? t('memory.room') : t('memory.agentScope', { agent: name(entry.target_agent_id) })}</p>
        <MemoryContent content={entry.content} />
        <p className="metadata">{t('memory.updated')} · {time(entry.updated_at)}</p>
        <details className="compact-details"><summary>{t('memory.source')}</summary>
          {entry.source_kind === 'event' && entry.source_seq && entry.source_event_id ? <><button type="button" className="text-link" onClick={() => onNavigate(entry.source_seq!, entry.source_event_id!)}>{t('results.source', { seq: entry.source_seq })}</button><p>{entry.source_snapshot.name || t('results.unrecorded')}</p></>
            : entry.source_kind === 'link' && entry.source_url && resultHttpUrl(entry.source_url) ? <><a href={entry.source_url} target="_blank" rel="noopener noreferrer">{entry.source_url}</a><p>{t('memory.linkLimit')}</p></> : <p>{t('memory.manual')}</p>}
          <p>{t('memory.revision', { revision: entry.revision })} · {t('memory.updatedBy', { owner: entry.updated_by })}</p></details>
        <div className="page-actions"><button type="button" className="button button--secondary" disabled={!canOpen} onKeyDown={blockRepeatedLauncherEnter} onClick={event => open({ mode: 'edit', roomRevision: data.revision, entry }, event.currentTarget)}>{t('memory.edit')}</button>
          <button type="button" className="button button--secondary" disabled={!canOpen} onKeyDown={blockRepeatedLauncherEnter} onClick={event => open({ mode: 'remove', roomRevision: data.revision, entry }, event.currentTarget)}>{t('memory.delete')}</button></div>
      </li>)}</ul>
      {data.agents.filter((agent) => Object.keys(agent.legacy).length > 0).map((agent) => <details className="compact-details" key={agent.agent_id}><summary>{t('memory.legacy')} · {name(agent.agent_id)} · {t(agent.legacy_disabled ? 'memory.disabled' : 'memory.inUse')}</summary>
        <p>{t('memory.legacyHelp')}</p><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(agent.legacy, null, 2)}</pre>
        {!agent.legacy_disabled && <div className="page-actions"><button type="button" disabled={!canOpen} onKeyDown={blockRepeatedLauncherEnter} onClick={event => open({ mode: 'disable', roomRevision: data.revision, legacy: agent }, event.currentTarget)}>{t('memory.disable')}</button>
          <button type="button" disabled={!canOpen || !agent.is_active} onKeyDown={blockRepeatedLauncherEnter} onClick={event => open({ mode: 'replace', roomRevision: data.revision, legacy: agent }, event.currentTarget)}>{t('memory.replace')}</button></div>}</details>)}
      <details className="compact-details"><summary>{t('memory.context')}</summary><p>{t('memory.revision', { revision: data.revision })}</p><p>{t('memory.floor', { seq: data.common_context_after_seq })}</p>
        {data.agents.map((agent) => <p key={agent.agent_id}>{name(agent.agent_id)} · {t('memory.floor', { seq: agent.context_after_seq })} · {t('memory.revision', { revision: agent.revision })}</p>)}</details>
    </>}
    {editor && <MemoryEditor key={editor.id} scopeKey={`${identity}:${roomId}:${mount}:${editor.id}`} readReady={readReady} editor={editor} agents={agents.filter((a) => a.is_active && data?.agents.some((m) => m.agent_id === a.id && m.is_active))} events={sourceEvents} onNavigate={(seq, id) => { if (alive.current && sameOwner()) onNavigate(seq, id) }}
      locked={locked || !active} changed={changed || conflicted} current={current} currentRoomRevision={data?.revision} onCancel={cancelEditor}
      onDraft={(patch) => edit(previous => previous.editor !== editor || memoryWorkspaceLocked(previous) ? previous : ({ ...previous, editor: Object.freeze({ ...editor, draft: Object.freeze({ ...editor.draft, ...patch }) }) }))}
      onRebase={() => { if (!data || !freshRead() || editor.entry && !current) return; const legacy = editor.legacy && data.agents.find(member => member.agent_id === editor.legacy!.agent_id); if (editor.legacy && (!legacy || legacy.legacy_disabled)) return; edit(previous => previous.editor !== editor || memoryWorkspaceLocked(previous) ? previous : ({ ...previous, editor: snapshotMemory({ ...editor, roomRevision: data.revision, ...(current ? { entry: current } : {}), ...(legacy ? { legacy } : {}) }), conflicted: false, notice: null })) }}
      onSubmit={(body) => {
        if (editor.mode === 'remove') begin({ operation: 'remove', memoryId: editor.entry!.memory_id, body: { expected_room_revision: editor.roomRevision, expected_revision: editor.entry!.revision } })
        else if (editor.mode === 'edit') begin({ operation: 'edit', memoryId: editor.entry!.memory_id, body: { ...body!, expected_revision: editor.entry!.revision } })
        else if (editor.legacy) begin({ operation: 'legacy', agentId: editor.legacy.agent_id, body: { expected_room_revision: editor.roomRevision, expected_agent_revision: editor.legacy.revision, expected_legacy_digest: editor.legacy.legacy_digest, operation: editor.mode === 'replace' ? 'replace' : 'disable', replacement: body } })
        else begin({ operation: 'add', body: body! })
      }} />}
  </section>
}

function OriginalMemoryRequest({ request }: { request: MemoryRequest }) {
  const { t } = useI18n(), intent = request.intent
  const write = intent.operation === 'add' || intent.operation === 'edit' ? intent.body : intent.operation === 'legacy' ? intent.body.replacement : null
  const targetId = intent.operation === 'legacy' ? intent.agentId : request.targetAgentId
  const capturedAgent = (id: number | null | undefined) => id === null || id === undefined ? t('results.unrecorded')
    : request.agentName ? `${request.agentName} · Agent #${id}` : `Agent #${id}`
  return <div className="stack stack--tight">
    <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{write?.content || request.beforeContent || t('memory.legacy')}</p>
    {write && <><p>{t(`memory.category.${write.category}`)} · {write.scope === 'room' ? t('memory.room') : t('memory.agentScope', { agent: capturedAgent(write.target_agent_id) })}</p>
      <p>{t(`memory.source.${write.source.kind}`)}{write.source.kind === 'event' ? ` · #${write.source.seq}` : write.source.kind === 'link' ? ` · ${write.source.url}` : ''}</p></>}
    {!write && targetId !== undefined && <p>{targetId === null ? t('memory.room') : t('memory.agentScope', { agent: capturedAgent(targetId) })}</p>}
    <p>{t('memory.listNotReceipt')}</p>
  </div>
}

function MemoryEditor({ scopeKey, readReady, editor, agents, events, onNavigate, locked, changed, current, currentRoomRevision, onCancel, onRebase, onDraft, onSubmit }: { scopeKey: string; readReady: boolean; editor: Editor; agents: Agent[]; events: RoomEvent[]; onNavigate: (seq: number, id: string) => void; locked: boolean; changed: boolean; current?: MemoryEntry; currentRoomRevision?: number; onCancel: (origin: HTMLButtonElement) => void; onRebase: () => void; onDraft: (patch: Partial<MemoryDraft>) => void; onSubmit: (body: MemoryWrite | null) => void }) {
  const { t } = useI18n(), entry = editor.entry
  const agentIdentity = (id: number | null) => {
    if (id === null) return t('results.unrecorded')
    const label = agents.find(a => a.id === id)?.name
    return label ? `${label} · Agent #${id}` : `Agent #${id}`
  }
  const ime = useImeFormGuard(scopeKey)
  const { content, category, scope, agent, source, url, eventKey } = editor.draft
  const [ack, setAck] = useState(false), [sourceQuery, setSourceQuery] = useState(''), [sourceAnchor,setSourceAnchor] = useState<string|null>(null)
  const fieldPrefix = useId()
  const changeDraft = (patch: Partial<MemoryDraft>) => { setAck(false); onDraft(patch) }
  useEffect(() => setAck(false), [editor.draft, editor.roomRevision, editor.entry?.revision, editor.legacy?.revision])
  const affectedAll = entry?.scope === 'room' || scope === 'room'
  const affected = [...new Set([entry?.target_agent_id, editor.legacy?.agent_id, scope === 'agent' ? Number(agent) : null].filter((id): id is number => !!id))]
  const affectedLabel = affectedAll ? t('memory.future') : affected.map(agentIdentity).join(', ')
  const remove = editor.mode === 'remove' || editor.mode === 'disable', impact = editor.mode !== 'add'
  // Keep the opening input across panel remounts and explicit revision rebases.
  // Older in-memory editors without a baseline conservatively ask before discard.
  const hasUnsavedDraft = !remove && (!editor.initialDraft || (['content', 'category', 'scope', 'agent', 'source', 'url', 'eventKey'] as const)
    .some(key => editor.draft[key] !== editor.initialDraft![key]))
  const [discardPrompt, setDiscardPrompt] = useState(false)
  const cancelButton = useRef<HTMLButtonElement>(null)
  useEffect(() => setDiscardPrompt(false), [editor.draft, editor.roomRevision, editor.entry?.revision, editor.legacy?.revision, locked])

  // Existing canonical event remains selectable even when older than the currently loaded feed.
  const selectedEvent = events.find((event) => event.event_id === eventKey)
  const savedEvent = editor.draft.event?.event_id === eventKey ? editor.draft.event : null
  const eventSource = selectedEvent ? { kind: 'event' as const, event_id: selectedEvent.event_id, seq: selectedEvent.seq }
    : savedEvent ? { kind: 'event' as const, event_id: savedEvent.event_id, seq: savedEvent.seq } : null

  const sourceChoices = useMemo(() => source !== 'event' ? [] : events.map(event => {
    const content = typeof event.payload.content === 'string' ? event.payload.content : ''
    const text = event.actor_type === 'agent' ? assistantVisibleMarkdown(content) : content
    return { event, text, excerpt: sourceExcerpt(text.replace(/\s+/g, ' ').trim(), 80) }
  }).sort((a,b)=>b.event.seq-a.event.seq), [events, source])
  const sourceMatches = useMemo(() => {
    if(!sourceQuery.trim())return sourceChoices
    const matches = directoryMatcher(sourceQuery)
    return sourceChoices.filter(({ event, text }) => matches([String(event.seq), `#${event.seq}`, event.actor_name_snapshot, text]))
  }, [sourceChoices, sourceQuery])
  const sourceStart=sourceAnchor?Math.max(0,sourceMatches.findIndex(choice=>choice.event.event_id===sourceAnchor)):0
  const sourceEnd=Math.min(sourceStart+50,sourceMatches.length),sourcePage=sourceMatches.slice(sourceStart,sourceEnd)
  const moveSourcePage=(index:number)=>{if(!locked)setSourceAnchor(sourceMatches[Math.max(0,Math.min(index,sourceMatches.length-1))]?.event.event_id||null)}
  const preview = sourceChoices.find(choice => choice.event.event_id === eventKey)
  const pinned = !!eventSource && !sourcePage.some(choice => choice.event.event_id === eventKey)
  const choiceLabel = (seq: number, name: string | null | undefined, excerpt = '') => `#${seq} · ${name || t('results.unrecorded')}${excerpt ? ` · ${excerpt}` : ''}`

  // Match the original trimmed write body and Python's code-point limits;
  // native maxlength counts UTF-16 units and can cut a pasted draft early.
  const storedContent = content.trim(), contentChars = editorTextLength(storedContent), urlChars = editorTextLength(url)
  const contentValid = contentChars >= 1 && contentChars <= 2000
  const urlValid = urlChars >= 1 && urlChars <= 2048 && !!resultHttpUrl(url)
  const contentInvalid = content.length > 0 && !contentValid, urlInvalid = url.length > 0 && !urlValid
  const valid = remove || contentValid && (scope === 'room' || agents.some((a) => a.id === Number(agent))) && (source === 'manual' || source === 'link' && urlValid || source === 'event' && !!eventSource)
  return <form {...ime.handlers} className="result-save stack stack--tight" aria-label={t('memory.editor')} onSubmit={(event) => {
    event.preventDefault(); if (ime.isSubmitBlocked(event) || locked || !readReady || changed || !valid || impact && !ack) return
    const provenance: MemorySource | null = source === 'manual' ? { kind: 'manual' } : source === 'link' ? { kind: 'link', url } : eventSource
    onSubmit(remove ? null : { expected_room_revision: editor.roomRevision, category, content: storedContent, scope, target_agent_id: scope === 'agent' ? Number(agent) : null, source: provenance! })
  }}>
    <h3>{t(remove ? 'memory.confirmDelete' : editor.mode === 'add' ? 'memory.add' : 'memory.edit')}</h3>
    {impact && <div className="notice notice--warning"><p id={`${fieldPrefix}-impact`}>{t('memory.impact')}</p><p>{t('memory.affected')}: {affectedLabel} · {t('memory.afterSave')}</p><p>{t('memory.keep')}</p></div>}
    {changed && <div role="alert" className="stack stack--tight">
      <p>{t('memory.conflict')}</p>
      <div id={`${fieldPrefix}-conflict-revisions`} className="stack stack--tight">
        <p className="metadata">{t('memory.conflictListRevision', { base: editor.roomRevision, received: currentRoomRevision ?? '—' })}</p>
        {entry && <p className="metadata">{t('memory.conflictEntryRevision', { base: entry.revision, received: current?.revision ?? '—' })}</p>}
      </div>
      {current ? <>
        <label className="field">{t('memory.conflictReceived')}<textarea readOnly rows={6} value={current.content} spellCheck={false} aria-describedby={`${fieldPrefix}-conflict-revisions`} /></label>
        <p>{t(`memory.category.${current.category}`)} · {current.scope === 'room' ? t('memory.room') : t('memory.agentScope', { agent: agentIdentity(current.target_agent_id) })} · {t(`memory.source.${current.source_kind}`)}</p>
        {current.source_kind === 'link' && current.source_url && <p style={{ overflowWrap: 'anywhere' }}>{t('memory.url')}: {current.source_url}</p>}
        {current.source_kind === 'event' && current.source_seq && <p>{t('memory.event')} · #{current.source_seq}</p>}
      </> : entry && <p>{t(readReady ? 'memory.deleted' : 'memory.readBeforeSave')}</p>}
      <p className="field-hint">{t('memory.conflictKeepDraft')}</p>
      <button type="button" disabled={locked || !readReady || !!entry && !current} onClick={() => { onRebase(); setAck(false) }}>{t('memory.rebase')}</button>
    </div>}
    <details className="compact-details"><summary>{t('common.inputNotes')}</summary><p>{t('memory.compositionHelp')}</p>{!remove && <p id={`${fieldPrefix}-unicode`}>{t('memory.input.unicode')}</p>}</details>
    {!readReady && <p role="status">{t('memory.readBeforeSave')}</p>}
    <fieldset disabled={locked} className="stack stack--tight">
      {remove ? <p style={{ whiteSpace: 'pre-wrap' }}>{entry?.content || t('memory.legacy')}</p> : <>
        <label className="field">{t('memory.category')}<select value={category} onChange={(e) => changeDraft({ category: e.target.value as MemoryCategory })}>{(['fact', 'decision', 'requirement'] as const).map((key) => <option key={key} value={key}>{t(`memory.category.${key}`)}</option>)}</select></label>
        <label className="field">{t(changed ? 'memory.conflictDraft' : 'memory.content')}<textarea autoFocus value={content} required aria-invalid={contentInvalid || undefined}
          aria-describedby={`${fieldPrefix}-content-count ${fieldPrefix}-content-rule ${fieldPrefix}-unicode`}
          onChange={(e) => changeDraft({ content: e.target.value })} />
          <span id={`${fieldPrefix}-content-count`} className={`metadata${contentInvalid ? ' metadata--danger' : ''}`}>{t('memory.input.contentCount', { count: contentChars })}</span>
          <span id={`${fieldPrefix}-content-rule`} className={`field-hint${contentInvalid ? ' metadata--danger' : ''}`}>{t('memory.input.contentRule')}</span>
        </label>
        {contentChars > 2000 && <p role="alert" className="notice notice--warning">{t('memory.input.tooLong')}</p>}
        <label className="field">{t('memory.scope')}<select value={scope} disabled={!!editor.legacy} onChange={(e) => changeDraft({ scope: e.target.value as 'room' | 'agent' })}><option value="room">{t('memory.room')}</option><option value="agent">{t('memory.agent')}</option></select></label>
        {scope === 'agent' && <label className="field">{t('memory.agent')}<select value={agent} disabled={!!editor.legacy} required onChange={(e) => changeDraft({ agent: e.target.value })}><option value="">—</option>{agent && !agents.some(value => String(value.id) === agent) && <option value={agent}>{t('memory.unavailableAgent', { id: agent })}</option>}{agents.map((a) => <option key={a.id} value={a.id}>{agentIdentity(a.id)}</option>)}</select></label>}
        <p className="field-hint">{t('memory.agentIdentityHelp')}</p>
        <label className="field">{t('memory.source')}<select value={source} onChange={(e) => changeDraft({ source: e.target.value as MemorySource['kind'] })}>{(['manual', 'event', 'link'] as const).map((key) => <option key={key} value={key}>{t(`memory.source.${key}`)}</option>)}</select></label>
        {source === 'manual' && <p>{t('memory.manual')}</p>}
        {source === 'link' && <><label className="field">{t('memory.url')}<input type="url" required value={url} aria-invalid={urlInvalid || undefined}
          aria-describedby={`${fieldPrefix}-url-count ${fieldPrefix}-url-rule ${fieldPrefix}-unicode`}
          onChange={(e) => changeDraft({ url: e.target.value })} />
          <span id={`${fieldPrefix}-url-count`} className={`metadata${urlInvalid ? ' metadata--danger' : ''}`}>{t('memory.input.urlCount', { count: urlChars })}</span>
          <span id={`${fieldPrefix}-url-rule`} className={`field-hint${urlInvalid ? ' metadata--danger' : ''}`}>{t('memory.input.urlRule')}</span>
        </label>{urlChars > 2048 && <p role="alert" className="notice notice--warning">{t('memory.input.tooLong')}</p>}<p>{t('memory.linkLimit')}</p></>}
        {source === 'event' && <section className="stack stack--tight" aria-label={t('memory.sourceSearch.title')}>
          <DirectorySearch value={sourceQuery} onChange={query=>{if(!locked){setSourceQuery(query);setSourceAnchor(null)}}} label={t('memory.sourceSearch.title')} hint={t('memory.sourceSearch.hint')}
            shown={sourceMatches.length} total={sourceChoices.length} known loading={false} controls={`${fieldPrefix}-events`} preventSubmit disabled={locked} />
          <p className="field-hint" id={`${fieldPrefix}-source-help`}>{t('memory.sourceSearch.help')}</p>
          <p className="field-hint">{t('memory.sourcePages.help')}</p>
          {sourceMatches.length>0&&<p className="metadata">{t('memory.sourcePages.range',{from:sourceStart+1,to:sourceEnd,total:sourceMatches.length})}</p>}
          <div className="page-actions">
            <button type="button" className="text-link" disabled={locked||sourceStart===0} onClick={()=>moveSourcePage(0)}>{t('memory.sourcePages.newest')}</button>
            <button type="button" className="text-link" disabled={locked||sourceStart===0} onClick={()=>moveSourcePage(sourceStart-50)}>{t('memory.sourcePages.newer')}</button>
            <button type="button" className="text-link" disabled={locked||sourceEnd>=sourceMatches.length} onClick={()=>moveSourcePage(sourceStart+50)}>{t('memory.sourcePages.older')}</button>
            <button type="button" className="text-link" disabled={locked||sourceEnd>=sourceMatches.length} onClick={()=>moveSourcePage(Math.max(0,sourceMatches.length-50))}>{t('memory.sourcePages.oldest')}</button>
            {pinned&&preview&&<button type="button" className="text-link" disabled={locked} onClick={()=>{if(!locked){setSourceQuery('');setSourceAnchor(eventKey)}}}>{t('memory.sourcePages.locate')}</button>}
          </div>
          {sourceQuery && !sourceMatches.length && <p role="status">{t('memory.sourceSearch.noMatches')}</p>}
          {pinned && <p className="field-hint">{t('memory.sourcePages.pinned')}</p>}
          <label className="field">{t('memory.event')}<select id={`${fieldPrefix}-events`} value={eventKey} required aria-describedby={`${fieldPrefix}-source-help`} onChange={(e) => { const selected = events.find(value => value.event_id === e.target.value); changeDraft({ eventKey: e.target.value, event: selected ? { event_id: selected.event_id, seq: selected.seq, name: selected.actor_name_snapshot || undefined } : e.target.value === savedEvent?.event_id ? savedEvent : null }) }}>
            <option value="">—</option>
            {pinned && eventSource && <option value={eventKey}>{choiceLabel(eventSource.seq, selectedEvent?.actor_name_snapshot || savedEvent?.name, preview?.excerpt)}</option>}
            {sourcePage.map(({ event, excerpt }) => <option key={event.event_id} value={event.event_id}>{choiceLabel(event.seq, event.actor_name_snapshot, excerpt)}</option>)}
          </select></label>
          {eventSource && <div className="notice notice--info stack stack--tight" aria-label={t('memory.sourcePreview.title')}>
            <strong>{t('memory.sourcePreview.title')} · #{eventSource.seq}</strong>
            <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{preview ? (sourceExcerpt(preview.text, 240) || t('memory.sourcePreview.empty')) : t('memory.sourcePreview.outside')}</p>
            <button type="button" className="text-link" onClick={() => onNavigate(eventSource.seq, eventSource.event_id)}>{t('memory.sourcePreview.open')}</button>
          </div>}
        </section>}
      </>}
      {impact && <div className="stack stack--tight">
        <p id={`${fieldPrefix}-affected-ack`} className="metadata"><strong>{t('memory.affected')}:</strong> {affectedLabel} · {t('memory.afterSave')}</p>
        <label><input type="checkbox" checked={ack} aria-describedby={`${fieldPrefix}-affected-ack ${fieldPrefix}-impact`} onChange={(e) => setAck(e.target.checked)} /> {t('memory.ack')}</label>
      </div>}
      <div className="page-actions"><button type="submit" className="button button--primary" disabled={!readReady || !valid || changed || impact && !ack}>{t(remove ? 'memory.confirmDelete' : 'memory.save')}</button><button ref={cancelButton} type="button" className="button button--secondary" aria-expanded={hasUnsavedDraft ? discardPrompt : undefined} aria-controls={hasUnsavedDraft && discardPrompt ? `${fieldPrefix}-discard` : undefined} onKeyDown={blockRepeatedLauncherEnter} onClick={event => {
        if (locked) return
        if (hasUnsavedDraft) setDiscardPrompt(true)
        else onCancel(event.currentTarget)
      }}>{t('common.cancel')}</button></div>
      {hasUnsavedDraft && discardPrompt && <section id={`${fieldPrefix}-discard`} className="notice notice--warning stack stack--tight" aria-label={t('memory.discard.title')}>
        <p role="status">{t('memory.discard.help')}</p>
        <div className="page-actions">
          <button type="button" className="button button--secondary" onKeyDown={blockRepeatedLauncherEnter} onClick={() => {
            setDiscardPrompt(false)
            const target = cancelButton.current
            if (document.hasFocus() && target?.isConnected && target.getClientRects().length && !target.matches(':disabled') && !target.closest('[hidden], [inert], [aria-hidden="true"]')) target.focus({ preventScroll: true })
          }}>{t('memory.discard.keep')}</button>
          <button type="button" className="button button--ghost" onKeyDown={blockRepeatedLauncherEnter} onClick={event => { if (!locked) onCancel(event.currentTarget) }}>{t('memory.discard.confirm')}</button>
        </div>
      </section>}
    </fieldset>
  </form>
}

// Excerpts are display-only; the canonical source and write body stay untouched.
function sourceExcerpt(text: string, limit: number) {
  let excerpt = '', count = 0
  for (const point of text) {
    if (count++ === limit) return excerpt + '…'
    excerpt += point
  }
  return excerpt
}
