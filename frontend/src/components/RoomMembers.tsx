import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { conversationsApi } from '../api/conversations'
import { connectionsApi } from '../api/connections'
import type { Agent } from '../api/agents'
import type { RoomEvent } from '../api/events'
import type { ToolActivity } from '../api/tools'
import { agentStatus, type ConnectionState } from './agentStatus'
import { StatusText } from './ui'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { memberReadiness, type ReadinessState } from '../lib/roomWorkflow'
import { providerDisplayName } from './connections/ProviderCatalog'
import { useAuthStore } from '../stores/authStore'
import { validateWorkState } from '../lib/workState'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { blocksMemberChanges, memberChanges, startMemberChange, subscribeMemberChanges, updateMemberChange, type MemberAction, type MemberChange } from '../lib/pendingMemberChanges'
import AgentWorkDetails from './AgentWorkDetails'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useDirectoryValue } from '../lib/useDirectoryView'
import { AgentCharacter } from './AgentCharacter'
import { useBotConversationMetadata } from '../lib/useBotScope'
export { AgentCharacter } from './AgentCharacter'

const resumeKeys: Record<string, MessageKey> = { ready:'members.resumeReady', resume_settling:'members.resumeSettling', resume_effect_unknown:'members.resumeUnknown', resume_context_changed:'members.resumeChanged', resume_cancelled:'members.resumeCancelled' }

const readinessKeys: Record<ReadinessState, MessageKey> = {
  ready: 'members.ready', unverified: 'members.unverified', missing_connection: 'members.missingConnection',
  disabled: 'members.connectionDisabled', missing_secret: 'members.missingSecret', missing_model: 'members.missingModel',
}

