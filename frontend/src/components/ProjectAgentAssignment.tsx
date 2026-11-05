import { useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { projectsApi, type Project } from '../api/projects'
import type { Agent } from '../api/agents'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { useDialogFocus } from '../lib/useDialogFocus'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { editorSaveFailure } from './EditorSaveNotice'
import ListLoadNotice from './ListLoadNotice'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { projectAssignmentRequests, projectAssignmentsBlocked, startProjectAssignment,
  subscribeProjectAssignments, updateProjectAssignment, type ProjectAssignmentRequest } from '../lib/projectAssignmentRequests'

type AgentListState = { failed: boolean; fetching: boolean; pending: boolean; hasSnapshot: boolean; retry: () => void }
export default function ProjectAgentAssignment({ project, allAgents, agentListState, identity, onClose }: {
  project: Project; allAgents: Agent[]; agentListState: AgentListState; identity: string; onClose: () => void
}) {
  const { t } = useI18n(), cache = useQueryClient()
  const dialog = useRef<HTMLDivElement>(null)
  useDialogFocus(dialog, onClose)
  const [generation] = useState(directoryViewGeneration)
  const [agentQuery, setAgentQuery] = useState('')
  const [locate, setLocate] = useState<{ requestId: string; agentId: number; origin: HTMLElement; active: Element | null } | null>(null)
  const cardId = (id: number) => `project-${project.id}-assignment-agent-${id}`
  const listId = `project-${project.id}-assignment-search`
  const queryKey = ['project-assignment', identity, project.id] as const
  const sameOwner = () => { const auth = useAuthStore.getState(); return !!auth.token && `${auth.user?.id || 0}:${auth.epoch}` === identity && generation === directoryViewGeneration() }
  const rows = useSyncExternalStore(subscribeProjectAssignments, () => projectAssignmentRequests(identity, project.id))
  const readProject = async () => {
    if (!sameOwner()) throw new Error('Owner changed')
    const value = await projectsApi.get(project.id)
    if (!sameOwner() || value.id !== project.id || value.user_id !== useAuthStore.getState().user?.id || !Array.isArray(value.agents)
      || value.agents.some(agent => !Number.isSafeInteger(agent.id) || agent.id <= 0 || agent.user_id !== value.user_id)
      || new Set(value.agents.map(agent => agent.id)).size !== value.agents.length) throw new Error('Project assignment read unavailable')
    return value
  }
  const snapshot = useQuery({ queryKey, queryFn: readProject, retry: false, refetchOnWindowFocus: false })
  const pending = rows.some(row => row.status === 'sending')
  const readCurrent = async () => {
    if (!sameOwner() || projectAssignmentRequests(identity, project.id).some(row => row.status === 'sending')) return
    const captured = projectAssignmentRequests(identity, project.id).filter(row => row.status !== 'sending').map(row => row.id)
    try {
      await cache.fetchQuery({ queryKey, queryFn: readProject, retry: false, staleTime: 0 })
      if (sameOwner()) captured.forEach(id => updateProjectAssignment(identity, project.id, id, generation, { needsRead: false, inspected: true }))
    } catch { /* The query exposes the read error without replacing the last snapshot. */ }
  }
  const canChange = () => {
    const state = cache.getQueryState<Project>(queryKey)
    return sameOwner() && !projectAssignmentsBlocked(projectAssignmentRequests(identity, project.id))
      && state?.status === 'success' && state.fetchStatus === 'idle' && !state.isInvalidated && state.data?.is_active === true
  }
  const change = async (agent: Agent, action: 'add' | 'remove') => {
    if (!canChange() || action === 'add' && (agentListState.failed || agentListState.pending || !agent.is_active)) return
    const current = cache.getQueryData<Project>(queryKey)
    const assigned = !!current?.agents?.some(value => value.id === agent.id)
    if (action === 'add' ? assigned : !assigned) return
    const sent: ProjectAssignmentRequest = Object.freeze({ id: crypto.randomUUID(), identity, projectId: project.id,
      agentId: agent.id, agentName: agent.name, action, status: 'sending', needsRead: false, inspected: false, acknowledged: false })
    if (!startProjectAssignment(sent, generation)) return
    try {
      await cache.cancelQueries({ queryKey, exact: true })
      if (!sameOwner()) return
      if (action === 'add') await projectsApi.addAgent(sent.projectId, sent.agentId)
      else await projectsApi.removeAgent(sent.projectId, sent.agentId)
      if (sameOwner()) updateProjectAssignment(identity, project.id, sent.id, generation, { status: 'accepted', needsRead: true })
    } catch (failure) {
      if (sameOwner()) updateProjectAssignment(identity, project.id, sent.id, generation,
        { status: editorSaveFailure(failure) === 'unknown' ? 'unknown' : 'rejected', needsRead: true })
    } finally {
      if (sameOwner()) {
        void cache.invalidateQueries({ queryKey: ['projects'] })
        await cache.invalidateQueries({ queryKey, exact: true, refetchType: 'none' })
        if (projectAssignmentRequests(identity, project.id).find(row => row.id === sent.id)?.status === 'accepted') await readCurrent()
      }
    }
  }
  const acknowledge = (row: ProjectAssignmentRequest) => {
    const current = projectAssignmentRequests(identity, project.id).find(value => value.id === row.id)
    if (!sameOwner() || !current?.inspected || current.needsRead || current.status !== 'unknown' || !window.confirm(t('projects.assignment.ackConfirm'))) return
    updateProjectAssignment(identity, project.id, row.id, generation, { acknowledged: true })
  }
  const assigned = snapshot.data?.agents || []
  const assignedIds = new Set(assigned.map(agent => agent.id))
  const available = snapshot.data ? allAgents.filter(agent => agent.is_active && !assignedIds.has(agent.id)) : []
  const matches = directoryMatcher(agentQuery)
  const matchesAgent = (agent: Agent) => matches([agent.name, agent.description, String(agent.id), `#${agent.id}`, agent.model_id, agent.model], agent.id)
  const shownAssigned = assigned.filter(matchesAgent), shownAvailable = available.filter(matchesAgent)
  const receivedLocation = (id: number) => !snapshot.data ? 'unread'
    : assignedIds.has(id) ? 'assigned' : !agentListState.hasSnapshot ? 'unread'
      : available.some(agent => agent.id === id) ? 'available' : 'missing'
  const showAgent = (row: ProjectAssignmentRequest, origin: HTMLButtonElement) => {
    const live = projectAssignmentRequests(identity, project.id).find(value => value.id === row.id && value.agentId === row.agentId)
    const state = cache.getQueryState<Project>(queryKey)
    const location = receivedLocation(row.agentId)
    if (!sameOwner() || !live || !origin.isConnected || !dialog.current?.contains(origin)
      || document.visibilityState !== 'visible' || !document.hasFocus() || snapshot.isFetching || agentListState.fetching
      || state?.dataUpdatedAt !== snapshot.dataUpdatedAt || (location !== 'assigned' && location !== 'available')) return
    // An explicit reveal may clear this view's filter. It never reads or writes
    // membership, acknowledges an unknown command, or focuses an action button.
    setLocate({ requestId: row.id, agentId: row.agentId, origin, active: document.activeElement })
    setAgentQuery('')
  }
  useLayoutEffect(() => {
    if (!locate) return
    setLocate(null)
    const active = document.activeElement, root = dialog.current
    if (!sameOwner() || !root || agentQuery || snapshot.isFetching || agentListState.fetching
      || document.visibilityState !== 'visible' || !document.hasFocus() || !locate.origin.isConnected
      || !root.contains(locate.origin) || active !== locate.active && active !== locate.origin
      || locate.origin.closest('[aria-modal="true"]') !== root
      || active !== document.body && active?.closest('[aria-modal="true"]') !== root
      || !projectAssignmentRequests(identity, project.id).some(row => row.id === locate.requestId && row.agentId === locate.agentId)) return
    const top = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node => !node.hidden && node.getClientRects().length > 0).pop()
    if (top !== root) return
    const card = document.getElementById(cardId(locate.agentId))
    if (!card || !root.contains(card)) return
    card.focus({ preventScroll: true }); card.scrollIntoView({ block: 'nearest' })
  }, [locate, agentQuery])
  const enabled = canChange()
  const agentRow = (agent: Agent, action: 'add' | 'remove') => <div key={agent.id} id={cardId(agent.id)} tabIndex={-1} className="surface split">
    <div><p className="section-title">{agent.name}</p><p className="metadata">#{agent.id} · {agent.model_id || agent.model || t('projects.modelUnspecified')}{!agent.is_active && ` · ${t('projects.agentInactive')}`}</p><p className="section-copy">{agent.description}</p></div>
    <button type="button" className={`button button--ghost ${action === 'remove' ? 'danger-link' : 'text-link'}`}
      aria-label={`${t(action === 'add' ? 'projects.add' : 'common.remove')} ${agent.name} · #${agent.id}`}
      disabled={!enabled || action === 'add' && (agentListState.failed || agentListState.pending)}
      onClick={() => void change(agent, action)}>{t(action === 'add' ? 'projects.add' : 'common.remove')}</button>
  </div>
  return <div className="modal-backdrop"><div ref={dialog} role="dialog" aria-modal="true" aria-label={t('projects.manageAgents')} className="modal-panel stack">
    <h2 className="page-title">{snapshot.data?.name || project.name} · {t('projects.manageAgents')}</h2>
    <p className="section-copy">{t('projects.assignment.scope')}</p>
    {snapshot.isPending && <p role="status">{t('common.loading')}</p>}
    {snapshot.isError && <p role="alert" className="notice notice--danger">{t('projects.assignment.readFailure')}</p>}
    {snapshot.data && (snapshot.isError || snapshot.isFetching) && <p className="metadata">{t('projects.assignment.lastSnapshot')}</p>}
    <div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={pending || snapshot.isFetching} onClick={() => void readCurrent()}>{t('projects.assignment.readCurrent')}</button>
      <button type="button" className="button button--secondary button--compact" disabled={agentListState.fetching} onClick={() => { if (sameOwner()) agentListState.retry() }}>{t('projects.assignment.readAgents')}</button></div>
    {snapshot.data && !snapshot.data.is_active && <p role="status">{t('projects.assignment.inactive')}</p>}
    <DirectorySearch exactId value={agentQuery} onChange={value => { if (sameOwner()) { setLocate(null); setAgentQuery(value) } }}
      label={t('projects.assignment.search')} hint={t('projects.setup.teamSearchHint')} controls={`${listId}-assigned ${listId}-available`}
      shown={shownAssigned.length + shownAvailable.length} total={assigned.length + available.length}
      known={!!snapshot.data && agentListState.hasSnapshot} loading={snapshot.isFetching || agentListState.fetching} preventSubmit />
    <section id={listId + '-assigned'} className="stack stack--tight"><h3 className="section-title">{t('projects.assigned')} {snapshot.data && <span className="metadata">{t('directory.matches', { shown: shownAssigned.length, total: assigned.length })}</span>}</h3>
      {snapshot.data ? shownAssigned.length ? shownAssigned.map(agent => agentRow(agent, 'remove')) : <p>{t(assigned.length ? 'projects.assignment.noSearchMatch' : 'projects.noneAssigned')}</p> : <p>{t('projects.assignment.notRead')}</p>}
    </section>
    <ListLoadNotice failed={agentListState.failed} fetching={agentListState.fetching} hasSnapshot={agentListState.hasSnapshot} message={t('agents.listFailure')} onRetry={agentListState.retry} />
    {agentListState.pending && <p role="status">{t('common.loading')}</p>}
    <section id={listId + '-available'} className="stack stack--tight"><h3 className="section-title">{t('projects.available')} {snapshot.data && agentListState.hasSnapshot && <span className="metadata">{t('directory.matches', { shown: shownAvailable.length, total: available.length })}</span>}</h3>
      {!snapshot.data ? <p>{t('projects.assignment.notRead')}</p> : !agentListState.hasSnapshot ? <p>{t('projects.assignment.agentListNeeded')}</p>
        : shownAvailable.length ? shownAvailable.map(agent => agentRow(agent, 'add')) : <p>{t(available.length ? 'projects.assignment.noSearchMatch' : 'projects.assignment.noAvailable')}</p>}
    </section>
    {rows.length > 0 && <section className="stack stack--tight" aria-label={t('projects.assignment.requests')}>
      <h3 className="section-title">{t('projects.assignment.requests')}</h3>
      <p className="field-hint">{t('projects.assignment.session')}</p>
      <p className="field-hint">{t('projects.assignment.observationOnly')}</p>
      {rows.map(row => <div className="notice notice--info stack stack--tight" key={row.id}>
        <strong>{row.agentName} · #{row.agentId} · {t(row.action === 'add' ? 'projects.add' : 'common.remove')}</strong>
        <p role={row.status === 'unknown' || row.status === 'rejected' ? 'alert' : 'status'}>{t(row.status === 'sending' ? 'projects.assignment.sending' : row.status === 'accepted' ? 'projects.assignment.accepted' : row.status === 'unknown' ? 'projects.assignment.unknown' : 'editor.rejected')}</p>
        <p className="metadata">{t(receivedLocation(row.agentId) === 'assigned' ? 'projects.assignment.locatedAssigned'
          : receivedLocation(row.agentId) === 'available' ? 'projects.assignment.locatedAvailable'
            : receivedLocation(row.agentId) === 'missing' ? 'projects.assignment.notInLists' : 'projects.assignment.notRead')}</p>
        {(receivedLocation(row.agentId) === 'assigned' || receivedLocation(row.agentId) === 'available') && <button type="button" className="button button--secondary button--compact"
          disabled={snapshot.isFetching || agentListState.fetching} onClick={event => showAgent(row, event.currentTarget)}>{t('projects.assignment.showAgent')}</button>}
        {row.needsRead && <p>{t('projects.assignment.needsRead')}</p>}
        {row.status === 'unknown' && <><p>{t('projects.assignment.readNotReceipt')}</p>
          {!row.acknowledged ? <button type="button" className="button button--secondary button--compact" disabled={!row.inspected || row.needsRead || pending || snapshot.isFetching} onClick={() => acknowledge(row)}>{t('projects.assignment.acknowledge')}</button>
            : <p>{t('projects.assignment.acknowledged')}</p>}</>}
      </div>)}
    </section>}
    <div className="page-actions"><Link to="/agents" className="button button--secondary">{t('agents.title')}</Link><button type="button" onClick={onClose} className="button button--primary">{t('projects.done')}</button></div>
  </div></div>
}
