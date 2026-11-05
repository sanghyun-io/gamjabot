import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { projectsApi, type Project } from '../api/projects'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import type { MessageKey } from '../i18n/locales/en'
import { useI18n } from '../i18n'
import { ProjectObservationDetails } from './EditorObservationDetails'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { projectCommandBlocked } from '../lib/projectEditorWorkspace'
import { lifecycleOwnerCurrent, requestProjectLifecycle } from '../lib/projectLifecycleActions'
import { acknowledgeProjectLifecycle, projectLifecycleVersion, readProjectLifecycle, stopProjectLifecycleWait, subscribeProjectLifecycle, type ProjectLifecycleCommand } from '../lib/projectLifecycleWorkspace'

function lifecycleActionKey(command: ProjectLifecycleCommand): MessageKey { return command.kind === 'archive' ? 'projects.archiveApply' : 'projects.restore' }
function lifecyclePhaseKey(command: ProjectLifecycleCommand): MessageKey { return command.phase === 'pending' ? 'projects.workspace.pending' : command.phase === 'unknown' ? 'projects.workspace.unknown' : command.phase === 'stale' ? 'projects.archiveStale' : command.phase === 'rejected' ? 'editor.rejected' : command.kind === 'archive' ? 'projects.archiveSuccess' : 'projects.lifecycle.restoreReply' }