export default function RoomMembers({ roomId, agents, actions, events, connection, readOnly = false, onNavigate, onAction, agentRead }:
  { roomId: number; agents: Agent[]; actions: ToolActivity[]; events: RoomEvent[]; connection: ConnectionState; readOnly?: boolean;
    agentRead?: { fetching: boolean; failed: boolean; ready: boolean; refresh: () => void };
    onNavigate?: (seq: number, eventId: string) => void; onAction?: (id: string) => void }) {
  const cache = useQueryClient()
  const ownerId = useAuthStore((state) => state.user?.id || 0)
  const authEpoch = useAuthStore((state) => state.epoch)
  const [selected, setSelected] = useState(0)
  const [choiceQuery, setChoiceQuery] = useState('')
  const selectedRef = useRef({ id: 0, revision: 0 })
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const identity = `${ownerId}:${authEpoch}`, generation = directoryViewGeneration()
  const current = () => {
    const auth = useAuthStore.getState()
    return auth.user?.id === ownerId && auth.epoch === authEpoch && directoryViewGeneration() === generation
  }
  const [memberQuery, setMemberQuery] = useDirectoryValue<string>(identity, `room-members:find:${roomId}`, '')
  const memberListId = useId()
  const records = useSyncExternalStore(subscribeMemberChanges, () => memberChanges(identity, roomId))
  const latest = records[records.length - 1]
  const blocked = blocksMemberChanges(records)
  const [reading, setReading] = useState(false), [readFailure, setReadFailure] = useState(false)
  const readLock = useRef(false)
  const { t, formatNumber } = useI18n()
  const queryKey = ['members', roomId, ownerId, authEpoch]
  const membersQuery = useQuery({ queryKey, queryFn: () => conversationsApi.listMembers(roomId), retry: false, refetchInterval: readOnly ? false : 2000 })
  const members = membersQuery.data || []
  const botConversation = useBotConversationMetadata(roomId)
  const canChangeMembers = botConversation.isSuccess && !botConversation.isError && botConversation.data.can_change_members
  const isLoading = membersQuery.isLoading, isError = membersQuery.isError
  const connectionsQuery = useQuery({ queryKey: ['connections', identity], queryFn: async () => {
    if (!current()) throw new Error('Owner changed')
    const value = await connectionsApi.list()
    if (!current()) throw new Error('Owner changed')
    return value
  } })
  const catalogQuery = useQuery({ queryKey: ['provider-catalog'], queryFn: connectionsApi.catalog })
  const readMembers = async () => {
    if (!current()) throw new Error('Owner changed')
    await cache.cancelQueries({ queryKey, exact: true })
    const result = await conversationsApi.listMembers(roomId)
    if (!current()) throw new Error('Owner changed')
    cache.setQueryData(queryKey, result)
    return result
  }
  const inspect = async () => {
    if (readLock.current || !current()) return
    const before = memberChanges(identity, roomId)
    readLock.current = true; setReading(true); setReadFailure(false)
    try {
      await readMembers()
      for (const row of before) {
        if (row.status === 'unknown') updateMemberChange(identity, roomId, row.id, { inspected: true }, generation)
        else if (row.status === 'accepted') updateMemberChange(identity, roomId, row.id, { needsRead: false }, generation)
      }
    } catch { if (alive.current && current()) setReadFailure(true) }
    finally { readLock.current = false; if (alive.current && current()) setReading(false) }
  }
  const change = async (action: MemberAction, agentId: number) => {
    if (!current() || readOnly || !membersQuery.isSuccess || readLock.current) return
    if ((action === 'add' || action === 'remove') && !canChangeMembers) return
    const row: MemberChange = { id: crypto.randomUUID(), identity, roomId, agentId, agentName: name(agentId), action,
      status: 'sending', needsRead: false, inspected: false, acknowledged: false }
    if (!startMemberChange(row, generation)) return
    const selectionRevision = selectedRef.current.revision
    try {
      if (action === 'add') {
        const added = await conversationsApi.addMember(roomId, agentId)
        if (added.room_id !== roomId || added.agent_id !== agentId || !added.is_active) throw new Error('Unexpected member response')
      } else if (action === 'remove') await conversationsApi.removeMember(roomId, agentId)
      else if (action === 'pause') await conversationsApi.pauseMember(roomId, agentId)
      else await conversationsApi.resumeMember(roomId, agentId)
      if (!current()) return
      updateMemberChange(identity, roomId, row.id, { status: 'refreshing', needsRead: true }, generation)
      if (action === 'add' && alive.current && selectedRef.current.id === agentId && selectedRef.current.revision === selectionRevision) {
        selectedRef.current = { id: 0, revision: selectionRevision + 1 }; setSelected(0)
      }
      try {
        await readMembers()
        updateMemberChange(identity, roomId, row.id, { status: 'accepted', needsRead: false }, generation)
      } catch { updateMemberChange(identity, roomId, row.id, { status: 'accepted', needsRead: true }, generation) }
    } catch (failure) {
      if (!current()) return
      const response = (failure as { response?: { status?: number; data?: { detail?: { code?: string } } } }).response
      const rejected = !!response?.status && [400, 401, 403, 404, 409, 413, 415, 422, 429].includes(response.status)
      const code = response?.data?.detail?.code
      updateMemberChange(identity, roomId, row.id, { status: rejected ? 'rejected' : 'unknown',
        reason: code && resumeKeys[code] ? code : undefined }, generation)
    }
  }
  const acknowledge = (row: MemberChange) => {
    const live = memberChanges(identity, roomId).find(item => item.id === row.id)
    if (!current() || !live?.inspected || live.status !== 'unknown' || readLock.current) return
    if (window.confirm(t('members.change.continueConfirm')) && current()) {
      updateMemberChange(identity, roomId, row.id, { acknowledged: true }, generation)
    }
  }
  const actionName = (action: MemberAction) => t(action === 'add' ? 'members.add' : action === 'remove' ? 'common.remove' : action === 'pause' ? 'members.pause' : 'members.resume')
  const memberIds = new Set(members.map((member) => member.agent_id))
  const name = (id: number) => agents.find((agent) => agent.id === id)?.name || t('members.fallbackName', { id: String(id) })
  const availability = (agent: Agent): MessageKey | '' => {
    if (!connectionsQuery.isSuccess) return 'projects.connectionsUnavailable'
    const saved = connectionsQuery.data.find((item) => item.id === agent.connection_id)
    if (!saved) return 'projects.connectionMissing'
    if (!saved.enabled) return 'projects.connectionDisabled'
    return ''
  }
  const candidates = agents.filter((agent) => !memberIds.has(agent.id) && agent.is_active)
  const selectedAgent = candidates.find((agent) => agent.id === selected)
  const matchesChoice = directoryMatcher(choiceQuery)
  const matchedCandidates = candidates.filter(agent => matchesChoice([
    agent.name, agent.description, String(agent.id), `#${agent.id}`, agent.model_id, agent.model,
    connectionsQuery.data?.find(item => item.id === agent.connection_id)?.label,
  ], agent.id))
  const selectedOutsideSearch = !!selectedAgent && !matchedCandidates.some(agent => agent.id === selectedAgent.id)
  // Keep the selected ID available in the native select even while a search hides it.
  // Filtering is never permission to choose or submit a different Agent.
  const choiceOptions = selectedOutsideSearch ? [selectedAgent!, ...matchedCandidates] : matchedCandidates
  const selectedUnavailable = agentRead?.ready === false || !selectedAgent || !!availability(selectedAgent)
  const addSelected = () => {
    if (!selectedAgent || selectedUnavailable || blocked || reading) return
    void change('add', selectedAgent.id)
  }
  const configured = members.map(member => {
    const agent = agents.find(item => item.id === member.agent_id)
    const readiness = memberReadiness(agent, connectionsQuery.data || [])
    const provider = readiness.connection?.provider_id || readiness.connection?.provider_kind
    const descriptor = catalogQuery.data?.providers.find(item => item.provider_id === provider)
    return { member, agent, readiness,
      status: agentStatus(member, actions, events, isError ? 'retrying' : connection, t),
      providerName: descriptor ? providerDisplayName(descriptor, t) : t('connections.providers.unknown') }
  })
  const matchMember = directoryMatcher(memberQuery)
  const matchingMembers = new Set(configured.filter(({ member, agent, readiness, status, providerName }) => matchMember([
    name(member.agent_id), String(member.agent_id), `#${member.agent_id}`, agent?.model_id, agent?.model,
    readiness.connection?.label, providerName, status.label, t(readinessKeys[readiness.state]),
    member.paused && member.resume_status ? t(resumeKeys[member.resume_status] || 'members.resumeSettling') : '',
  ], member.agent_id)).map(({ member }) => member.agent_id))
  const pinnedMembers = new Set(records.filter(row => row.status === 'sending' || row.status === 'refreshing'
    || row.needsRead || row.status === 'unknown' && !row.acknowledged).map(row => row.agentId))
  for (const member of members) if (member.paused && member.resume_status && member.resume_status !== 'ready') pinnedMembers.add(member.agent_id)
  const visibleMembers = new Set(members.filter(member => matchingMembers.has(member.agent_id) || pinnedMembers.has(member.agent_id)).map(member => member.agent_id))
  const pinnedOutsideSearch = members.filter(member => pinnedMembers.has(member.agent_id) && !matchingMembers.has(member.agent_id)).length
  return <section aria-label={t('members.region')} className="rail-section stack stack--tight">
    <h2 className="section-title">{t('members.title')} <span className="metadata">{membersQuery.isSuccess ? formatNumber(members.length) : '—'}</span></h2>
    {!readOnly && <p className="field-hint">{t('members.pauseBoundaryHelp')}</p>}
    {isLoading && <p>{t('members.loading')}</p>}
    {isError && <p role="status">{t('members.unavailable')} <button type="button" className="text-link" disabled={reading} onClick={() => { void inspect() }}>{t(reading ? 'common.loading' : 'common.retry')}</button></p>}
    {membersQuery.isSuccess && members.length === 0 && <p className="section-copy">{t('members.empty')}</p>}
    {botConversation.isSuccess && botConversation.data.personal_agent_id !== null && <p className="metadata">{t('bots.personalConversation')}{' '}<Link className="text-link" to="/projects">{t('bots.newGroup')}</Link></p>}
    {!readOnly && !botConversation.isSuccess && <p className="metadata" role="status">{t(botConversation.isError ? 'bots.membershipUnknown' : 'bots.membershipReading')}{botConversation.isError && <>{' '}<button type="button" className="text-link" disabled={botConversation.isFetching} onClick={() => { if (current()) void botConversation.refetch({ cancelRefetch: false }) }}>{t('common.retry')}</button></>}</p>}
    <DirectorySearch exactId value={memberQuery} onChange={value => { if (alive.current && current()) setMemberQuery(value) }}
      label={t('members.find.title')} hint={t('members.find.hint')} shown={matchingMembers.size} total={members.length}
      known={membersQuery.data !== undefined} loading={membersQuery.isPending} controls={memberListId} preventSubmit/>
    <p className="field-hint">{t('members.find.scope')}</p>
    {pinnedOutsideSearch > 0 && <p role="status" className="notice notice--info">{t('members.find.pinned', { count: pinnedOutsideSearch })}</p>}
    {members.length > 0 && memberQuery.trim() && matchingMembers.size === 0 && <p role="status" className="field-hint">{t('directory.noMatches')}</p>}
    <ul id={memberListId} className="stack stack--tight">{configured.map(({ member, agent, readiness, status, providerName }) => {
      const work = !isError && connection === 'live' && member.room_id === roomId
        ? validateWorkState(member.work_state, roomId, member.agent_id) : null
      const memberName = name(member.agent_id)
      return <li key={member.agent_id} className="member-row" hidden={!visibleMembers.has(member.agent_id)} style={{ display: visibleMembers.has(member.agent_id) ? undefined : 'none' }}>
      <AgentCharacter agentId={member.agent_id} state={status.visual} />
      <div className="member-row__main"><div className="member-row__identity"><span className="member-row__name">{memberName}</span>{agent?.name && <span className="metadata">#{member.agent_id}</span>}
        <StatusText tone={status.tone} className="member-status"><span role="status" aria-label={t('members.statusAria', { name: agent?.name ? `${memberName} · #${member.agent_id}` : memberName })}>{status.label}</span></StatusText>
        {work && <AgentWorkDetails key={`${work.trigger_event_id}:${work.generation}:${work.scope}`} state={work} onNavigate={onNavigate} onAction={onAction} />}
        {connectionsQuery.isSuccess && <><details className="member-provider"><summary>{t('members.providerModel')}</summary>
            <p className={`member-readiness member-readiness--${readiness.state}`}>{t(readinessKeys[readiness.state])}</p>
            {readiness.connection && agent?.model_id && <p>{readiness.connection.label} · {providerName}<br /><span className="technical">{agent.model_id}</span></p>}
            {readiness.connection ? <Link className="text-link" to={`/connections?edit=${readiness.connection.id}`}>{t('members.openConnection')}</Link>
              : <Link className="text-link" to="/agents">{t('members.openAgent')}</Link>}
          </details></>}</div>
      {member.paused && member.resume_status && <p className="field-hint" role="status">{t(resumeKeys[member.resume_status] || 'members.resumeSettling')}</p>}
      {!readOnly && <div className="member-row__actions"><button disabled={blocked || reading || !membersQuery.isSuccess || !!(member.paused && member.resume_status && member.resume_status !== 'ready')} aria-label={`${t(member.paused ? 'members.resume' : 'members.pause')} ${memberName} · #${member.agent_id}`}
        onClick={() => { void change(member.paused ? 'resume' : 'pause', member.agent_id) }}>{t(member.paused ? 'members.resume' : 'members.pause')}</button>
      {canChangeMembers && <button className="danger-link" disabled={blocked || reading || !membersQuery.isSuccess} aria-label={t('members.removeAria', { name: `${memberName} · #${member.agent_id}` })}
        onClick={() => { void change('remove', member.agent_id) }}>{t('common.remove')}</button>}</div>}</div>
    </li>})}</ul>
    {!readOnly && canChangeMembers && <div className="stack stack--tight">
      {agentRead && <div className="stack stack--tight">
        <button type="button" className="text-link" disabled={agentRead.fetching || connectionsQuery.isFetching} onClick={() => {
          if (!current()) return
          agentRead.refresh(); void connectionsQuery.refetch({ cancelRefetch: false })
        }}>{t('members.refreshChoices')}</button>
        {agentRead.fetching && <p role="status" className="metadata">{t('members.agentsReading')}</p>}
        {agentRead.failed && <p role="alert" className="notice notice--warning">{t('members.agentsReadFailed')}</p>}
      </div>}
      {!connectionsQuery.isSuccess && <p role="status" className="notice notice--warning">
        {connectionsQuery.isError ? t('connections.loadFailure') : t('connections.loading')}
        {connectionsQuery.isError && <> <button className="text-link" onClick={() => { void connectionsQuery.refetch() }}>
          {connectionsQuery.isFetching ? t('connections.retrying') : t('common.retry')}
        </button></>}
      </p>}
      <DirectorySearch exactId value={choiceQuery} onChange={value => { if (current()) setChoiceQuery(value) }}
        label={t('members.choicesSearch')} hint={t('members.choicesSearchHint')} controls={`room-${roomId}-agent`}
        shown={matchedCandidates.length} total={candidates.length} loading={!!agentRead?.fetching || membersQuery.isFetching}
        known={membersQuery.isSuccess && agentRead?.ready !== false} preventSubmit />
      {choiceQuery.trim() && matchedCandidates.length === 0 && <p role="status" className="field-hint">{t('members.choicesNoMatch')}</p>}
      {selectedOutsideSearch && <p role="status" className="field-hint">{t('members.choicesSelectionHeld', { name: selectedAgent!.name })}</p>}
      {membersQuery.isSuccess && agentRead?.ready !== false && candidates.length === 0 && <p className="field-hint">
        {t('members.choicesEmpty')} <Link className="text-link" to="/agents">{t('members.openAgent')}</Link>
      </p>}
      <label className="sr-only" htmlFor={`room-${roomId}-agent`}>{t('members.addLabel')}</label>
      <select id={`room-${roomId}-agent`} value={selected} onChange={(e) => { const id = Number(e.target.value); selectedRef.current = { id, revision: selectedRef.current.revision + 1 }; setSelected(id) }}>
        <option value={0}>{t('members.choose')}</option>
        {choiceOptions.map((agent) => { const reason = availability(agent); return <option key={agent.id} value={agent.id} disabled={!!reason}>
          {agent.name} · #{agent.id}{agent.model_id && ` · ${agent.model_id}`}{reason && ` · ${t(reason)}`}
        </option> })}
      </select>
      <button className="button button--secondary" disabled={!selected || selectedUnavailable || blocked || reading || !membersQuery.isSuccess} onClick={addSelected}>{t('members.add')}</button>
    </div>}
    {latest && <div className="notice notice--warning stack stack--tight">
      <strong>{t('members.change.request', { action: actionName(latest.action), name: latest.agentName })}</strong>
      <p role={latest.status === 'unknown' || latest.status === 'rejected' ? 'alert' : 'status'}>{t(latest.status === 'sending' ? 'members.change.sending' : latest.status === 'unknown' ? 'members.change.unknown' : latest.status === 'rejected' ? 'members.change.rejected' : 'members.change.accepted')}</p>
      {latest.reason && resumeKeys[latest.reason] && <p>{t(resumeKeys[latest.reason])}</p>}
      {latest.needsRead && <p role="status">{t(latest.status === 'refreshing' ? 'common.loading' : 'members.change.refreshFailure')}</p>}
      {(latest.status === 'unknown' || latest.needsRead) && <button type="button" className="button button--secondary" disabled={reading || latest.status === 'refreshing'} onClick={() => { void inspect() }}>{t(reading ? 'common.loading' : 'members.change.inspect')}</button>}
      {latest.status === 'unknown' && latest.inspected && <p>{t('members.change.observed')}</p>}
      {latest.status === 'unknown' && !latest.acknowledged && latest.inspected && <button type="button" className="button button--secondary" disabled={reading} onClick={() => acknowledge(latest)}>{t('members.change.continue')}</button>}
      <p className="field-hint">{t('members.change.session')}</p>
    </div>}
    {readFailure && <p role="alert">{t('members.unavailable')}</p>}
    {records.some(row => row.status === 'unknown' && row.acknowledged) && <details>
      <summary>{t('members.change.earlier')}</summary>
      <p className="field-hint">{t('members.change.observed')}</p>
      <ul>{records.filter(row => row.status === 'unknown' && row.acknowledged).map(row => <li key={row.id}>{t('members.change.request', { action: actionName(row.action), name: row.agentName })}</li>)}</ul>
    </details>}
  </section>
}
