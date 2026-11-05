import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { Link } from 'react-router-dom'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import type { MessageKey } from '../i18n/locales/en'
import { conversationsApi } from '../api/conversations'
import { roomTemplatesApi } from '../api/roomTemplates'
import type { Project } from '../api/projects'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { isRoomSetupResult, requestRoomSetupAction, roomSetupOwnerCurrent } from '../lib/roomSetupActions'
import { acknowledgeRoomSetupUnknown, applyTemplateToRoomSetup, readRoomSetup, roomSetupBlocked, roomSetupVersion, stopRoomSetupWait, subscribeRoomSetup, type RoomSetupCommand } from '../lib/roomSetupWorkspace'

function setupActionKey(command: RoomSetupCommand): MessageKey {
  return command.kind === 'launch' ? 'projects.createRoom' : command.kind === 'template-room' ? 'projects.templates.use' : command.kind === 'template-delete' ? 'projects.templates.delete' : command.kind === 'template-update' ? 'projects.templates.saveChanges' : 'projects.templates.saveNew'
}
function setupPhaseKey(command: RoomSetupCommand): MessageKey {
  return command.phase === 'pending' ? 'projects.workspace.pending' : command.phase === 'unknown' ? 'projects.workspace.unknown' : command.phase === 'rejected' ? 'editor.rejected' : 'projects.setup.succeeded'
}

type SetupObservation = { kind: 'room'; id: number; name: string; active: boolean }
  | { kind: 'template'; id: number; name: string; title: string; agentIds: number[]; rounds: number; tools: number; seconds: number }
const positiveId = (value: number) => Number.isSafeInteger(value) && value > 0
const rangeInteger = (value: number, min: number, max: number) => Number.isSafeInteger(value) && value >= min && value <= max