function LifecycleRow({ identity, command }: { identity: string; command: ProjectLifecycleCommand }) {
  const { t, formatDateTime } = useI18n(), generation = useRef(directoryViewGeneration()).current
  const [reading, setReading] = useState(false), [observed, setObserved] = useState<{ project: Project; at: string } | null>(null), [failed, setFailed] = useState(false)
  const pending = useRef(false), mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const valid = () => lifecycleOwnerCurrent(identity, generation)
  const inspect = async () => {
    if (!valid() || pending.current) return
    pending.current = true; setReading(true); setFailed(false)
    try {
      const project = await projectsApi.get(command.target.id)
      if (!valid()) return
      if (project.id !== command.target.id || project.user_id !== useAuthStore.getState().user?.id || typeof project.is_active !== 'boolean') throw new Error('Project target changed')
      if (mounted.current) setObserved({ project, at: new Date().toISOString() })
    } catch { if (valid() && mounted.current) setFailed(true) }
    finally { pending.current = false; if (valid() && mounted.current) setReading(false) }
  }
  const acknowledge = () => {
    if (!valid() || reading || failed || !observed) return
    if (window.confirm(t('projects.workspace.newConfirm', { name: command.target.name })) && valid()) acknowledgeProjectLifecycle(identity, generation, command.id)
  }
  const retryOriginal = () => {
    if (!valid() || reading || failed || !observed || projectCommandBlocked(identity)) return
    if (!window.confirm(t('projects.lifecycle.retryConfirm', { project: command.target.name }))) return
    requestProjectLifecycle(identity, generation, 'archive', command.target, undefined, command.id)
  }
  const live = readProjectLifecycle(identity)
  const retryBlocked = projectCommandBlocked(identity) || !!live.pending || live.commands.some(value => value.phase === 'unknown' && !value.acknowledged && value.id !== command.id)
  const result = command.result
  const cleanup = result && 'cleanup_unconfirmed' in result ? result.cleanup_unconfirmed : []
  return <div className={`notice stack stack--tight ${command.phase === 'succeeded' && !cleanup.length ? 'notice--success' : command.phase === 'pending' ? 'notice--info' : 'notice--warning'}`}>
    <p className="metadata">{t(lifecycleActionKey(command))} · #{command.target.id} · {command.target.name}</p>
    <p className="metadata">{t('projects.workspace.started', { time: formatDateTime(command.startedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}{command.retryOf && <> · {t('projects.lifecycle.retryLabel')}</>}</p>
    <p role={['unknown', 'stale', 'rejected'].includes(command.phase) ? 'alert' : 'status'}>{t(lifecyclePhaseKey(command))}</p>
    {command.finishedAt && <p className="metadata">{t('projects.workspace.received', { time: formatDateTime(command.finishedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
    {result && 'closed_rooms' in result && <p>{t('projects.restoreSuccess', { count: result.closed_rooms })}</p>}
    {cleanup.length > 0 && <p role="status">{t('projects.archiveCleanupWarning', { count: cleanup.length })}</p>}
    {command.code === 'project_restore_active_children' && command.activeRooms !== undefined && <p>{t('projects.restoreActiveRooms', { count: command.activeRooms })}</p>}
    {command.phase === 'stale' && <p>{t('projects.lifecycle.staleHelp')}</p>}
    {command.phase === 'pending' && <button type="button" className="button button--secondary button--compact" onClick={() => { if (valid() && window.confirm(t('projects.workspace.stopConfirm')) && valid()) stopProjectLifecycleWait(identity, generation, command.id) }}>{t('projects.workspace.stopWaiting')}</button>}
    {command.phase === 'unknown' && <>
      <button type="button" className="button button--secondary button--compact" disabled={reading} onClick={() => void inspect()}>{t('projects.lifecycle.inspect')}</button>
      {failed && <p role="alert">{t('projects.workspace.targetError')}</p>}
      {observed && <><p>{t('projects.lifecycle.observationOnly')}</p>{(reading || failed) && <p role="status" className="metadata">{t('editor.previousObservation')}</p>}<p>#{observed.project.id} · {observed.project.name} · {t(observed.project.is_active ? 'projects.activeProject' : 'projects.archivedProject')}</p><p className="metadata">{t('projects.workspace.targetReceived', { time: formatDateTime(observed.at, { dateStyle: 'medium', timeStyle: 'short' }) })}</p><ProjectObservationDetails project={observed.project}/></>}
      {!command.acknowledged ? <button type="button" className="button button--secondary button--compact" disabled={reading || failed || !observed} onClick={acknowledge}>{t('projects.workspace.allowNew')}</button> : <p>{t('projects.workspace.acknowledged')}</p>}
      {command.kind === 'archive' && <button type="button" className="button button--secondary button--compact" disabled={reading || failed || !observed || retryBlocked} onClick={retryOriginal}>{t('projects.lifecycle.retryOriginal')}</button>}
    </>}
    <details className="compact-details"><summary>{t('common.technicalDetails')}</summary>
      {command.plan && <><p className="technical">{command.plan.plan_hash}</p><p>{t('projects.archiveRoomCount', { count: command.plan.counts.active_rooms })}</p><p>{t('projects.lifecycle.planRetained')}</p><pre className="technical">{JSON.stringify(command.plan.counts, null, 2)}</pre><ul>{command.plan.rooms.map(room => <li key={room.id}>#{room.id} · {room.title}</li>)}</ul>{command.plan.rooms_truncated && <p>{t('projects.archiveRoomsTruncated')}</p>}</>}
      {command.code && <p className="technical">{command.code}</p>}
      <p>{t('projects.lifecycle.scope')}</p>
    </details>
  </div>
}
export default function ProjectLifecycleHistory({ identity }: { identity: string }) {
  return <LifecycleHistory key={`${identity}:${directoryViewGeneration()}`} identity={identity}/>
}
function LifecycleHistory({ identity }: { identity: string }) {
  const { t } = useI18n(), generation = useRef(directoryViewGeneration()).current, resultId = useId()
  const [query, setQuery] = useState('')
  useSyncExternalStore(subscribeProjectLifecycle, projectLifecycleVersion)
  const commands = readProjectLifecycle(identity).commands, latest = commands[commands.length - 1]
  if (!latest) return null
  const important = commands.filter(command => command.phase === 'pending' || command.phase === 'unknown' && !command.acknowledged || command.result && 'cleanup_unconfirmed' in command.result && command.result.cleanup_unconfirmed.length > 0)
  const ids = new Set([...important.map(command => command.id), latest.id])
  const older = commands.filter(command => !ids.has(command.id))
  const matches = directoryMatcher(query)
  const matching = new Set(older.filter(command => matches([
    command.target.name, String(command.target.id), `#${command.target.id}`,
    t(lifecycleActionKey(command)), t(lifecyclePhaseKey(command)),
  ])).map(command => command.id))
  return <section className="stack stack--tight" aria-label={t('projects.lifecycle.history')}>
    <p className="field-hint">{t('projects.lifecycle.retained')}</p>
    {commands.filter(command => ids.has(command.id)).map(command => <LifecycleRow key={command.id} identity={identity} command={command}/>)}
    {older.length > 0 && <details className="compact-details"><summary>{t('projects.workspace.previous')}</summary>
      <div className="stack stack--tight">
        <DirectorySearch value={query} onChange={value => { if (lifecycleOwnerCurrent(identity, generation)) setQuery(value) }} label={t('directory.search')} hint={t('projects.lifecycle.historyFind.hint')}
          shown={matching.size} total={older.length} loading={false} known controls={resultId} preventSubmit />
        <p className="field-hint">{t('projects.lifecycle.historyFind.scope')}</p>
        {query.trim() && !matching.size && <p role="status">{t('directory.noMatches')}</p>}
        {/* Keep received observations mounted while narrowing the local history. */}
        <div id={resultId} className="stack stack--tight">{older.map(command => <div key={command.id} hidden={!matching.has(command.id)}><LifecycleRow identity={identity} command={command}/></div>)}</div>
      </div>
    </details>}
  </section>
}
