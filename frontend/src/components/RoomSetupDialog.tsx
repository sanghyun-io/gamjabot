import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { Project } from '../api/projects'
import { projectsApi } from '../api/projects'
import type { Agent } from '../api/agents'
import type { RoomLaunch } from '../api/conversations'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { useViewNavigationIntent } from '../lib/useViewNavigationIntent'
import { useDialogFocus } from '../lib/useDialogFocus'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { isRoomSetupResult, requestRoomSetupAction, roomSetupOwnerCurrent } from '../lib/roomSetupActions'
import { createdFromRoomSetup, defaultRoomSetup, initializeRoomSetup, patchRoomSetup, readRoomSetup, roomCreationSignature, roomSetupBlocked, roomSetupFingerprint, roomSetupVersion, subscribeRoomSetup, type RoomSetupCommand, type RoomSetupDraft } from '../lib/roomSetupWorkspace'
import ListLoadNotice from './ListLoadNotice'
import RoomTemplates from './RoomTemplates'
import RoomSetupHistory from './RoomSetupHistory'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'

type AgentListState = { failed: boolean; fetching: boolean; pending: boolean; hasSnapshot: boolean; retry: () => void }
export default function RoomSetupDialog({ project, agents, agentListState, identity, authEpoch, onClose, onCreated }: { project: Project; agents: Agent[]; agentListState: AgentListState; identity: string; authEpoch: number; onClose: () => void; onCreated: (roomId: number) => void }) {
  const { t, formatNumber } = useI18n(), generation = useRef(directoryViewGeneration()).current
  const formRef = useRef<HTMLFormElement>(null), navigation = useViewNavigationIntent()
  const dialog = useRef<HTMLDivElement>(null), alive = useRef(false), closed = useRef(false), submitted = useRef<string | null>(null)
  const [error, setError] = useState<'validation' | 'agents' | ''>('')
  const [teamQuery, setTeamQuery] = useState('')
  const [errorFingerprint, setErrorFingerprint] = useState('')
  type SetupField = 'title' | 'rounds' | 'tools' | 'seconds' | 'participants'
  const [focusIssue, setFocusIssue] = useState<{ field: SetupField; fingerprint: string } | null>(null)
  const fieldId = (field: SetupField) => `room-setup-${project.id}-${field}`
  useSyncExternalStore(subscribeRoomSetup, roomSetupVersion)
  const workspace = readRoomSetup(identity), record = workspace.drafts.get(project.id)
  const [initial] = useState(() => defaultRoomSetup(project)), draft = record?.value || initial
  const visibleError = errorFingerprint === roomSetupFingerprint(draft) ? error : ''
  const valid = () => roomSetupOwnerCurrent(identity, generation) && authEpoch === useAuthStore.getState().epoch
  const close = () => { if (valid()) { closed.current = true; submitted.current = null; navigation.cancel(); onClose() } }
  useDialogFocus(dialog, close)
  useLayoutEffect(() => { if (valid()) initializeRoomSetup(identity, generation, project) }, [])
  useEffect(() => { alive.current = true; closed.current = false; return () => { alive.current = false; submitted.current = null } }, [])
  const target = useQuery({ queryKey: ['room-setup-project', identity, project.id], retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    queryFn: async () => { if (!valid()) throw new Error('Owner changed'); const value = await projectsApi.get(project.id); if (!valid() || value.id !== project.id || value.user_id !== useAuthStore.getState().user?.id) throw new Error('Project target changed'); return value } })
  const targetBlocked = target.isFetching || target.isError || !target.data?.is_active
  const agentsReady = !agentListState.failed && !agentListState.pending
  const pending = !!workspace.pending, blocked = roomSetupBlocked(identity), ime = useImeFormGuard(`${identity}:new-room:${project.id}`)
  const change = (patch: Partial<RoomSetupDraft>) => { if (valid() && patchRoomSetup(identity, generation, project.id, patch)) { setError(''); setFocusIssue(null) } }
  const started = (command: RoomSetupCommand) => { if (valid() && (command.kind === 'launch' || command.kind === 'template-room')) { submitted.current = command.id; navigation.begin(command.id, formRef.current) } }
  useEffect(() => {
    const command = workspace.commands.find(value => value.id === submitted.current)
    if (!command || !valid() || !alive.current || closed.current) return
    if (command.waitStopped || workspace.commands[workspace.commands.length - 1]?.id !== command.id || command.kind === 'template-room' && !draft.templatesOpen) { submitted.current = null; navigation.cancel(); return }
    if (command.phase === 'succeeded' && command.result && isRoomSetupResult(command.result)) {
      submitted.current = null
      const current = readRoomSetup(identity).drafts.get(project.id)?.value
      if (current && navigation.current(command.id) && roomSetupFingerprint(current) === command.draftFingerprint) { closed.current = true; onCreated(command.result.id) }
    } else if (command.phase !== 'pending') { submitted.current = null; navigation.cancel() }
  }, [workspace.commands, draft.templatesOpen])
  const integer = (value: string, min: number, max: number) => { if (!/^(0|[1-9]\d*)$/.test(value)) return null; const n = Number(value); return Number.isSafeInteger(n) && n >= min && n <= max ? n : null }
  const fieldProblem = (value: RoomSetupDraft): SetupField | null => {
    if (!value.title.trim() || value.title.trim().length > 200) return 'title'
    if (integer(value.rounds, 1, 4) === null) return 'rounds'
    if (integer(value.tools, 0, 64) === null) return 'tools'
    if (integer(value.seconds, 1, 3600) === null) return 'seconds'
    if (new Set(value.selected).size > 64) return 'participants'
    return null
  }
  const invalid = (field: SetupField) => visibleError === 'validation' && (field === 'title'
    ? !draft.title.trim() || draft.title.trim().length > 200
    : field === 'participants' ? new Set(draft.selected).size > 64
      : integer(draft[field], field === 'tools' ? 0 : 1, field === 'rounds' ? 4 : field === 'tools' ? 64 : 3600) === null)
  const reportIssue = (kind: 'validation' | 'agents', field: SetupField, value: RoomSetupDraft) => {
    setError(kind); setErrorFingerprint(roomSetupFingerprint(value)); setFocusIssue({ field, fingerprint: roomSetupFingerprint(value) })
  }
  useLayoutEffect(() => {
    if (!focusIssue || !valid() || !alive.current || closed.current || workspace.pending) return
    const form = formRef.current, active = document.activeElement
    const value = readRoomSetup(identity).drafts.get(project.id)?.value
    // Focus only this still-current form. No deferred focus can follow a close,
    // another dialog, an Owner switch, or a changed retained draft.
    if (!form || !value || roomSetupFingerprint(value) !== focusIssue.fingerprint
      || !active || !form.contains(active) || active.closest('[aria-modal="true"]') !== dialog.current) return
    const input = form.querySelector<HTMLElement>(`[data-setup-field="${focusIssue.field}"]`)
    if (!input || input.matches(':disabled')) return
    for (let parent = input.parentElement; parent && form.contains(parent); parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) parent.open = true
    }
    input.focus({ preventScroll: true }); input.scrollIntoView({ block: 'nearest' })
  }, [focusIssue])
  const launchBody = (value: RoomSetupDraft): RoomLaunch | null => {
    const rounds = integer(value.rounds, 1, 4), tools = integer(value.tools, 0, 64), seconds = integer(value.seconds, 1, 3600), ids = [...new Set(value.selected)].sort((a, b) => a - b)
    if (!value.title.trim() || value.title.trim().length > 200 || rounds === null || tools === null || seconds === null || ids.length > 64) return null
    return { project_id: project.id, title: value.title.trim(), agent_ids: ids, max_rounds: rounds, max_tool_calls: tools, max_executor_seconds: seconds }
  }
  const body = launchBody(draft), previousRoom = body && createdFromRoomSetup(identity, roomCreationSignature('launch', project.id, draft.intent, body))
  const create = (event: React.FormEvent) => {
    event.preventDefault()
    if (ime.isSubmitBlocked(event) || !valid() || blocked || targetBlocked || !agentsReady) return
    const value = readRoomSetup(identity).drafts.get(project.id)?.value
    if (!value || value.templateId !== null) return
    const input = launchBody(value)
    if (!input) { reportIssue('validation', fieldProblem(value) || 'title', value); return }
    if (input.agent_ids.some(id => !agents.some(agent => agent.id === id && agent.is_active))) { reportIssue('agents', 'participants', value); return }
    const original = requestRoomSetupAction(identity, generation, project, 'launch', input)
    if (original) started(original)
  }
  const selectedIds = new Set(draft.selected)
  const teamMatches = directoryMatcher(teamQuery)
  const matchingAgents = agents.filter(agent => teamMatches([agent.name, agent.description, String(agent.id), `#${agent.id}`, agent.model_id, agent.model], agent.id))
  const matchingIds = new Set(matchingAgents.map(agent => agent.id))
  const visibleAgents = agents.filter(agent => matchingIds.has(agent.id) || selectedIds.has(agent.id))
  const selectedOutsideSearch = teamQuery.trim() ? [...selectedIds].filter(id => !matchingIds.has(id)).length : 0
  const another = () => { if (valid() && !pending && window.confirm(t('projects.setup.anotherConfirm'))) change({ intent: crypto.randomUUID() }) }
  const clear = () => { if (valid() && !pending && window.confirm(t('projects.setup.clearConfirm'))) change(defaultRoomSetup(target.data || project)) }
  return <div className="modal-backdrop"><div ref={dialog} role="dialog" aria-modal="true" aria-label={t('projects.createRoom')} className="modal-panel stack" onKeyDownCapture={event => { if (event.key === 'Escape' && (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229)) event.stopPropagation() }}>
    <h2 className="page-title">{t('projects.newRoomIn', { project: project.name })}</h2><p>{t('projects.roomHelp')}</p>
    <p className="field-hint">{t('projects.setup.draftScope')}</p>
    <RoomSetupHistory identity={identity} projectId={project.id}/>
    <div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={target.isFetching} onClick={() => { if (valid()) void target.refetch({ cancelRefetch: false }) }}>{t('projects.workspace.readTarget')}</button><button type="button" className="button button--secondary button--compact" disabled={pending} onClick={clear}>{t('projects.setup.clear')}</button><button type="button" className="button button--secondary button--compact" disabled={pending} onClick={another}>{t('projects.setup.another')}</button></div>
    {target.isFetching && <p role="status">{t('common.loading')}</p>}{target.isError && <p role="alert">{t('projects.workspace.targetError')}</p>}{target.data && !target.data.is_active && <p>{t('projects.workspace.archivedTarget')}</p>}
    <form ref={formRef} {...ime.handlers} onSubmit={create} noValidate className="form-grid">
      <label className="field">{t('projects.roomTitle')}<input data-dialog-initial data-setup-field="title" disabled={pending} required maxLength={200}
        aria-invalid={invalid('title') || undefined} aria-describedby={invalid('title') ? fieldId('title') : undefined}
        value={draft.title} onChange={event => change({ title: event.target.value })}/>
        {invalid('title') && <span id={fieldId('title')} className="field-hint notice--danger">{t('projects.roomTitleValidation')}</span>}
      </label>
      <ListLoadNotice failed={agentListState.failed} fetching={agentListState.fetching} hasSnapshot={agentListState.hasSnapshot} message={t('agents.listFailure')} onRetry={agentListState.retry}/>
      {agentListState.pending && <p role="status">{t('common.loading')}</p>}
      <DirectorySearch exactId value={teamQuery} onChange={value => { if (valid()) setTeamQuery(value) }}
        label={t('projects.setup.teamSearch')} hint={t('projects.setup.teamSearchHint')} controls={fieldId('participants') + '-list'}
        shown={matchingAgents.length} total={agents.length} known={agentListState.hasSnapshot} loading={agentListState.fetching} preventSubmit />
      <p className="metadata" role="status">{t('projects.templates.memberCount', { count: selectedIds.size })}</p>
      {agentListState.hasSnapshot && teamQuery.trim() && matchingAgents.length === 0 && <p role="status" className="field-hint">{t('projects.setup.teamNoMatch')}</p>}
      {selectedOutsideSearch > 0 && <p className="field-hint">{t('projects.setup.teamSelectionKept', { count: formatNumber(selectedOutsideSearch) })}</p>}
      <fieldset id={fieldId('participants') + '-list'} data-setup-field="participants" tabIndex={-1} aria-invalid={invalid('participants') || visibleError === 'agents' || undefined} aria-describedby={invalid('participants') || visibleError === 'agents' ? fieldId('participants') : undefined} disabled={!agentsReady || pending} className="stack stack--tight"><legend className="section-title">{t('projects.participants')}</legend>{visibleAgents.map(agent => {
        const checked = draft.selected.includes(agent.id)
        return <label key={agent.id} className="check-row"><input type="checkbox" checked={checked} disabled={!checked && (!agent.is_active || selectedIds.size >= 64)} onChange={event => { const current = readRoomSetup(identity).drafts.get(project.id)?.value; if (current) change({ selected: event.target.checked ? [...current.selected, agent.id] : current.selected.filter(id => id !== agent.id) }) }}/>{agent.name} · #{agent.id} · {agent.model_id || agent.model || t('projects.modelUnspecified')}{!agent.is_active && ` · ${t('projects.agentInactive')}`}</label>
      })}{(agentsReady ? draft.selected : []).filter(id => !agents.some(agent => agent.id === id)).map(id => <label key={id} className="check-row"><input type="checkbox" checked onChange={() => change({ selected: draft.selected.filter(value => value !== id) })}/>{t('projects.templates.missingMember', { id })}</label>)}</fieldset>
      {(invalid('participants') || visibleError === 'agents') && <p id={fieldId('participants')} className="field-hint notice--danger">{t(visibleError === 'agents' ? 'projects.launch.agentsUnavailable' : 'projects.setup.participantLimit')}</p>}
      {agentsReady && draft.selected.length === 0 && <p className="metadata">{t('projects.launch.emptyTeam')}</p>}
      <details className="compact-details"><summary>{t('projects.launch.policy')}</summary><div className="room-policy__fields">{(['rounds', 'tools', 'seconds'] as const).map((field, index) => <label key={field} className="field">{t(index === 0 ? 'chat.policy.rounds' : index === 1 ? 'chat.policy.tools' : 'chat.policy.executor')}<input data-setup-field={field} disabled={pending} type="number" inputMode="numeric" min={index === 1 ? 0 : 1} max={index === 0 ? 4 : index === 1 ? 64 : 3600} step="1"
        aria-invalid={invalid(field) || undefined} aria-describedby={fieldId(field)} value={draft[field]} onChange={event => change({ [field]: event.target.value })}/>
        <span id={fieldId(field)} className={`field-hint${invalid(field) ? ' notice--danger' : ''}`}>{t(index === 0 ? 'chat.policy.roundsHint' : index === 1 ? 'chat.policy.toolsHint' : 'chat.policy.executorHint')}</span></label>)}</div></details>
      {record && <RoomTemplates project={project} agents={agents} identity={identity} authEpoch={authEpoch} externalBusy={targetBlocked || !agentsReady} onActionStarted={started} onSharedFieldIssue={reportIssue}/>}
      {visibleError && <p role="alert" className="notice notice--danger">{t(visibleError === 'validation' ? 'projects.launch.validation' : 'projects.launch.agentsUnavailable')}</p>}
      {previousRoom && <p>{t('projects.setup.alreadyCreated')}</p>}
      <div className="page-actions"><button type="button" className="button button--secondary" onClick={close}>{t('common.close')}</button>{draft.templateId === null && <button className="button button--primary" disabled={pending || blocked || targetBlocked || !agentsReady || !!previousRoom}>{t('projects.createRoom')}</button>}</div>
    </form>
  </div></div>
}
