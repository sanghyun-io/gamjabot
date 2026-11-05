import CommandHistoryArchive from './CommandHistoryArchive'
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { Agent } from '../api/agents'
import { useI18n } from '../i18n'
import { AgentObservationDetails } from './EditorObservationDetails'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { acknowledgeAgentUnknown, agentWorkspaceVersion, readAgentWorkspace, subscribeAgentWorkspace, stopAgentCommandWait, type AgentCommand } from '../lib/agentEditorWorkspace'

function CommandRow({ command, identity, onInspect }: { command: AgentCommand; identity: string; onInspect: (command: AgentCommand) => Promise<Agent[]> }) {
  const { t, formatDateTime } = useI18n(), generation = useRef(directoryViewGeneration()).current
  const [reading, setReading] = useState(false), [observed, setObserved] = useState<Agent[] | null>(null), [failed, setFailed] = useState(false)
  const pending = useRef(false), mounted = useRef(true)
  const valid = () => {
    const owner = useAuthStore.getState()
    return !!owner.token && `${owner.user?.id || 0}:${owner.epoch}` === identity && generation === directoryViewGeneration()
  }
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const inspect = async () => {
    if (pending.current || !valid()) return
    pending.current = true; setReading(true); setFailed(false)
    try { const items = await onInspect(command); if (valid() && mounted.current) setObserved(items) }
    catch { if (valid() && mounted.current) setFailed(true) }
    finally { pending.current = false; if (valid() && mounted.current) setReading(false) }
  }
  const acknowledge = () => {
    if (!valid() || reading || failed || observed === null || command.phase !== 'unknown') return
    if (!window.confirm(t('agents.workspace.newConfirm', { name: command.target.name }))) return
    if (valid()) acknowledgeAgentUnknown(identity, generation, command.id)
  }
  return <div className={`notice stack stack--tight ${command.phase === 'succeeded' ? 'notice--success' : command.phase === 'pending' ? 'notice--info' : 'notice--warning'}`}>
    <p className="metadata">{t(command.kind === 'create' ? 'agents.create' : command.kind === 'update' ? 'agents.edit' : 'common.delete')} · {command.target.id === undefined ? t('agents.workspace.newAgent') : `#${command.target.id}`} · {command.target.name}</p>
    <p className="metadata">{t('agents.workspace.started', { time: formatDateTime(command.startedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}{command.body && <> · #{command.body.connection_id} · {command.body.model_id}</>}</p>
    <p role={command.phase === 'rejected' || command.phase === 'unknown' ? 'alert' : 'status'}>{t(command.phase === 'pending' ? 'agents.workspace.pending' : command.phase === 'succeeded' ? 'agents.workspace.succeeded' : command.phase === 'rejected' ? 'editor.rejected' : 'agents.workspace.unknown')}</p>
    {command.phase === 'pending' && <button type="button" className="button button--secondary button--compact" onClick={() => { if (valid() && window.confirm(t('agents.workspace.stopConfirm')) && valid()) stopAgentCommandWait(identity, generation, command.id) }}>{t('agents.workspace.stopWaiting')}</button>}
    {command.finishedAt && <p className="metadata">{t('agents.workspace.received', { time: formatDateTime(command.finishedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
    {command.phase === 'unknown' && <>
      <button type="button" className="button button--secondary button--compact" disabled={reading} onClick={() => void inspect()}>{t('editor.inspect')}</button>
      {failed && <p role="alert">{t('editor.inspectFailure')}</p>}
      {observed !== null && <><p>{t('agents.workspace.listOnly')}</p>{(reading || failed) && <p role="status" className="metadata">{t('editor.previousObservation')}</p>}{observed.length ? <ul className="stack stack--tight">{observed.map(agent => <li key={agent.id}><p>#{agent.id} · {agent.name} · {agent.model_id || agent.model}</p><AgentObservationDetails agent={agent}/></li>)}</ul> : <p>{t('editor.noMatch')}</p>}</>}
      {!command.acknowledged ? <button type="button" className="button button--secondary button--compact" disabled={reading || failed || observed === null} onClick={acknowledge}>{t('agents.workspace.allowNew')}</button> : <p>{t('agents.workspace.acknowledged')}</p>}
    </>}
  </div>
}
export default function AgentCommandHistory({ identity, onInspect }: { identity: string; onInspect: (command: AgentCommand) => Promise<Agent[]> }) {
  const { t,formatDateTime } = useI18n()
  useSyncExternalStore(subscribeAgentWorkspace, agentWorkspaceVersion)
  const commands = readAgentWorkspace(identity).commands
  const important = commands.filter(command => command.phase === 'pending' || command.phase === 'unknown' && !command.acknowledged)
  const recent = commands[commands.length - 1]
  const shown = new Set(important.map(command => command.id))
  if (recent) shown.add(recent.id)
  const older = commands.filter(command => !shown.has(command.id)).reverse()
  if (!recent) return null
  return <section className="stack stack--tight" aria-label={t('agents.workspace.history')}>
    <p className="field-hint">{t('agents.workspace.retained')}</p>
    {commands.filter(command => shown.has(command.id)).map(command => <CommandRow key={command.id} command={command} identity={identity} onInspect={onInspect}/>)}
    {older.length > 0 && <CommandHistoryArchive key={identity} title={t('agents.workspace.previous')} items={older.map(command=>({id:command.id,phase:command.phase,search:[command.target.name,command.target.id===undefined?'':String(command.target.id),command.target.id===undefined?'':`#${command.target.id}`,command.id,command.kind,t(command.kind==='create'?'agents.create':'agents.edit'),command.startedAt,formatDateTime(command.startedAt,{dateStyle:'medium',timeStyle:'short'})],content:<CommandRow command={command} identity={identity} onInspect={onInspect}/>}))}/>}
  </section>
}
