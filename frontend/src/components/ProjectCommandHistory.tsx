import CommandHistoryArchive from './CommandHistoryArchive'
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { Project } from '../api/projects'
import { useI18n } from '../i18n'
import { ProjectObservationDetails } from './EditorObservationDetails'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { acknowledgeProjectUnknown, projectWorkspaceVersion, readProjectWorkspace, subscribeProjectWorkspace, stopProjectCommandWait, type ProjectCommand } from '../lib/projectEditorWorkspace'

function CommandRow({ command, identity, onInspect }: { command: ProjectCommand; identity: string; onInspect: (command: ProjectCommand) => Promise<Project[]> }) {
  const { t, formatDateTime } = useI18n(), generation = useRef(directoryViewGeneration()).current
  const [reading, setReading] = useState(false), [observed, setObserved] = useState<Project[] | null>(null), [failed, setFailed] = useState(false)
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
    if (!window.confirm(t('projects.workspace.newConfirm', { name: command.target.name }))) return
    if (valid()) acknowledgeProjectUnknown(identity, generation, command.id)
  }
  return <div className={`notice stack stack--tight ${command.phase === 'succeeded' ? 'notice--success' : command.phase === 'pending' ? 'notice--info' : 'notice--warning'}`}>
    <p className="metadata">{t(command.kind === 'create' ? 'projects.create' : 'projects.edit')} · {command.target.id === undefined ? t('projects.workspace.newProject') : `#${command.target.id}`} · {command.target.name}</p>
    <p className="metadata">{t('projects.workspace.started', { time: formatDateTime(command.startedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>
    <p role={command.phase === 'rejected' || command.phase === 'unknown' ? 'alert' : 'status'}>{t(command.phase === 'pending' ? 'projects.workspace.pending' : command.phase === 'succeeded' ? 'projects.workspace.succeeded' : command.phase === 'rejected' ? 'editor.rejected' : 'projects.workspace.unknown')}</p>
    {command.phase === 'pending' && <button type="button" className="button button--secondary button--compact" onClick={() => { if (valid() && window.confirm(t('projects.workspace.stopConfirm')) && valid()) stopProjectCommandWait(identity, generation, command.id) }}>{t('projects.workspace.stopWaiting')}</button>}
    {command.finishedAt && <p className="metadata">{t('projects.workspace.received', { time: formatDateTime(command.finishedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
    {command.phase === 'unknown' && <>
      <button type="button" className="button button--secondary button--compact" disabled={reading} onClick={() => void inspect()}>{t('editor.inspect')}</button>
      {failed && <p role="alert">{t('editor.inspectFailure')}</p>}
      {observed !== null && <><p>{t('projects.workspace.listOnly')}</p>{(reading || failed) && <p role="status" className="metadata">{t('editor.previousObservation')}</p>}{observed.length ? <ul className="stack stack--tight">{observed.map(project => <li key={project.id}><p>#{project.id} · {project.name} · {t(project.is_active ? 'projects.activeProject' : 'projects.archivedProject')}</p><ProjectObservationDetails project={project}/></li>)}</ul> : <p>{t('editor.noMatch')}</p>}</>}
      {!command.acknowledged ? <button type="button" className="button button--secondary button--compact" disabled={reading || failed || observed === null} onClick={acknowledge}>{t('projects.workspace.allowNew')}</button> : <p>{t('projects.workspace.acknowledged')}</p>}
    </>}
  </div>
}
export default function ProjectCommandHistory({ identity, onInspect }: { identity: string; onInspect: (command: ProjectCommand) => Promise<Project[]> }) {
  const { t,formatDateTime } = useI18n()
  useSyncExternalStore(subscribeProjectWorkspace, projectWorkspaceVersion)
  const commands = readProjectWorkspace(identity).commands
  const important = commands.filter(command => command.phase === 'pending' || command.phase === 'unknown' && !command.acknowledged)
  const recent = commands[commands.length - 1]
  const shown = new Set(important.map(command => command.id))
  if (recent) shown.add(recent.id)
  const older = commands.filter(command => !shown.has(command.id)).reverse()
  if (!recent) return null
  return <section className="stack stack--tight" aria-label={t('projects.workspace.history')}>
    <p className="field-hint">{t('projects.workspace.retained')}</p>
    {commands.filter(command => shown.has(command.id)).map(command => <CommandRow key={command.id} command={command} identity={identity} onInspect={onInspect}/>)}
    {older.length > 0 && <CommandHistoryArchive key={identity} title={t('projects.workspace.previous')} items={older.map(command=>({id:command.id,phase:command.phase,search:[command.target.name,command.target.id===undefined?'':String(command.target.id),command.target.id===undefined?'':`#${command.target.id}`,command.id,command.kind,t(command.kind==='create'?'projects.create':'projects.edit'),command.startedAt,formatDateTime(command.startedAt,{dateStyle:'medium',timeStyle:'short'})],content:<CommandRow command={command} identity={identity} onInspect={onInspect}/>}))}/>}
  </section>
}
