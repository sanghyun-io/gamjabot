import CommandHistoryArchive from './CommandHistoryArchive'
import RoomDraftFinder from './RoomDraftFinder'
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Link } from 'react-router-dom'
import { conversationsApi, type Conversation } from '../api/conversations'
import { useI18n } from '../i18n'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { roomActionOwnerCurrent } from '../lib/roomActionActions'
import { acknowledgeRoomAction, readRoomActions, roomActionVersion, stopRoomActionWait, subscribeRoomActions, type RoomActionCommand } from '../lib/roomActionWorkspace'

function OriginalAction({ identity, command }: { identity: string; command: RoomActionCommand }) {
  const { t, formatDateTime } = useI18n(), generation = useRef(directoryViewGeneration()).current
  const [reading, setReading] = useState(false), [observed, setObserved] = useState<Conversation[] | null>(null), [failed, setFailed] = useState(false)
  const pending = useRef(false), mounted = useRef(false), valid = () => roomActionOwnerCurrent(identity, generation)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const inspect = async () => {
    if (!valid() || pending.current) return
    pending.current = true; setReading(true); setFailed(false)
    try {
      const values = command.kind === 'rename' ? [await conversationsApi.get(command.source.id)] : await conversationsApi.listByProject(command.body.project_id!, 'all')
      if (valid() && mounted.current) setObserved(values.filter(value => value.user_id === command.source.user_id && value.project_id === (command.body.project_id ?? command.source.project_id)
        && (command.kind === 'rename' ? value.id === command.source.id : value.id !== command.source.id && value.title === command.body.title)))
    } catch { if (valid() && mounted.current) setFailed(true) }
    finally { pending.current = false; if (valid() && mounted.current) setReading(false) }
  }
  return <div className={`notice stack stack--tight ${command.phase === 'succeeded' ? 'notice--success' : command.phase === 'pending' ? 'notice--info' : 'notice--warning'}`}>
    <p className="metadata">{t(command.kind === 'rename' ? 'projects.renameRoom' : 'projects.newRoomFromSetup')} · #{command.source.id} · {command.source.title}</p>
    <p><strong>{command.body.title}</strong>{command.kind === 'clone' && <> · {t('projects.targetProject')} #{command.body.project_id} · {command.targetProjectName}</>}</p>
    <p className="metadata">{t('projects.workspace.started', { time: formatDateTime(command.startedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>
    <p role={command.phase === 'unknown' || command.phase === 'rejected' ? 'alert' : 'status'}>{t(command.phase === 'pending' ? 'projects.workspace.pending' : command.phase === 'unknown' ? 'projects.workspace.unknown' : command.phase === 'rejected' ? 'editor.rejected' : 'projects.setup.succeeded')}</p>
    {command.finishedAt && <p className="metadata">{t('projects.workspace.received', { time: formatDateTime(command.finishedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
    {command.result && <p>{t('projects.setup.originalRoom')} · #{command.result.id} · {command.result.title} <Link className="text-link" to={`/chat/${command.result.id}`} onClick={event => { if (!valid()) event.preventDefault() }}>{t(command.kind === 'clone' ? 'projects.openCreatedRoom' : 'projects.openObservedRoom')}</Link></p>}
    {command.phase === 'pending' && <button type="button" className="button button--secondary button--compact" onClick={() => { if (valid() && window.confirm(t('projects.workspace.stopConfirm')) && valid()) stopRoomActionWait(identity, generation, command.id) }}>{t('projects.workspace.stopWaiting')}</button>}
    {command.phase === 'unknown' && <>
      <button type="button" className="button button--secondary button--compact" disabled={reading} onClick={() => void inspect()}>{t('editor.inspect')}</button>
      {failed && <p role="alert">{t('editor.inspectFailure')}</p>}
      {observed && <><p>{t('projects.roomHistory.observation')}</p>{observed.length ? <ul>{observed.map(value => <li key={value.id}>#{value.id} · {value.title} <Link className="text-link" to={`/chat/${value.id}`} onClick={event => { if (!valid()) event.preventDefault() }}>{t('projects.openObservedRoom')}</Link></li>)}</ul> : <p>{t('editor.noMatch')}</p>}</>}
      {!command.acknowledged && <button type="button" className="button button--secondary button--compact" disabled={reading || failed || !observed} onClick={() => { if (valid() && window.confirm(t('projects.roomHistory.newConfirm')) && valid()) acknowledgeRoomAction(identity, generation, command.id) }}>{t('projects.workspace.allowNew')}</button>}
    </>}
    {command.code === 'room_clone_members_unavailable' && <p>{t('projects.cloneMembersUnavailable')}</p>}
    <details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p>{t('projects.roomHistory.contract')}</p><pre className="technical">{JSON.stringify({ sourceRoomId: command.source.id, requested: command.body, receivedPreview: command.preview }, null, 2)}</pre>{command.code && <p className="technical">{command.code}</p>}</details>
  </div>
}
export default function RoomActionHistory({ identity, roomId, onResume }: { identity: string; roomId?: number; onResume?: (room: Conversation) => void }) {
  const { t,formatDateTime } = useI18n(), generation = useRef(directoryViewGeneration()).current
  useSyncExternalStore(subscribeRoomActions, roomActionVersion)
  const workspace = readRoomActions(identity), commands = workspace.commands.filter(command => roomId === undefined || command.source.id === roomId), latest = commands[commands.length - 1]
  const drafts = [...workspace.drafts.values()].filter(record => (roomId === undefined || record.room.id === roomId) && (record.draft.title !== record.draft.baselineTitle || !!record.draft.cloneTitle || record.draft.targetProject !== record.room.project_id))
  const important = new Set(commands.filter(command => command.phase === 'pending' || command.phase === 'unknown' && !command.acknowledged).map(command => command.id))
  if (latest) important.add(latest.id)
  const older = commands.filter(command => !important.has(command.id)).reverse()
  if (!latest && !(onResume && drafts.length)) return null
  return <section className="stack stack--tight" aria-label={t('projects.roomHistory.title')}>
    <p className="field-hint">{t('projects.roomHistory.retained')}</p>
    {onResume && drafts.length > 0 && <RoomDraftFinder key={`drafts:${identity}:${roomId??'all'}`} records={drafts} canUse={()=>roomActionOwnerCurrent(identity,generation)} onResume={id=>{
      if(!roomActionOwnerCurrent(identity,generation)||roomId!==undefined&&roomId!==id)return
      const latest=readRoomActions(identity).drafts.get(id)
      if(latest)onResume(latest.room)
    }}/>}
    {commands.filter(command => important.has(command.id)).map(command => <OriginalAction key={command.id} identity={identity} command={command}/>)}
    {older.length > 0 && <CommandHistoryArchive key={`history:${identity}:${roomId??'all'}`} title={t('projects.workspace.previous')} items={older.map(command=>({id:command.id,phase:command.phase,search:[command.id,command.kind,t(command.kind==='rename'?'projects.renameRoom':'projects.newRoomFromSetup'),command.source.title,String(command.source.id),`#${command.source.id}`,command.body.title,command.body.project_id===undefined?'':String(command.body.project_id),command.body.project_id===undefined?'':`#${command.body.project_id}`,command.targetProjectName||'',command.result?String(command.result.id):'',command.result?`#${command.result.id}`:'',command.startedAt,formatDateTime(command.startedAt,{dateStyle:'medium',timeStyle:'short'})],content:<OriginalAction command={command} identity={identity}/>}))}/>}
  </section>
}