function SetupResult({ identity, command, onResume }: { identity: string; command: RoomSetupCommand; onResume?: (project: Project) => void }) {
  const { t, formatDateTime, formatNumber } = useI18n(), generation = useRef(directoryViewGeneration()).current
  const [reading, setReading] = useState(false), [observed, setObserved] = useState<SetupObservation[] | null>(null), [failed, setFailed] = useState(false)
  const pending = useRef(false), mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const valid = () => roomSetupOwnerCurrent(identity, generation)
  const inspect = async () => {
    if (!valid() || pending.current) return
    pending.current = true; setReading(true); setFailed(false)
    try {
      const rooms = command.kind === 'launch' || command.kind === 'template-room'
      const owner = useAuthStore.getState().user?.id
      let values: SetupObservation[]
      if (rooms) {
        const records = await conversationsApi.listByProject(command.target.projectId, 'all')
        if (!Array.isArray(records) || records.some(value => !positiveId(value.id) || value.project_id !== command.target.projectId || value.user_id !== owner)
          || new Set(records.map(value => value.id)).size !== records.length) throw new Error('Observed room scope changed')
        values = records.filter(value => value.title === command.target.title).map((value): SetupObservation => {
          if (typeof value.is_active !== 'boolean') throw new Error('Observed room unavailable')
          return { kind: 'room', id: value.id, name: value.title, active: value.is_active }
        })
      } else {
        const records = await roomTemplatesApi.list(command.target.projectId)
        if (!Array.isArray(records) || records.some(value => !positiveId(value.id) || value.project_id !== command.target.projectId || value.user_id !== owner)
          || new Set(records.map(value => value.id)).size !== records.length) throw new Error('Observed template scope changed')
        values = records.filter(value => command.target.templateId ? value.id === command.target.templateId : value.name === command.target.name).map((value): SetupObservation => {
          if (typeof value.name !== 'string' || typeof value.room_title !== 'string' || !Array.isArray(value.agent_ids)
            || value.agent_ids.length > 64 || value.agent_ids.some(id => !positiveId(id)) || new Set(value.agent_ids).size !== value.agent_ids.length
            || !rangeInteger(value.max_rounds, 1, 4) || !rangeInteger(value.max_tool_calls, 0, 64) || !rangeInteger(value.max_executor_seconds, 1, 3600)) throw new Error('Observed template unavailable')
          return { kind: 'template', id: value.id, name: value.name, title: value.room_title, agentIds: [...value.agent_ids],
            rounds: value.max_rounds, tools: value.max_tool_calls, seconds: value.max_executor_seconds }
        })
      }
      if (valid() && mounted.current) setObserved(values)
    } catch { if (valid() && mounted.current) setFailed(true) }
    finally { pending.current = false; if (valid() && mounted.current) setReading(false) }
  }
  const retry = () => {
    const project = readRoomSetup(identity).drafts.get(command.target.projectId)?.project
    if (!project || !valid() || !window.confirm(t('projects.setup.retryManualConfirm'))) return
    requestRoomSetupAction(identity, generation, project, 'launch', undefined, undefined, command.id)
  }
  const useReceived = () => {
    if (!valid() || !command.result || isRoomSetupResult(command.result) || roomSetupBlocked(identity)) return
    if (!window.confirm(t('projects.setup.replaceDraft'))) return
    if (valid() && applyTemplateToRoomSetup(identity, generation, command.result)) {
      const project = readRoomSetup(identity).drafts.get(command.target.projectId)?.project
      if (project) onResume?.(project)
    }
  }
  const key = setupActionKey(command)
  return <div className={`notice stack stack--tight ${command.phase === 'succeeded' ? 'notice--success' : command.phase === 'pending' ? 'notice--info' : 'notice--warning'}`}>
    <p className="metadata">{t(key)} · #{command.target.projectId} · {command.target.projectName}{command.target.templateId && <> · #{command.target.templateId}</>}</p>
    <p><strong>{command.target.title || command.target.name}</strong></p>
    <p className="metadata">{t('projects.workspace.started', { time: formatDateTime(command.startedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>
    <p role={command.phase === 'unknown' || command.phase === 'rejected' ? 'alert' : 'status'}>{t(setupPhaseKey(command))}</p>
    {command.finishedAt && <p className="metadata">{t('projects.workspace.received', { time: formatDateTime(command.finishedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
    {command.result && isRoomSetupResult(command.result) && <p>{t('projects.setup.originalRoom')} · #{command.result.id} · {command.result.title} <Link className="text-link" to={`/chat/${command.result.id}`} onClick={event => { if (!valid()) event.preventDefault() }}>{t('projects.openCreatedRoom')}</Link></p>}
    {command.result && !isRoomSetupResult(command.result) && <><p>#{command.result.id} · {command.result.name}</p><button type="button" className="button button--secondary button--compact" disabled={roomSetupBlocked(identity)} onClick={useReceived}>{t('projects.setup.useReceived')}</button></>}
    {command.phase === 'pending' && <button type="button" className="button button--secondary button--compact" onClick={() => { if (valid() && window.confirm(t('projects.workspace.stopConfirm')) && valid()) stopRoomSetupWait(identity, generation, command.id) }}>{t('projects.workspace.stopWaiting')}</button>}
    {command.phase === 'unknown' && <>
      <button type="button" className="button button--secondary button--compact" disabled={reading} onClick={() => void inspect()}>{t('editor.inspect')}</button>
      {failed && <p role="alert">{t('editor.inspectFailure')}</p>}
      {observed && <><p>{t('projects.setup.observationOnly')}</p>
        {(reading || failed) && <p className="metadata" role="status">{t('projects.setup.previousObservation')}</p>}
        {observed.length ? <ul className="stack stack--tight">{observed.map(value => <li key={`${value.kind}:${value.id}`}>
          <p>#{formatNumber(value.id)} · {value.name}</p>
          {value.kind === 'room' ? <>
            {!value.active && <p className="metadata">{t('projects.closedRoom')}</p>}
            <Link className="text-link" to={`/chat/${value.id}`} onClick={event => { if (!valid() || !mounted.current) event.preventDefault() }}>{t('projects.setup.openObservedRoom')}</Link>
          </> : <details className="compact-details"><summary>{t('projects.setup.observedTemplateDetails')}</summary>
            <p>{value.title}</p><p>{t('projects.templates.memberCount', { count: value.agentIds.length })}</p>
            {value.agentIds.length > 0 && <p className="technical">{value.agentIds.map(id => `#${id}`).join(' · ')}</p>}
            <p>{t('projects.cloneSettings', { rounds: value.rounds, tools: value.tools, seconds: value.seconds })}</p>
          </details>}
        </li>)}</ul> : <p>{t('editor.noMatch')}</p>}
      </>}
      {!command.acknowledged && <button type="button" className="button button--secondary button--compact" disabled={reading || failed || !observed} onClick={() => { if (valid() && window.confirm(t('projects.setup.newActionConfirm')) && valid()) acknowledgeRoomSetupUnknown(identity, generation, command.id) }}>{t('projects.workspace.allowNew')}</button>}
    </>}
    {command.kind === 'launch' && ['unknown', 'rejected'].includes(command.phase) && command.code !== 'room_launch_idempotency_conflict' && <button type="button" className="button button--secondary button--compact" disabled={roomSetupBlocked(identity, command.id)} onClick={retry}>{t('projects.setup.retryManual')}</button>}
    {command.code === 'room_template_name_conflict' && <p>{t('projects.templates.nameConflict')}</p>}
    {command.code === 'room_template_members_unavailable' && <p>{t('projects.templates.membersUnavailable')}</p>}
    {command.code === 'room_launch_agents_unavailable' && <p>{t('projects.launch.agentsUnavailable')}</p>}
    <details className="compact-details"><summary>{t('common.technicalDetails')}</summary>
      <p>{t(command.kind === 'launch' ? 'projects.setup.manualContract' : 'projects.setup.templateContract')}</p>
      {command.body && <pre className="technical">{JSON.stringify(command.body, null, 2)}</pre>}
      {command.template && <pre className="technical">{JSON.stringify({ id: command.template.id, name: command.template.name, title: command.template.room_title, agents: command.template.agent_ids, rounds: command.template.max_rounds, tools: command.template.max_tool_calls, seconds: command.template.max_executor_seconds }, null, 2)}</pre>}
      {command.code && <p className="technical">{command.code}</p>}
    </details>
  </div>
}
type SetupHistoryProps = { identity: string; projectId?: number; onResume?: (project: Project) => void }
export default function RoomSetupHistory(props: SetupHistoryProps) {
  return <SetupHistory key={`${props.identity}:${directoryViewGeneration()}:${props.projectId ?? 'all'}`} {...props}/>
}
function SetupHistory({ identity, projectId, onResume }: SetupHistoryProps) {
  const { t } = useI18n(), generation = useRef(directoryViewGeneration()).current, resultId = useId()
  const [query, setQuery] = useState('')
  useSyncExternalStore(subscribeRoomSetup, roomSetupVersion)
  const workspace = readRoomSetup(identity), commands = workspace.commands.filter(command => projectId === undefined || command.target.projectId === projectId), latest = commands[commands.length - 1]
  const drafts = [...workspace.drafts.values()].filter(record => (projectId === undefined || record.project.id === projectId) && (record.value.title || record.value.templateName || record.value.templateId !== null || record.value.rounds !== '2' || record.value.tools !== '8' || record.value.seconds !== '60' || record.value.templateRounds !== '2' || record.value.templateTools !== '8' || record.value.templateSeconds !== '60' || JSON.stringify([...record.value.selected].sort((a,b)=>a-b)) !== JSON.stringify((record.project.agents || []).filter(agent=>agent.is_active).map(agent=>agent.id).sort((a,b)=>a-b))))
  const important = new Set(commands.filter(command => command.phase === 'pending' || command.phase === 'unknown' && !command.acknowledged).map(command => command.id))
  if (latest) important.add(latest.id)
  const older = commands.filter(command => !important.has(command.id))
  const matches = directoryMatcher(query)
  const matching = new Set(older.filter(command => matches([
    String(command.target.projectId), `#${command.target.projectId}`, command.target.projectName,
    command.target.name, command.target.title, command.target.templateId ? `#${command.target.templateId}` : '',
    t(setupActionKey(command)), t(setupPhaseKey(command)),
    command.result ? `#${command.result.id}` : '',
    command.result ? isRoomSetupResult(command.result) ? command.result.title : command.result.name : '',
  ])).map(command => command.id))
  if (!latest && !(onResume && drafts.length)) return null
  return <section className="stack stack--tight" aria-label={t('projects.setup.history')}>
    <p className="field-hint">{t('projects.setup.retained')}</p>
    {onResume && drafts.length > 0 && <details className="compact-details"><summary>{t('projects.setup.drafts')}</summary>{drafts.map(record => <button key={record.project.id} type="button" className="button button--secondary" onClick={() => { if (roomSetupOwnerCurrent(identity, generation)) onResume(record.project) }}>#{record.project.id} · {record.project.name} · {record.value.title || t('projects.newRoom')}</button>)}</details>}
    {commands.filter(command => important.has(command.id)).map(command => <SetupResult key={command.id} identity={identity} command={command} onResume={onResume}/>)}
    {older.length > 0 && <details className="compact-details"><summary>{t('projects.workspace.previous')}</summary>
      <div className="stack stack--tight">
        <DirectorySearch value={query} onChange={value => { if (roomSetupOwnerCurrent(identity, generation)) setQuery(value) }} label={t('directory.search')} hint={t('projects.setup.historyFind.hint')}
          shown={matching.size} total={older.length} loading={false} known controls={resultId} preventSubmit />
        <p className="field-hint">{t('projects.setup.historyFind.scope')}</p>
        {query.trim() && !matching.size && <p role="status">{t('directory.noMatches')}</p>}
        {/* Keep result readers mounted so filtering does not discard a received observation. */}
        <div id={resultId} className="stack stack--tight">{older.map(command => <div key={command.id} hidden={!matching.has(command.id)}><SetupResult identity={identity} command={command} onResume={onResume}/></div>)}</div>
      </div>
    </details>}
  </section>
}
