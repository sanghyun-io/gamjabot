import { useEffect, useRef, useSyncExternalStore } from 'react'
import { useQuery } from '@tanstack/react-query'
import { projectsApi, type Project } from '../api/projects'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { projectCommandBlocked } from '../lib/projectEditorWorkspace'
import { lifecycleOwnerCurrent, requestProjectLifecycle } from '../lib/projectLifecycleActions'
import { projectLifecycleBlocked, projectLifecycleVersion, readProjectLifecycle, receiveLifecyclePlan, subscribeProjectLifecycle } from '../lib/projectLifecycleWorkspace'
import ProjectLifecycleHistory from './ProjectLifecycleHistory'
import { useDialogFocus } from '../lib/useDialogFocus'

export default function ProjectArchiveDialog({ project, identity, returnFocus, onClose }: { project: Project; identity: string; returnFocus: HTMLElement | null; onClose: () => void }) {
  const { t, formatDateTime } = useI18n(), generation = useRef(directoryViewGeneration()).current
  const dialog = useRef<HTMLDivElement>(null), submitted = useRef<string | null>(null), closed = useRef(false)
  useSyncExternalStore(subscribeProjectLifecycle, projectLifecycleVersion)
  const workspace = readProjectLifecycle(identity), stored = workspace.plans.get(project.id)
  const valid = () => lifecycleOwnerCurrent(identity, generation)
  const close = () => { if (valid()) { closed.current = true; submitted.current = null; onClose() } }
  useEffect(() => {
    closed.current = false
    return () => {
      closed.current = true
      if (!valid() || document.visibilityState !== 'visible' || !document.hasFocus() || !returnFocus?.isConnected
          || !returnFocus.getClientRects().length || returnFocus.matches(':disabled')
          || returnFocus.closest('[hidden], [inert], [aria-hidden="true"]')) return
      const anotherDialog = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).some(node =>
        node.isConnected && node.getClientRects().length > 0 && !node.closest('[hidden], [inert], [aria-hidden="true"]')
        && getComputedStyle(node).visibility !== 'hidden')
      if (!anotherDialog) returnFocus.focus({ preventScroll: true })
    }
  }, [])
  useDialogFocus(dialog, close, { restoreFocus: false })
  const preview = useQuery({
    queryKey: ['project-archive-preview', identity, project.id], retry: false, staleTime: Infinity, refetchOnWindowFocus: false, refetchOnReconnect: false,
    queryFn: async () => {
      if (!valid()) throw new Error('Owner changed')
      const [target, value] = await Promise.all([projectsApi.get(project.id), projectsApi.previewArchive(project.id)])
      if (!valid()) throw new Error('Owner changed')
      if (target.id !== project.id || target.user_id !== useAuthStore.getState().user?.id || typeof target.is_active !== 'boolean' || typeof target.name !== 'string' || value.project_id !== project.id || !/^[a-f0-9]{64}$/.test(value.plan_hash)
        || !value.counts || !['active_rooms', 'pending_triggers', 'running_triggers', 'running_runs', 'waiting_runs', 'waiting_approvals', 'started_actions_uncertain', 'open_actions_cancelled', 'grants_revoked'].every(key => { const count = (value.counts as unknown as Record<string, number>)[key]; return Number.isSafeInteger(count) && count >= 0 }) || !Array.isArray(value.rooms) || !value.rooms.every(room => Number.isSafeInteger(room.id) && room.id > 0 && typeof room.title === 'string' && Number.isSafeInteger(room.active_work_count) && room.active_work_count >= 0)) throw new Error('Archive preview target changed')
      const plan = receiveLifecyclePlan(identity, generation, value)
      if (!plan) throw new Error('Owner changed')
      return { target, planId: plan.id }
    },
  })
  useEffect(() => {
    if (!submitted.current || closed.current || !valid()) return
    const current = workspace.commands.find(command => command.id === submitted.current)
    if (workspace.commands[workspace.commands.length - 1]?.id !== submitted.current) { submitted.current = null; return }
    if (current?.phase === 'succeeded') close()
  }, [workspace.commands])
  const refresh = () => { if (valid() && !preview.isFetching) { submitted.current = null; void preview.refetch({ cancelRefetch: false }) } }
  const canApply = !!stored && !!preview.data && stored.id === preview.data.planId && !stored.used && preview.data.target.is_active && !preview.isFetching && !preview.isError && !projectLifecycleBlocked(identity) && !projectCommandBlocked(identity)
  const apply = () => {
    if (!valid() || !canApply || !stored || !preview.data) return
    const original = requestProjectLifecycle(identity, generation, 'archive', { id: project.id, name: preview.data.target.name }, stored.id)
    if (original) submitted.current = original.id
  }
  const value = stored?.value
  return <div className="modal-backdrop"><div ref={dialog} role="dialog" aria-modal="true" aria-label={t('projects.archiveDialog', { project: project.name })} className="modal-panel stack">
    <h2 className="page-title">{t('projects.archiveDialog', { project: project.name })}</h2>
    <p>{t('projects.archiveHelp')}</p>
    <ProjectLifecycleHistory identity={identity}/>
    <p className="field-hint">{t('projects.lifecycle.previewScope')}</p>
    {preview.isFetching && <p role="status">{t('projects.archivePreviewLoading')}</p>}
    {preview.isError && <p role="alert" className="notice notice--warning">{t('projects.archivePreviewFailure')}</p>}
    {(preview.isFetching || preview.isError) && stored && <p>{t('projects.lifecycle.oldPreview')}</p>}
    {preview.data && !preview.data.target.is_active && <p className="notice notice--warning">{t('projects.workspace.archivedTarget')}</p>}
    {stored && <p className="metadata">{t('projects.workspace.targetReceived', { time: formatDateTime(stored.receivedAt, { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
    {stored?.used && <p className="notice notice--warning">{t('projects.lifecycle.usedPreview')}</p>}
    {value && <div className="stack stack--tight"><div className="project-archive-summary">
      <strong>{t('projects.archiveRoomCount', { count: value.counts.active_rooms })}</strong>
      <span>{t('projects.archiveTriggerCount', { count: value.counts.pending_triggers + value.counts.running_triggers })}</span>
      <span>{t('projects.archiveRunCount', { count: value.counts.running_runs + value.counts.waiting_runs })}</span>
      <span>{t('projects.archiveApprovalCount', { count: value.counts.waiting_approvals })}</span>
      <span>{t('projects.archiveUncertainCount', { count: value.counts.started_actions_uncertain })}</span>
      <span>{t('projects.archiveActionCount', { count: value.counts.open_actions_cancelled })}</span>
      <span>{t('projects.archiveGrantCount', { count: value.counts.grants_revoked })}</span>
    </div>{value.rooms.length > 0 && <div className="stack stack--tight"><h3>{t('projects.archiveRooms')}</h3><ul className="room-list">{value.rooms.map(room => <li className="room-list__item split" key={room.id}><span>{room.title}</span><span>{t('projects.archiveRoomWork', { count: room.active_work_count })}</span></li>)}</ul>{value.rooms_truncated && <p>{t('projects.archiveRoomsTruncated')}</p>}</div>}</div>}
    <div className="page-actions"><button data-dialog-initial type="button" className="button button--secondary" onClick={close}>{t('common.close')}</button>
      <button type="button" className="button button--secondary" disabled={preview.isFetching} onClick={refresh}>{t('projects.archiveRefresh')}</button>
      <button type="button" className="button button--danger" disabled={!canApply} onClick={apply}>{t('projects.archiveApply')}</button>
    </div>
  </div></div>
}
