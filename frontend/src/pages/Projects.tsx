import DirectoryDescription from '../components/DirectoryDescription'
import ProjectDraftComparison from '../components/ProjectDraftComparison'
import ProjectAgentAssignment from '../components/ProjectAgentAssignment'
import { useDirectoryValue, useDirectoryPosition } from '../lib/useDirectoryView'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useDialogFocus } from '../lib/useDialogFocus'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { projectsApi } from '../api/projects'
import type { Project, ProjectStatus } from '../api/projects'
import { agentsApi } from '../api/agents'
import { conversationsApi } from '../api/conversations'
import { useAuthStore } from '../stores/authStore'
import { Plus, Edit2, Archive, ArchiveRestore, Folder, MessageSquare } from 'lucide-react'
import { AppShell, PageHeader } from '../components/ui'
import NewRoomModal from '../components/RoomSetupDialog'
import RoomSetupHistory from '../components/RoomSetupHistory'
import RoomActions, { RoomActionResumeDialog } from '../components/RoomActions'
import RoomActionHistory from '../components/RoomActionHistory'
import ListLoadNotice from '../components/ListLoadNotice'
import DirectorySearch, { directoryMatcher, type DirectorySearchHandle } from '../components/DirectorySearch'
import EditorSavedNotice, { focusSavedEditorCard, type SavedEditorResult } from '../components/EditorSavedNotice'
import EditorDraftNotice from '../components/EditorDraftNotice'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import ProjectCommandHistory from '../components/ProjectCommandHistory'
import ProjectArchiveDialog from '../components/ProjectArchiveDialog'
import ProjectLifecycleHistory from '../components/ProjectLifecycleHistory'
import { lifecycleOwnerCurrent, requestProjectLifecycle } from '../lib/projectLifecycleActions'
import { readProjectLifecycle, projectLifecycleBlocked, projectLifecycleVersion, subscribeProjectLifecycle } from '../lib/projectLifecycleWorkspace'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { projectCommandBlocked, projectDraftKey, projectWorkspaceVersion, beginProjectCommand, discardProjectDraft, dismissProjectSaved, keepProjectDraft, readProjectWorkspace, settleProjectCommand, subscribeProjectWorkspace, type ProjectCommand, type ProjectDraft } from '../lib/projectEditorWorkspace'
import WorkspaceTransfer from '../components/WorkspaceTransfer'
import { readRoomSetup, roomSetupVersion, subscribeRoomSetup } from '../lib/roomSetupWorkspace'
import { readRoomActions, roomActionVersion, subscribeRoomActions } from '../lib/roomActionWorkspace'
import { useI18n } from '../i18n'
import { editorTextLength, focusEditorField } from '../lib/editorValidation'
import { reviewTimestamp } from '../lib/reviewTime'

