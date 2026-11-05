import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Settings2 } from 'lucide-react'
import { conversationsApi, type Conversation } from '../api/conversations'
import { projectsApi, type Project } from '../api/projects'
import { schedulesApi } from '../api/schedules'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { useDialogFocus } from '../lib/useDialogFocus'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { useViewNavigationIntent } from '../lib/useViewNavigationIntent'
import { requestRoomAction, roomActionOwnerCurrent } from '../lib/roomActionActions'
import { defaultRoomAction, initializeRoomAction, patchRoomAction, readRoomActions, returnedRoomClone, roomActionBlocked, roomActionFingerprint, roomActionVersion, roomCloneSignature, subscribeRoomActions, type RoomActionDraft, type RoomClonePreview } from '../lib/roomActionWorkspace'
import RoomActionHistory from './RoomActionHistory'

export default function RoomActions({ room, projects, identity, authEpoch, forcedOpen = false }: { room: Conversation; projects: Project[]; identity: string; authEpoch: number; forcedOpen?: boolean }) {
  const { t } = useI18n(), generation = useRef(directoryViewGeneration()).current, navigate = useNavigate()
  const ime = useImeFormGuard(`${identity}:room-actions:${room.id}`), navigation = useViewNavigationIntent(), cloneForm = useRef<HTMLFormElement>(null), submitted = useRef<string | null>(null)
  useSyncExternalStore(subscribeRoomActions, roomActionVersion)
  const workspace = readRoomActions(identity), [initial] = useState(() => defaultRoomAction(room)), draft = workspace.drafts.get(room.id)?.draft || initial
  const open = forcedOpen || draft.open, busy = !!workspace.pending, blocked = roomActionBlocked(identity)
  const valid = () => roomActionOwnerCurrent(identity, generation) && authEpoch === useAuthStore.getState().epoch
  useLayoutEffect(() => { if (valid()) initializeRoomAction(identity, generation, room) }, [])
  const [error, setError] = useState(false), [acknowledged, setAcknowledged] = useState(false)
  const change = (patch: Partial<RoomActionDraft>) => { if (valid() && patchRoomAction(identity, generation, room.id, patch)) { navigation.cancel(); setError(false) } }
  const queryOptions = { enabled: open, retry: false, staleTime: 0, refetchOnWindowFocus: false, refetchOnReconnect: false } as const
  const source = useQuery({ ...queryOptions, queryKey: ['room-action-source', identity, room.id], queryFn: async () => {
    if (!valid()) throw new Error('Owner changed'); const value = await conversationsApi.get(room.id)
    if (!valid() || value.id !== room.id || value.user_id !== room.user_id || value.project_id !== room.project_id) throw new Error('Room target changed'); return value
  } })
  const sourceProject = useQuery({ ...queryOptions, queryKey: ['room-action-project', identity, room.project_id], queryFn: async () => {
    if (!valid()) throw new Error('Owner changed'); const value = await projectsApi.get(room.project_id)
    if (!valid() || value.id !== room.project_id || value.user_id !== room.user_id) throw new Error('Project target changed'); return value
  } })
  const destinations = useQuery({ ...queryOptions, queryKey: ['room-action-destinations', identity], queryFn: async () => { if (!valid()) throw new Error('Owner changed'); const value = await projectsApi.list('active'); if (!valid()) throw new Error('Owner changed'); return value } })
  const members = useQuery({ ...queryOptions, queryKey: ['room-action-members', identity, room.id], queryFn: async () => {
    if (!valid()) throw new Error('Owner changed'); const value = await conversationsApi.listMembers(room.id)
    if (!valid() || value.some(item => item.room_id !== room.id || !Number.isSafeInteger(item.agent_id) || item.agent_id <= 0)) throw new Error('Room members changed'); return value
  } })
  const schedule = useQuery({ ...queryOptions, queryKey: ['room-action-schedule', identity, room.id], queryFn: async () => { if (!valid()) throw new Error('Owner changed'); const value = await schedulesApi.get(room.id); if (!valid()) throw new Error('Owner changed'); return value } })
  const activeProjects = (destinations.data || projects).filter(project => project.is_active && project.user_id === room.user_id)
  const destination = activeProjects.find(project => project.id === draft.targetProject)
  const targetUnavailable = source.isFetching || source.isError || !source.data || sourceProject.isFetching || sourceProject.isError || !sourceProject.data?.is_active
  const basisChanged = !!source.data && source.data.title !== draft.baselineTitle
  useEffect(() => { setAcknowledged(false) }, [source.data?.title, source.dataUpdatedAt, draft.title])
  const preview: RoomClonePreview | undefined = members.data && schedule.data ? { members: members.data, schedule: schedule.data, membersReceivedAt: members.dataUpdatedAt, scheduleReceivedAt: schedule.dataUpdatedAt } : undefined
  const previewBlocked = members.isFetching || schedule.isFetching || !members.isSuccess || !schedule.isSuccess || destinations.isFetching || !destinations.isSuccess || !destination
  const previousClone = preview && returnedRoomClone(identity, roomCloneSignature(room.id, draft, preview))
  useEffect(() => {
    const command = workspace.commands.find(value => value.id === submitted.current)
    if (!command || !valid()) return
    if (!open || command.waitStopped || workspace.commands[workspace.commands.length - 1]?.id !== command.id) { navigation.cancel(); submitted.current = null; return }
    if (command.phase === 'succeeded' && command.result) {
      submitted.current = null
      const current = readRoomActions(identity).drafts.get(room.id)?.draft
      if (current && navigation.current(command.id) && roomActionFingerprint(current) === command.fingerprint) navigate(`/chat/${command.result.id}`)
      navigation.cancel()
    } else if (command.phase !== 'pending') { navigation.cancel(); submitted.current = null }
  }, [workspace.commands, open])
  const rename = (event: React.FormEvent) => {
    event.preventDefault(); if (ime.isSubmitBlocked(event) || !valid() || blocked || targetUnavailable || basisChanged && !acknowledged) return
    const current = readRoomActions(identity).drafts.get(room.id)?.draft
    if (!current?.title.trim() || current.title.trim().length > 200) { setError(true); return }
    navigation.cancel(); requestRoomAction(identity, generation, source.data!, 'rename')
  }
  const clone = (event: React.FormEvent) => {
    event.preventDefault(); if (ime.isSubmitBlocked(event) || !valid() || blocked || targetUnavailable || previewBlocked || !preview || previousClone) return
    const current = readRoomActions(identity).drafts.get(room.id)?.draft
    if (!current?.cloneTitle.trim() || current.cloneTitle.trim().length > 200) { setError(true); return }
    const command = requestRoomAction(identity, generation, source.data!, 'clone', preview, destination?.name)
    if (command) { submitted.current = command.id; navigation.begin(command.id, cloneForm.current) }
  }
  const refresh = () => {
    if (!valid() || busy) return
    navigation.cancel()
    void Promise.allSettled([source.refetch({ cancelRefetch: false }), sourceProject.refetch({ cancelRefetch: false }), destinations.refetch({ cancelRefetch: false }), members.refetch({ cancelRefetch: false }), schedule.refetch({ cancelRefetch: false })])
  }
  const clear = () => {
    if (!valid() || busy || !window.confirm(t('projects.roomHistory.clearConfirm'))) return
    change({ title: source.data?.title ?? draft.baselineTitle, baselineTitle: source.data?.title ?? draft.baselineTitle, cloneTitle: '', targetProject: room.project_id, intent: crypto.randomUUID() })
  }
  return <div className="stack stack--tight"><details open={open} className="room-actions compact-details" onToggle={event => {
    const next = event.currentTarget.open
    if (!forcedOpen && next !== readRoomActions(identity).drafts.get(room.id)?.draft.open) change({ open: next })
    if (!next) { navigation.cancel(); submitted.current = null }
  }}><summary><Settings2 size={15} aria-hidden="true"/>{t('projects.roomActions')}</summary>{open && <div className="room-actions__body stack stack--tight">
    <p className="field-hint">{t('projects.roomHistory.retained')}</p>
    <div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={busy || source.isFetching || sourceProject.isFetching || destinations.isFetching || members.isFetching || schedule.isFetching} onClick={refresh}>{t('projects.roomHistory.read')}</button><button type="button" className="button button--secondary button--compact" disabled={busy} onClick={clear}>{t('projects.roomHistory.clear')}</button></div>
    {targetUnavailable && <p role="status" className="notice notice--warning">{t('projects.roomHistory.unavailable')}</p>}
    {blocked && <p role="status">{t('projects.workspace.blocked')}</p>}
    <form {...ime.handlers} noValidate className="stack stack--tight" onSubmit={rename}><strong>{t('projects.renameRoom')}</strong>
      <label className="field">{t('projects.roomTitle')}<input value={draft.title} disabled={busy} maxLength={200} onChange={event => change({ title: event.target.value })}/></label>
      {basisChanged && <div className="notice notice--warning stack stack--tight"><p>{t('projects.roomHistory.changed')}</p><p>#{room.id} · {source.data?.title}</p><label className="check-row"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)}/>{t('projects.roomHistory.acknowledge')}</label></div>}
      <button className="button button--secondary button--compact" disabled={blocked || targetUnavailable || basisChanged && !acknowledged}>{t('projects.renameSave')}</button>
    </form>
    <form ref={cloneForm} {...ime.handlers} noValidate className="stack stack--tight room-actions__clone" onSubmit={clone}><strong>{t('projects.newRoomFromSetup')}</strong>
      <p className="metadata">{t('projects.cloneSource', { room: source.data?.title || room.title })} · #{room.id}</p>
      <label className="field">{t('projects.newRoomName')}<input value={draft.cloneTitle} disabled={busy} maxLength={200} onChange={event => change({ cloneTitle: event.target.value })}/></label>
      <label className="field">{t('projects.targetProject')}<select value={draft.targetProject} disabled={busy} onChange={event => change({ targetProject: Number(event.target.value) })}>{!destination && <option value={draft.targetProject}>#{draft.targetProject} · {t('projects.roomHistory.destinationMissing')}</option>}{activeProjects.map(project => <option value={project.id} key={project.id}>{project.name}</option>)}</select></label>
      {previewBlocked && <p role="status">{t(members.isFetching || schedule.isFetching || destinations.isFetching ? 'projects.cloneLoading' : 'projects.clonePreviewFailure')}</p>}
      {preview && <div className="room-actions__preview"><span>{preview.members.length ? t('projects.cloneTeamCount', { count: preview.members.length }) : t('projects.cloneNoTeam')}</span><span>{t('projects.cloneSettings', { rounds: preview.schedule.max_rounds, tools: preview.schedule.max_tool_calls, seconds: preview.schedule.max_executor_seconds })}</span></div>}
      <p className="field-hint">{t('projects.roomHistory.preview')}</p>
      {previousClone && <p>{t('projects.setup.alreadyCreated')}</p>}
      <div className="page-actions"><button className="button button--primary button--compact" disabled={blocked || targetUnavailable || previewBlocked || !!previousClone}>{t('projects.createFromSetup')}</button><button type="button" className="button button--secondary button--compact" disabled={busy} onClick={() => { if (valid() && window.confirm(t('projects.roomHistory.anotherConfirm'))) change({ intent: crypto.randomUUID() }) }}>{t('projects.setup.another')}</button></div>
    </form>
    {error && <p role="alert" className="metadata metadata--danger">{t('projects.roomTitleValidation')}</p>}
  </div>}</details><RoomActionHistory identity={identity} roomId={room.id}/></div>
}
export function RoomActionResumeDialog({ room, identity, authEpoch, onClose }: { room: Conversation; identity: string; authEpoch: number; onClose: () => void }) {
  const { t } = useI18n(), generation = useRef(directoryViewGeneration()).current, dialog = useRef<HTMLDivElement>(null)
  const close = () => { if (roomActionOwnerCurrent(identity, generation)) onClose() }
  useDialogFocus(dialog, close)
  return <div className="modal-backdrop"><div ref={dialog} role="dialog" aria-modal="true" aria-labelledby="room-action-resume" className="modal-panel stack"><h2 id="room-action-resume" className="page-title">{t('projects.roomHistory.drafts')} · #{room.id}</h2><button data-dialog-initial type="button" className="button button--secondary" onClick={close}>{t('common.close')}</button><RoomActions room={room} projects={[]} identity={identity} authEpoch={authEpoch} forcedOpen/></div></div>
}