export default function Projects() {
  const identity = useAuthStore((state) => `${state.user?.id || 0}:${state.epoch}`)
  return <ProjectDirectory key={`${identity}:${directoryViewGeneration()}`} identity={identity}/>
}
function ProjectDirectory({ identity }: { identity: string }) {
  const [showCreateModal, setShowCreateModal] = useState(false)
  const generation = useRef(directoryViewGeneration()).current
  const submittedHere = useRef<string | null>(null)
  useSyncExternalStore(subscribeProjectWorkspace, projectWorkspaceVersion)
  useSyncExternalStore(subscribeProjectLifecycle, projectLifecycleVersion)
  useSyncExternalStore(subscribeRoomSetup, roomSetupVersion)
  useSyncExternalStore(subscribeRoomActions, roomActionVersion)
  const workspace = readProjectWorkspace(identity), draftKey = projectDraftKey
  const saving = !!workspace.pending, blocked = projectCommandBlocked(identity) || projectLifecycleBlocked(identity)
  const needsWorkAttention = blocked || readRoomSetup(identity).commands.some(command => command.phase === 'pending' || command.phase === 'unknown' && !command.acknowledged)
    || readRoomActions(identity).commands.some(command => command.phase === 'pending' || command.phase === 'unknown' && !command.acknowledged)
    || readProjectLifecycle(identity).commands.some(command => command.result && 'cleanup_unconfirmed' in command.result && command.result.cleanup_unconfirmed.length > 0)
  const latest = workspace.commands[workspace.commands.length - 1]
  const savedResult: SavedEditorResult | null = latest?.phase === 'succeeded' && latest.result && !latest.dismissed
    ? { id: latest.result.id, name: latest.result.name, kind: latest.kind === 'create' ? 'created' : 'updated', snapshotVersion: latest.snapshotVersion, identity } : null
  const [editingProject, setEditingProject] = useState<Project | null>(null)
  const [showAgentModal, setShowAgentModal] = useState<Project | null>(null)
  const [resumingRoom, setResumingRoom] = useState<import('../api/conversations').Conversation | null>(null)
  const [newRoomProject, setNewRoomProject] = useState<Project | null>(null)
  const [archiveProject, setArchiveProject] = useState<Project | null>(null)
  const [archiveReturnFocus, setArchiveReturnFocus] = useState<HTMLElement | null>(null)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const { t } = useI18n()
  const ownerId = useAuthStore((state) => state.user?.id)
  const authEpoch = useAuthStore((state) => state.epoch)
  const [projectStatus, setProjectStatus] = useDirectoryValue<Exclude<ProjectStatus, 'all'>>(identity, 'project.status', 'active')
  const ownerStillCurrent = () => { const owner = useAuthStore.getState(); return !!owner.token && generation === directoryViewGeneration() && `${owner.user?.id || 0}:${owner.epoch}` === identity }
  const [directoryQuery, setDirectoryQuery] = useDirectoryValue<string>(identity, 'project.search', '')
  const directoryRoot = useRef<HTMLElement>(null)
  const directoryFinder = useRef<DirectorySearchHandle>(null)
  const [pendingSavedFocus, setPendingSavedFocus] = useState<{ identity: string; id: number } | null>(null)
  const changeDirectoryQuery = (value: string) => {
    if (!ownerStillCurrent()) return
    setPendingSavedFocus(null); setDirectoryQuery(value)
  }
  const clearDirectorySearch = () => {
    if (!(ownerStillCurrent()) || !directoryRoot.current?.isConnected) return
    directoryFinder.current?.focus()
    changeDirectoryQuery('')
  }



  const projectsQuery = useQuery({
    retry: false,
    queryKey: ['projects', identity, projectStatus],
    queryFn: async () => { if (!ownerStillCurrent()) throw new Error('Owner changed'); const value = await projectsApi.list(projectStatus); if (!ownerStillCurrent()) throw new Error('Owner changed'); return value },
  })

  const agentsQuery = useQuery({
    retry: false,
    queryKey: ['agents', identity],
    queryFn: async () => { if (!ownerStillCurrent()) throw new Error('Owner changed'); const value = await agentsApi.list(); if (!ownerStillCurrent()) throw new Error('Owner changed'); return value },
  })

  const projects = projectsQuery.data || []
  const visibleItems = useMemo(() => { const matches = directoryMatcher(directoryQuery)
    return projects.filter(item => matches([item.name, item.description, String(item.id), `#${item.id}`], item.id))
  }, [projects, directoryQuery])
  const visibleIds = useMemo(() => new Set(visibleItems.map(item => item.id)), [visibleItems])
  useDirectoryPosition(identity, 'project', JSON.stringify([directoryQuery, projectStatus]), directoryRoot, projectsQuery.data !== undefined)
  const allAgents = agentsQuery.data || []
  const agentListState = { failed: agentsQuery.isError, fetching: agentsQuery.isFetching, pending: agentsQuery.isPending,
    hasSnapshot: agentsQuery.data !== undefined, retry: () => { if (ownerStillCurrent()) void agentsQuery.refetch() } }

  const basisId = editingProject?.id
  const projectBasis = useQuery({
    queryKey: ['project-editor-basis', identity, basisId], enabled: basisId !== undefined, retry: false,
    refetchOnWindowFocus: false, refetchOnReconnect: false,
    queryFn: async () => {
      if (!ownerStillCurrent() || basisId === undefined) throw new Error('Owner changed')
      const value = await projectsApi.get(basisId)
      if (!ownerStillCurrent() || value.id !== basisId || value.user_id !== ownerId) throw new Error('Project response target changed')
      return value
    },
  })
  const inspectCommand = async (command: ProjectCommand) => {
    if (!ownerStillCurrent()) throw new Error('Owner changed')
    const items = await queryClient.fetchQuery({ queryKey: ['projects', identity, 'all'], staleTime: 0, retry: false, queryFn: async () => {
      if (!ownerStillCurrent()) throw new Error('Owner changed')
      const value = await projectsApi.list('all')
      if (!ownerStillCurrent()) throw new Error('Owner changed')
      return value
    } })
    if (!ownerStillCurrent()) throw new Error('Owner changed')
    return items.filter(item => command.target.id !== undefined ? item.id === command.target.id : item.name === command.target.name)
  }
  const dismissSaved = () => { if (ownerStillCurrent() && latest) dismissProjectSaved(identity, generation, latest.id) }
  const sendCommand = async (original: ProjectCommand) => {
    if (!ownerStillCurrent()) return
    try {
      const result = original.kind === 'create' ? await projectsApi.create(original.body!) : await projectsApi.update(original.target.id!, original.body!)
      if (!ownerStillCurrent()) return
      if (result.user_id !== ownerId || !Number.isSafeInteger(result.id) || result.id <= 0 || original.kind === 'update' && result.id !== original.target.id) throw new Error('Project response target mismatch')
      settleProjectCommand(identity, generation, original, 'succeeded', result)
      void queryClient.invalidateQueries({ queryKey: ['projects', identity] })
      if (original.target.id !== undefined) void queryClient.invalidateQueries({ queryKey: ['project-editor-basis', identity, original.target.id] })
    } catch (error) {
      if (ownerStillCurrent()) settleProjectCommand(identity, generation, original, editorSaveFailure(error) === 'rejected' ? 'rejected' : 'unknown')
    }
  }
  useEffect(() => {
    const command = workspace.commands.find(value => value.id === submittedHere.current)
    if (!command || command.phase === 'pending' || command.phase === 'unknown' && command.waitStopped && !command.finishedAt) return
    submittedHere.current = null
    if (command.phase === 'succeeded' && (showCreateModal || editingProject) && draftKey(editingProject?.id) === draftKey(command.target.id)) { setShowCreateModal(false); setEditingProject(null) }
  }, [workspace.commands, showCreateModal, editingProject])
  const savedReady = Boolean(savedResult && savedResult.identity === identity && projectsQuery.isSuccess && !projectsQuery.isFetching && projectsQuery.dataUpdatedAt !== savedResult.snapshotVersion && projectsQuery.data?.some(item => item.id === savedResult.id))
  useEffect(() => {
    if (!pendingSavedFocus) return
    if (pendingSavedFocus.identity !== identity || savedResult?.id !== pendingSavedFocus.id) { setPendingSavedFocus(null); return }
    if (directoryQuery || !savedReady || !visibleIds.has(pendingSavedFocus.id) || !ownerStillCurrent()) return
    focusSavedEditorCard('project', pendingSavedFocus.id); setPendingSavedFocus(null)
  }, [pendingSavedFocus, identity, savedResult, directoryQuery, savedReady, visibleIds])

  const restoreProject = (project: Project) => {
    if (!ownerStillCurrent() || blocked) return
    requestProjectLifecycle(identity, generation, 'restore', { id: project.id, name: project.name })
  }

  return (
    <AppShell>
      <PageHeader title={t('projects.title')} subtitle={t('projects.subtitle')} backTo="/" actions={projectStatus === 'active' ?
            <button onClick={() => { if (ownerStillCurrent()) setShowCreateModal(true) }} className="button button--primary">
              <Plus size={20} />
              <span>{t(workspace.drafts.has(draftKey()) ? 'editor.continueDraft' : 'projects.new')}</span>
            </button> : undefined} />

      <main ref={directoryRoot} className="page-frame page-frame--wide page-content stack project-directory-page">
        <EditorSavedNotice result={savedResult?.identity === identity ? savedResult : null} fetching={projectsQuery.isFetching}
          ready={savedReady}
          onShow={() => { if (savedResult && ownerStillCurrent()) { setProjectStatus('active'); setDirectoryQuery(''); setPendingSavedFocus({ identity, id: savedResult.id }) } }}
          onReload={() => { if (!ownerStillCurrent()) return; if (projectStatus !== 'active') { setProjectStatus('active'); void queryClient.invalidateQueries({ queryKey: ['projects', identity, 'active'] }) } else void projectsQuery.refetch({ cancelRefetch: false }) }} onDismiss={dismissSaved} />
        {needsWorkAttention && <div role="status" className="notice notice--warning directory-work-attention"><span>{t('projects.workNeedsAttention')}</span><button type="button" className="button button--secondary button--compact" onClick={() => { if (!ownerStillCurrent()) return; const target = document.getElementById('project-work-records'); target?.focus({ preventScroll: true }); target?.scrollIntoView({ block: 'start' }) }}>{t('projects.openWorkRecords')}</button></div>}
        <div className="directory-controls">
        <DirectorySearch compact exactId ref={directoryFinder} value={directoryQuery} onChange={changeDirectoryQuery} hint={t('directory.projectHint')}
          shown={visibleItems.length} total={projects.length} loading={projectsQuery.isPending} known={projectsQuery.data !== undefined}
          controls={projects.length ? 'project-directory-results' : undefined} />
        <div className="project-status-filter">
          <div className="segmented-control" role="group" aria-label={t('projects.projectStatus')}>
            <button type="button" aria-pressed={projectStatus === 'active'} onClick={() => { setProjectStatus('active') }}>{t('projects.activeProjects')}</button>
            <button type="button" aria-pressed={projectStatus === 'archived'} onClick={() => { setProjectStatus('archived') }}>{t('projects.archivedProjects')}</button>
          </div>
        </div>
        </div>
        <ListLoadNotice failed={projectsQuery.isError} fetching={projectsQuery.isFetching} hasSnapshot={projectsQuery.data !== undefined}
          message={t('projects.listFailure')} onRetry={() => { if (ownerStillCurrent()) void projectsQuery.refetch() }} />
        {projectsQuery.isPending ? (
          <div className="surface">
            <p role="status" className="section-copy">{t('common.loading')}</p>
          </div>
        ) : projects.length === 0 && projectsQuery.isError ? null : projects.length === 0 ? (
          <div className="surface stack">
            <Folder color="var(--text-muted)" size={40} />
            <p className="section-copy">{t(projectStatus === 'archived' ? 'projects.noArchived' : 'projects.noActive')}</p>
            {projectStatus === 'active' && <button
              onClick={() => { if (ownerStillCurrent()) setShowCreateModal(true) }}
              className="button button--primary"
            >
              {t('projects.first')}
            </button>}
          </div>
        ) : (
          <div className="project-list" id="project-directory-results">
            {visibleItems.length === 0 && <div className="surface stack" style={{ gridColumn: '1 / -1' }}>
              <p className="section-copy">{t('directory.noMatches')}</p>
              <button type="button" className="button button--secondary" onClick={clearDirectorySearch}>{t('directory.clear')}</button>
            </div>}
            {projects.map((project) => (
              <div
                key={project.id} id={`project-card-${project.id}`} tabIndex={-1} hidden={!visibleIds.has(project.id)} style={{ display: visibleIds.has(project.id) ? undefined : 'none' }}
                className="surface stack project-card"
              >
                <div className="split project-card__header">
                  <div>
                    <h3 className="section-title">
                      {project.name}<span className="metadata"> · #{project.id}</span>{savedReady && savedResult?.id === project.id && <span className="metadata"> · {t('editor.justSaved')}</span>}
                    </h3>
                    {project.description && <DirectoryDescription text={project.description} fallback="" />}
                  </div>
                  {project.is_active && <div className="page-actions">
                    <button
                      onClick={() => { if (ownerStillCurrent()) setEditingProject(project) }}
                      className="icon-button"
                      aria-label={workspace.drafts.has(draftKey(project.id)) ? `${t('editor.continueDraft')} · ${project.name}` : t('projects.editNamed', { project: project.name })}
                    >
                      <Edit2 size={18} />
                    </button>
                    <button
                      disabled={projectCommandBlocked(identity)} onClick={(event) => { if (!ownerStillCurrent() || projectCommandBlocked(identity)) return; setArchiveReturnFocus(event.currentTarget); setArchiveProject(project) }}
                      className="icon-button danger-link"
                      aria-label={t('projects.archiveNamed', { project: project.name })}
                    >
                      <Archive size={18} />
                    </button>
                  </div>}
                </div>

                <ProjectActivity project={project} />
                <div className="project-card__tools">
                <div className="stack stack--tight">
                  <p className="metadata">
                    {t('projects.assignedCount', { count: project.agents?.length || 0 })}
                  </p>
                  {project.agents && project.agents.length > 0 && (
                    <div className="page-actions">
                      {project.agents.map((agent) => (
                        <span
                          key={agent.id}
                          className="status-text status-text--busy"
                        >
                          {agent.name}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                {project.is_active ? <div className="page-actions">
                  <button
                    onClick={() => setShowAgentModal(project)}
                    className="button button--secondary"
                  >
                    {t('projects.manageAgents')}
                  </button>
                  <button
                    onClick={() => setNewRoomProject(project)}
                    className="button button--primary"
                  >
                    <MessageSquare size={16} />
                    <span>{t('projects.newRoom')}</span>
                  </button>
                </div> : <div className="page-actions">
                  <span className="status-text status-text--neutral">{t('projects.archivedProject')}</span>
                  <button type="button" className="button button--secondary" disabled={blocked}
                    aria-label={t('projects.restoreNamed', { project: project.name })}
                    onClick={() => restoreProject(project)}>
                    <ArchiveRestore size={16} aria-hidden="true" />{t('projects.restore')}
                  </button>
                </div>}
                </div>
                <ProjectRoomDetails key={`${identity}:${project.id}`} projectId={project.id} projects={projects} ownerId={ownerId} authEpoch={authEpoch} visible={visibleIds.has(project.id)}
                  onArchiveLegacy={(trigger) => { if (!ownerStillCurrent() || projectCommandBlocked(identity)) return; setArchiveReturnFocus(trigger); setArchiveProject(project) }} />
              </div>
            ))}
          </div>
        )}
        <section id="project-work-records" tabIndex={-1} className="directory-work-records stack" aria-label={t('projects.workRecords')}>
          <ProjectCommandHistory identity={identity} onInspect={inspectCommand}/>
          <ProjectLifecycleHistory identity={identity}/>
          <RoomSetupHistory identity={identity} onResume={project => { if (ownerStillCurrent()) setNewRoomProject(project) }}/>
          <RoomActionHistory identity={identity} onResume={room => { if (ownerStillCurrent()) setResumingRoom(room) }}/>
        </section>
        <details className="directory-supporting-details"><summary>{t('projects.moreTools')}</summary><div className="stack">
        <p className="field-hint">{t('projects.workspace.draftScope')}</p>
        {workspace.drafts.size > 0 && <details className="compact-details"><summary>{t('projects.workspace.drafts')}</summary><div className="stack stack--tight">{[...workspace.drafts.entries()].map(([key, record]) => <button key={key} type="button" className="button button--secondary" onClick={() => { if (!ownerStillCurrent()) return; setEditingProject(record.baseline); setShowCreateModal(!record.baseline) }}>{key === 'new' ? t('projects.workspace.newProject') : `#${key}`} · {record.value.name || t('editor.continueDraft')}</button>)}</div></details>}
        </div></details>
        <WorkspaceTransfer key={identity} identity={identity} authEpoch={authEpoch} />
      </main>

      {/* Create/Edit Modal */}
      {(showCreateModal || (editingProject && editingProject.user_id === ownerId)) && (
        <ProjectModal key={`${identity}:${editingProject?.id ?? 'new'}`}
          project={workspace.drafts.get(draftKey(editingProject?.id))?.baseline ?? editingProject}
          currentProject={projectBasis.data}
          basisFetching={projectBasis.isFetching}
          basisError={projectBasis.isError}
          basisReceivedAt={projectBasis.dataUpdatedAt}
          onReadBasis={() => { if (ownerStillCurrent() && editingProject) void projectBasis.refetch({ cancelRefetch: false }) }}
          draft={workspace.drafts.get(draftKey(editingProject?.id))?.value}
          onKeepDraft={(value) => { if (!ownerStillCurrent()) return
            const sent = readProjectWorkspace(identity).commands.find(command => command.id === submittedHere.current)
            if (sent?.phase === 'succeeded' && JSON.stringify(value) === JSON.stringify(sent.draft)) return
            const baseline = workspace.drafts.get(draftKey(editingProject?.id))?.baseline ?? editingProject
            keepProjectDraft(identity, generation, editingProject?.id, value, baseline, defaultProjectDraft(baseline))
          }}
          onDiscardDraft={() => { if (ownerStillCurrent()) { discardProjectDraft(identity, generation, editingProject?.id); if (editingProject && projectBasis.data) setEditingProject(projectBasis.data) } }}
          busy={saving}
          blocked={blocked}
          identity={identity}
          onInspect={inspectCommand}
          onClose={() => { setShowCreateModal(false); setEditingProject(null) }}
          onSubmit={(data) => {
            if (!ownerStillCurrent() || projectLifecycleBlocked(identity) || editingProject && (projectBasis.isFetching || projectBasis.isError || !projectBasis.data?.is_active)) return
            const original = beginProjectCommand(identity, generation, { kind: editingProject ? 'update' : 'create', target: { id: editingProject?.id, name: data.name }, body: data, draft: data, snapshotVersion: projectsQuery.dataUpdatedAt })
            if (original) { submittedHere.current = original.id; void sendCommand(original) }
          }}
        />
      )}

      {/* Agent Assignment Modal */}
      {showAgentModal && (
        <ProjectAgentAssignment
          key={`${identity}:${showAgentModal.id}`} identity={identity}
          project={showAgentModal}
          allAgents={allAgents}
          agentListState={agentListState}
          onClose={() => setShowAgentModal(null)}
        />
      )}
      {resumingRoom && <RoomActionResumeDialog key={`${identity}:${resumingRoom.id}:${generation}`} room={resumingRoom} identity={identity} authEpoch={authEpoch} onClose={() => setResumingRoom(null)}/>}
      {newRoomProject && <NewRoomModal key={`${identity}:${newRoomProject.id}:${generation}`} project={newRoomProject} agents={allAgents} agentListState={agentListState} identity={identity} authEpoch={authEpoch}
        onClose={() => setNewRoomProject(null)}
        onCreated={(roomId) => { if (!ownerStillCurrent()) return; queryClient.invalidateQueries({ queryKey: ['rooms', newRoomProject.id] }); navigate(`/chat/${roomId}`) }} />}
      {archiveProject && <ProjectArchiveDialog key={`${identity}:${archiveProject.id}`} project={archiveProject} identity={identity}
        returnFocus={archiveReturnFocus} onClose={() => { if (lifecycleOwnerCurrent(identity, generation)) setArchiveProject(null) }} />}
    </AppShell>
  )
}

function ProjectActivity({ project }: { project: Project }) {
  const { t, formatDateTime } = useI18n()
  const summary = project.room_summary
  const timestamp = reviewTimestamp(summary?.latest_activity_at)
  return <div className="metadata project-card__summary">
    <span>{summary ? t('projects.summaryRooms', { total: summary.total_rooms, active: summary.active_rooms }) : t('projects.summaryUnavailable')}</span>
    {timestamp && <time dateTime={timestamp} title={t('projects.latestActivity')}>{formatDateTime(timestamp, { dateStyle: 'medium', timeStyle: 'short' })}</time>}
  </div>
}

function ProjectRoomDetails({ visible, ...props }: Parameters<typeof ProjectRooms>[0] & { visible: boolean }) {
  const { t } = useI18n()
  const identity = `${props.ownerId || 0}:${props.authEpoch}`
  const [open, setOpen] = useDirectoryValue<boolean>(identity, `project.${props.projectId}.rooms.open`, false)
  return <details open={open} className="compact-details project-card__rooms" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t('projects.roomDetails')}</summary>
    <ProjectRooms {...props} enabled={open && visible} />
  </details>
}

function ProjectRooms({ projectId, projects, ownerId, authEpoch, onArchiveLegacy, enabled = true }: {
  projectId: number; projects: Project[]; ownerId?: number; authEpoch: number; enabled?: boolean; onArchiveLegacy: (trigger: HTMLElement) => void
}) {
  const { t } = useI18n()
  const identity = `${ownerId || 0}:${authEpoch}`
  const projectActive = projects.find((project) => project.id === projectId)?.is_active !== false
  const [savedStatus, setStatus] = useDirectoryValue<'active' | 'closed' | 'all'>(identity, `project.${projectId}.rooms.status`, projectActive ? 'active' : 'all')
  const status = projectActive && savedStatus === 'all' ? 'active' : savedStatus
  const roomsQuery = useQuery({ retry: false, queryKey: ['rooms', identity, projectId, status], enabled, queryFn: async () => {
    const result = await conversationsApi.listByProject(projectId, status)
    if (`${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}` !== identity) throw new Error('Owner changed')
    return result
  } })
  const rooms = roomsQuery.data || []
  return <section className="stack stack--tight" aria-label={`${t('projects.existingRooms')} ${projectId}`}>
    <div className="split"><h4 className="section-title">{t('projects.existingRooms')}</h4><div className="segmented-control" role="group" aria-label={t('projects.roomStatus')}>
      <button type="button" aria-pressed={status === 'active'} onClick={() => setStatus('active')}>{t('projects.activeRooms')}</button>
      <button type="button" aria-pressed={status === 'closed'} onClick={() => setStatus('closed')}>{t('projects.closedRooms')}</button>
      {!projectActive && <button type="button" aria-pressed={status === 'all'} onClick={() => setStatus('all')}>{t('projects.allRooms')}</button>}
    </div></div>
    {roomsQuery.isPending && <p role="status" className="metadata">{t('common.loading')}</p>}
    <ListLoadNotice failed={roomsQuery.isError} fetching={roomsQuery.isFetching} hasSnapshot={roomsQuery.data !== undefined}
      message={t('projects.roomsFailure')} onRetry={() => { if (`${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}` === identity) void roomsQuery.refetch() }} />
    {!projectActive && status !== 'closed' && rooms.some((room) => room.is_active) && <div className="notice notice--warning stack stack--tight"><p role="status">{t('projects.legacyActiveRooms', { count: rooms.filter((room) => room.is_active).length })}</p>
      <button type="button" className="button button--secondary button--compact" onClick={(event) => onArchiveLegacy(event.currentTarget)}>{t('projects.reviewLegacyRooms')}</button></div>}
    {!roomsQuery.isPending && !roomsQuery.isError && rooms.length === 0 && <p className="metadata">{status === 'active' ? t('projects.noActiveRooms')
      : status === 'closed' ? t('projects.noClosedRooms') : t('projects.noRooms')}</p>}
    <ul className="room-list">{rooms.map((room) => <li key={`${identity}:${room.id}`} className="room-list__item"><div className="split">
      <Link className="text-link" to={`/chat/${room.id}`}>{room.title}</Link>{room.is_active === false && <span className="status-text status-text--neutral">{t('projects.closedRoom')}</span>}
    </div>{projectActive && <RoomActions room={room} projects={projects} identity={identity} authEpoch={authEpoch} />}</li>)}</ul>
  </section>
}

function defaultProjectDraft(project: Project | null): ProjectDraft { return { name: project?.name || '', description: project?.description || '' } }

function ProjectModal({
  project,
  currentProject,
  basisFetching,
  basisError,
  basisReceivedAt,
  onReadBasis,
  draft,
  onKeepDraft,
  onDiscardDraft,
  busy,
  blocked,
  identity,
  onInspect,
  onClose,
  onSubmit,
}: {
  project: Project | null
  currentProject?: Project
  basisFetching: boolean
  basisError: boolean
  basisReceivedAt: number
  onReadBasis: () => void
  draft?: ProjectDraft
  onKeepDraft: (value: ProjectDraft) => void
  onDiscardDraft: () => void
  busy: boolean
  blocked: boolean
  identity: string
  onInspect: (command: ProjectCommand) => Promise<Project[]>
  onClose: () => void
  onSubmit: (data: ProjectDraft) => void
}) {
  const imeGuard = useImeFormGuard(`${identity}:project:${project?.id ?? 'new'}`)
  const initialDraft = draft ?? defaultProjectDraft(project)
  const [name, setName] = useState(initialDraft.name)
  const [description, setDescription] = useState(initialDraft.description)
  const [showErrors, setShowErrors] = useState(false)
  const nameLength = editorTextLength(name), descriptionLength = editorTextLength(description)
  const errors = { name: !name.trim() ? 'editor.requiredField' : nameLength > 100 ? 'editor.nameLimit' : null,
    description: descriptionLength > 1000 ? 'editor.descriptionLimit' : null } as const
  const { t, formatDateTime } = useI18n()
  const dialogRef = useRef<HTMLDivElement>(null)
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  const focusGeneration = useRef(directoryViewGeneration()).current
  const closeRef = useRef(onClose)
  const [restoredDraft, setRestoredDraft] = useState(Boolean(draft))
  const keepDraft = useRef(onKeepDraft); keepDraft.current = onKeepDraft
  useLayoutEffect(() => { keepDraft.current({ name, description }) }, [name, description])
  const currentBasis = JSON.stringify(currentProject ? defaultProjectDraft(currentProject) : null)
  const basisChanged = !!project && !!currentProject && JSON.stringify(defaultProjectDraft(project)) !== currentBasis
  const [basisAcknowledged, setBasisAcknowledged] = useState(false)
  useEffect(() => { setBasisAcknowledged(false) }, [currentBasis, name, description])
  const targetUnavailable = !!project && (basisFetching || basisError || !currentProject?.is_active)
  const closeEditor = () => {
    if (`${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}` !== identity) return
    onKeepDraft({ name, description }); onClose()
  }
  closeRef.current = closeEditor
  const discardDraft = () => {
    if (busy || `${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}` !== identity) return
    const initial = defaultProjectDraft(currentProject || project)
    setName(initial.name); setDescription(initial.description); setRestoredDraft(false); onDiscardDraft(); focusEditorField(dialogRef.current, 'name')
  }

  useDialogFocus(dialogRef, () => closeRef.current(), { restoreFocus: false })
  useEffect(() => () => {
    const owner = useAuthStore.getState(), target = returnFocus.current
    const modal = Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).some(node => node.getClientRects().length > 0
      && !node.closest('[hidden], [inert], [aria-hidden="true"]'))
    if (owner.token && identity === `${owner.user?.id || 0}:${owner.epoch}` && focusGeneration === directoryViewGeneration()
      && !modal && document.visibilityState === 'visible' && document.hasFocus() && target?.isConnected
      && target.getClientRects().length > 0 && !target.matches(':disabled') && !target.closest('[hidden], [inert], [aria-hidden="true"]')) target.focus({ preventScroll: true })
  }, [])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (imeGuard.isSubmitBlocked(e)) return
    if (busy || blocked || targetUnavailable || basisChanged && !basisAcknowledged) return
    const invalid = (Object.keys(errors) as Array<keyof typeof errors>).find(key => errors[key])
    if (invalid) { setShowErrors(true); focusEditorField(dialogRef.current, invalid); return }
    onSubmit({ name, description })
  }

  return (
    <div className="modal-backdrop">
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="project-dialog-title" className="modal-panel">
          <h2 id="project-dialog-title" className="page-title">
            {t(project ? 'projects.edit' : 'projects.create')}
          </h2>

          <p className="sr-only">{t('editor.keyboardHelp')}</p>
          <form {...imeGuard.handlers} onSubmit={handleSubmit} noValidate className="form-grid" aria-busy={busy}>
            {showErrors && Object.values(errors).some(Boolean) && <p role="alert" className="notice notice--warning">{t('editor.inputIssues')}</p>}
            <EditorDraftNotice restored={restoredDraft} busy={busy} revision={JSON.stringify({name, description, currentBasis, basisReceivedAt})} onDiscard={discardDraft} />
            <ProjectCommandHistory identity={identity} onInspect={onInspect}/>
            {blocked && !busy && <p role="alert" className="notice notice--warning">{t('projects.workspace.blocked')}</p>}
            {project && <div className="notice notice--info stack stack--tight">
              <button type="button" className="button button--secondary button--compact" disabled={basisFetching} onClick={onReadBasis}>{t('projects.workspace.readTarget')}</button>
              {basisFetching && <p role="status">{t('common.loading')}</p>}
              {basisError && <p role="alert">{t('projects.workspace.targetError')}</p>}
              {currentProject && <p>{t(currentProject.is_active ? 'projects.activeProject' : 'projects.workspace.archivedTarget')}</p>}
              {basisReceivedAt > 0 && <p className="metadata">{t('projects.workspace.targetReceived', { time: formatDateTime(new Date(basisReceivedAt).toISOString(), { dateStyle: 'medium', timeStyle: 'short' }) })}</p>}
            </div>}
            {basisChanged && <details className="notice notice--warning compact-details"><summary>{t('projects.workspace.changedBasis')}</summary><p>{t('projects.workspace.basisHelp')}</p><ProjectDraftComparison baseline={defaultProjectDraft(project)} received={defaultProjectDraft(currentProject||null)} draft={{name,description}}/><label className="checkbox-row"><input type="checkbox" checked={basisAcknowledged} disabled={busy} onChange={event => setBasisAcknowledged(event.target.checked)}/>{t('projects.workspace.basisAcknowledge')}</label></details>}
            <fieldset disabled={busy} className="form-grid" style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
              <label className="field">
                {t('projects.name')}
              <input
                data-dialog-initial data-editor-field="name" aria-invalid={Boolean(errors.name && (showErrors || nameLength > 100))} aria-describedby="project-name-help"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
                <span id="project-name-help" className={errors.name && (showErrors || nameLength > 100) ? 'metadata metadata--danger' : 'sr-only'}>{t(showErrors && errors.name ? errors.name : 'editor.nameLimit', { count: nameLength })}</span>
              </label>

              <label className="field">
                {t('projects.description')}
              <textarea
                data-editor-field="description" aria-invalid={Boolean(errors.description)} aria-describedby="project-description-help"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
              />
                <span id="project-description-help" className={errors.description ? 'metadata metadata--danger' : 'sr-only'}>{t('editor.descriptionLimit', { count: descriptionLength })}</span>
              </label>

            </fieldset>
            <div className="page-actions">
              <button
                type="button"
                onClick={closeEditor}
                className="button button--secondary"
              >
                {t(busy ? 'common.close' : 'common.cancel')}
              </button>
              <button
                type="submit"
                disabled={busy || blocked || targetUnavailable || basisChanged && !basisAcknowledged}
                className="button button--primary"
              >
                {t(busy ? 'common.saving' : project ? 'common.edit' : 'common.create')}
              </button>
            </div>
          </form>
      </div>
    </div>
  )
}

