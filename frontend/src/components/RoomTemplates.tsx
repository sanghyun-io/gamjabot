import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { Agent } from '../api/agents'
import type { Project } from '../api/projects'
import { roomTemplatesApi, type RoomTemplate, type RoomTemplateWrite } from '../api/roomTemplates'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import RoomTemplateRemoveDialog, { sameTemplateRemoval } from './RoomTemplateRemoveDialog'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { isRoomSetupResult, requestRoomSetupAction, roomSetupOwnerCurrent } from '../lib/roomSetupActions'
import { applyTemplateToRoomSetup, createdFromRoomSetup, patchRoomSetup, readRoomSetup, roomCreationSignature, roomSetupBlocked, roomSetupFingerprint, roomSetupVersion, subscribeRoomSetup, type RoomSetupCommand, type RoomSetupDraft } from '../lib/roomSetupWorkspace'

type TemplateAction = 'create' | 'update' | 'delete' | 'room'
type TemplateField = 'name' | 'title' | 'rounds' | 'tools' | 'seconds' | 'participants'
type TemplateError = 'validation' | 'name' | 'members' | 'failure' | ''
function parseInteger(value: string, minimum: number, maximum: number): number | null {
  if (!/^\d+$/.test(value)) return null
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed >= minimum && parsed <= maximum ? parsed : null
}
export default function RoomTemplates({ project, agents, identity, authEpoch, externalBusy, onActionStarted, onSharedFieldIssue }: {
  project: Project; agents: Agent[]; identity: string; authEpoch: number; externalBusy: boolean; onActionStarted: (command: RoomSetupCommand) => void; onSharedFieldIssue: (kind: 'validation' | 'agents', field: 'title' | 'participants', value: RoomSetupDraft) => void
}) {
  const { t } = useI18n(), generation = useRef(directoryViewGeneration()).current
  const [error, setError] = useState<TemplateError>(''), [basisAcknowledged, setBasisAcknowledged] = useState(false)
  const [removing, setRemoving] = useState<RoomTemplate | null>(null)
  const queryClient = useQueryClient()
  const editorRoot = useRef<HTMLDetailsElement>(null)
  const [validationAttempt, setValidationAttempt] = useState<{ fingerprint: string; fields: TemplateField[] } | null>(null)
  const [focusIssue, setFocusIssue] = useState<{ field: TemplateField; fingerprint: string } | null>(null)
  const fieldId = (field: TemplateField) => `room-template-${project.id}-${field}`
  const ownIntent = useRef<string | null>(null), mounted = useRef(false)
  useSyncExternalStore(subscribeRoomSetup, roomSetupVersion)
  const workspace = readRoomSetup(identity), record = workspace.drafts.get(project.id)
  const draft = record!.value, open = draft.templatesOpen, selectedId = draft.templateId, title = draft.title, selected = draft.selected
  const name = draft.templateName, rounds = draft.templateRounds, tools = draft.templateTools, seconds = draft.templateSeconds
  const sameOwner = () => roomSetupOwnerCurrent(identity, generation) && authEpoch === useAuthStore.getState().epoch
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; ownIntent.current = null } }, [])
  const change = (patch: Partial<RoomSetupDraft>) => { if (sameOwner()) patchRoomSetup(identity, generation, project.id, patch) }
  const setName = (value: string) => change({ templateName: value }), setRounds = (value: string) => change({ templateRounds: value }), setTools = (value: string) => change({ templateTools: value }), setSeconds = (value: string) => change({ templateSeconds: value })
  const busy = !!workspace.pending
  const templates = useQuery({ queryKey: ['room-templates', identity, project.id], enabled: open, retry: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
    queryFn: async () => { if (!sameOwner()) throw new Error('Owner changed'); const values = await roomTemplatesApi.list(project.id); if (!sameOwner() || values.some(value => value.project_id !== project.id || value.user_id !== useAuthStore.getState().user?.id)) throw new Error('Template target changed'); return values } })
  const receivedTemplate = templates.data?.find(value => value.id === selectedId) || null
  const selectedTemplate = draft.templateBaseline?.id === selectedId ? draft.templateBaseline : receivedTemplate
  const changedBasis = !!selectedTemplate && !!receivedTemplate && JSON.stringify(selectedTemplate) !== JSON.stringify(receivedTemplate)
  useEffect(() => { setBasisAcknowledged(false) }, [JSON.stringify(receivedTemplate), roomSetupFingerprint(draft)])
  const problems = (value: RoomSetupDraft): TemplateField[] => {
    const fields: TemplateField[] = []
    if (!value.templateName.trim() || value.templateName.trim().length > 100) fields.push('name')
    if (!value.title.trim() || value.title.trim().length > 200) fields.push('title')
    if (parseInteger(value.templateRounds, 1, 4) === null) fields.push('rounds')
    if (parseInteger(value.templateTools, 0, 64) === null) fields.push('tools')
    if (parseInteger(value.templateSeconds, 1, 3600) === null) fields.push('seconds')
    if (new Set(value.selected).size > 64 || value.selected.some(id => !agents.some(agent => agent.id === id && agent.is_active))) fields.push('participants')
    return fields
  }
  const activeValidation = validationAttempt?.fingerprint === roomSetupFingerprint(draft) ? validationAttempt : null
  const visibleError = (error === 'validation' || error === 'members') && !activeValidation ? '' : error
  const invalid = (field: TemplateField) => !!visibleError && !!activeValidation?.fields.includes(field)
  const body = (): RoomTemplateWrite | null => {
    const current = readRoomSetup(identity).drafts.get(project.id)?.value
    if (!current || problems(current).length) return null
    const maxRounds = parseInteger(current.templateRounds, 1, 4), maxTools = parseInteger(current.templateTools, 0, 64), maxSeconds = parseInteger(current.templateSeconds, 1, 3600)
    const cleanName = current.templateName.trim(), cleanTitle = current.title.trim()
    if (!cleanName || cleanName.length > 100 || !cleanTitle || cleanTitle.length > 200 || maxRounds === null || maxTools === null || maxSeconds === null) return null
    return { name: cleanName, room_title: cleanTitle, agent_ids: [...new Set(current.selected)].sort((a, b) => a - b), max_rounds: maxRounds, max_tool_calls: maxTools, max_executor_seconds: maxSeconds }
  }
  const apply = (template: RoomTemplate) => { if (sameOwner() && !busy) { applyTemplateToRoomSetup(identity, generation, template); setError('') } }
  useEffect(() => {
    const command = workspace.commands.find(value => value.id === ownIntent.current)
    if (!command || !mounted.current || !sameOwner()) return
    if (!open || command.waitStopped || workspace.commands[workspace.commands.length - 1]?.id !== command.id) { ownIntent.current = null; return }
    if (command.phase === 'pending') return
    ownIntent.current = null
    const current = readRoomSetup(identity).drafts.get(project.id)?.value
    if (command.phase === 'succeeded' && current && roomSetupFingerprint(current) === command.draftFingerprint) {
      if (command.result && !isRoomSetupResult(command.result)) apply(command.result)
      else if (command.kind === 'template-delete') change({ templateId: null, templateBaseline: null })
    }
  }, [workspace.commands, open])
  const sameIds = (left: number[], right: number[]) => left.length === right.length && left.every((id, index) => id === right[index])
  const sortedSelected = [...new Set(selected)].sort((a, b) => a - b)
  const dirty = !!receivedTemplate && (name.trim() !== receivedTemplate.name || title.trim() !== receivedTemplate.room_title || !sameIds(sortedSelected, receivedTemplate.agent_ids)
    || rounds !== String(receivedTemplate.max_rounds) || tools !== String(receivedTemplate.max_tool_calls) || seconds !== String(receivedTemplate.max_executor_seconds))
  const initialTeam = (project.agents || []).filter(agent => agent.is_active).map(agent => agent.id).sort((a, b) => a - b)
  const unsaved = selectedTemplate ? dirty : !!(name.trim() || title.trim() || rounds !== '2' || tools !== '8' || seconds !== '60' || !sameIds(sortedSelected, initialTeam))
  const locked = busy || roomSetupBlocked(identity) || externalBusy || templates.isFetching || templates.isError
  useLayoutEffect(() => {
    if (!focusIssue || !open || !mounted.current || !sameOwner() || locked) return
    const root = editorRoot.current, active = document.activeElement
    const current = readRoomSetup(identity).drafts.get(project.id)?.value
    if (!root || !current || roomSetupFingerprint(current) !== focusIssue.fingerprint || !active || !root.contains(active)
      || active.closest('[aria-modal="true"]') !== root.closest('[aria-modal="true"]')) return
    const input = root.querySelector<HTMLElement>(`[data-template-field="${focusIssue.field}"]`)
    if (!input || input.matches(':disabled')) return
    for (let parent = input.parentElement; parent && root.contains(parent); parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) parent.open = true
    }
    input.focus({ preventScroll: true }); input.scrollIntoView({ block: 'nearest' })
  }, [focusIssue])
  const unavailableIds = receivedTemplate?.agent_ids.filter(id => !agents.some(agent => agent.id === id && agent.is_active)) || []
  const knownRoom = receivedTemplate && createdFromRoomSetup(identity, roomCreationSignature('template-room', project.id, draft.intent, undefined, receivedTemplate))
  const runWrite = (action: TemplateAction, value?: RoomTemplateWrite) => {
    if (!sameOwner() || locked || action !== 'create' && (!receivedTemplate || changedBasis && !basisAcknowledged)) return
    let target = receivedTemplate!
    if (action === 'delete') {
      // Inspect the existing cache and shared workspace at the confirmation
      // click. This is not a new list request or a server-side revision lock.
      const latest = queryClient.getQueryState(['room-templates', identity, project.id])
      const current = queryClient.getQueryData<RoomTemplate[]>(['room-templates', identity, project.id])?.find(item => item.id === removing?.id)
      const live = readRoomSetup(identity)
      const liveDraft = live.drafts.get(project.id)?.value
      if (!mounted.current || !removing || !liveDraft?.templatesOpen || liveDraft.templateId !== removing.id
        || live.pending || roomSetupBlocked(identity) || latest?.status !== 'success' || latest.fetchStatus !== 'idle'
        || !current || !sameTemplateRemoval(removing, current) || current.project_id !== project.id || current.user_id !== useAuthStore.getState().user?.id) return
      target = current
    }
    if (action === 'room' && dirty && !window.confirm(t('projects.templates.useSavedConfirm'))) return
    const original = requestRoomSetupAction(identity, generation, project, action === 'room' ? 'template-room' : action === 'delete' ? 'template-delete' : action === 'update' ? 'template-update' : 'template-create', value, action === 'create' ? undefined : target)
    if (original) { if (action === 'delete') setRemoving(null); ownIntent.current = original.id; setError(''); onActionStarted(original) }
  }
  const submit = (mode: 'create' | 'update') => {
    if (locked || !sameOwner()) return
    const current = readRoomSetup(identity).drafts.get(project.id)?.value
    if (!current) return
    const fields = problems(current), value = body()
    if (!value) {
      const field = fields[0] || 'name', fingerprint = roomSetupFingerprint(current)
      setValidationAttempt({ fingerprint, fields }); setError(field === 'participants' ? 'members' : 'validation')
      if (field === 'title' || field === 'participants') {
        setFocusIssue(null); onSharedFieldIssue(field === 'participants' && new Set(current.selected).size <= 64 ? 'agents' : 'validation', field, current)
      } else setFocusIssue({ field, fingerprint })
      return
    }
    runWrite(mode === 'update' && selectedId ? 'update' : 'create', value)
  }
  const selectTemplate = (id: number) => {
    if (!sameOwner() || busy || externalBusy || id === selectedId) return
    if (unsaved && !window.confirm(t('projects.templates.discardSelection'))) return
    ownIntent.current = null
    const template = templates.data?.find(value => value.id === id)
    if (template) apply(template)
    else { change({ templateId: null, templateBaseline: null, templateName: '' }); setError('') }
  }
  return <><details ref={editorRoot} open={open} className="room-template compact-details" onKeyDown={event => { if (event.key === 'Enter' && event.target instanceof HTMLInputElement && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); event.stopPropagation() } }} onToggle={event => { if (event.target !== event.currentTarget || !sameOwner()) return; if (!event.currentTarget.open) { ownIntent.current = null; setRemoving(null) }; change({ templatesOpen: event.currentTarget.open }) }}>
    <summary>{t('projects.templates.title')}{busy && ` · ${t('common.loading')}`}</summary>
    {open && <div className="room-template__body stack stack--tight">
      {templates.isLoading && <p role="status" className="metadata">{t('common.loading')}</p>}
      {templates.isError && <div className="notice notice--danger"><span>{t('projects.templates.loadFailure')}</span>
        <button type="button" className="text-link" disabled={templates.isFetching} onClick={() => { if (sameOwner()) void templates.refetch({ cancelRefetch: false }) }}>{t('common.retry')}</button></div>}
      <p className="field-hint">{t('projects.setup.templateContract')}</p>
      {(templates.isFetching || templates.isError) && templates.data && <p>{t('projects.setup.oldTemplates')}</p>}
      {changedBasis && <details className="notice notice--warning compact-details"><summary>{t('projects.setup.templateChanged')}</summary><pre className="technical">{JSON.stringify(receivedTemplate, null, 2)}</pre><label className="check-row"><input type="checkbox" checked={basisAcknowledged} disabled={busy} onChange={event => setBasisAcknowledged(event.target.checked)}/>{t('projects.setup.templateAcknowledge')}</label></details>}
      {knownRoom && <p>{t('projects.setup.alreadyCreated')}</p>}
      {templates.data && <>
        <label className="field">{t('projects.templates.saved')}<select disabled={locked} value={selectedId || ''} onChange={(event) => selectTemplate(Number(event.target.value))}><option value="">{templates.data.length ? t('projects.templates.choose') : t('projects.templates.empty')}</option>
          {templates.data.map((template) => <option key={template.id} value={template.id}>{template.name}</option>)}</select></label>
        {selectedTemplate && <div className="room-template__preview stack stack--tight">
          <strong>{selectedTemplate.name}</strong><span>{selectedTemplate.room_title}</span>
          <span>{selectedTemplate.agent_ids.length ? t('projects.templates.memberCount', { count: selectedTemplate.agent_ids.length }) : t('projects.templates.noMembers')}</span>
          <span>{t('projects.cloneSettings', { rounds: selectedTemplate.max_rounds, tools: selectedTemplate.max_tool_calls, seconds: selectedTemplate.max_executor_seconds })}</span>
          <details><summary>{t('projects.templates.members')}</summary><div className="page-actions">
            {selectedTemplate.agent_ids.length ? selectedTemplate.agent_ids.map((id) => <span key={id} className="status-text status-text--neutral">
              {agents.find((agent) => agent.id === id)?.name || t('projects.templates.missingMember', { id })}</span>) : <span className="metadata">{t('projects.templates.noMembers')}</span>}
          </div></details>
          {unavailableIds.length > 0 && <p className="metadata metadata--danger">{t('projects.templates.unavailablePreview')}</p>}
          {dirty && <p className="metadata metadata--danger">{t('projects.templates.savedOnly')}</p>}
          <div className="page-actions"><button type="button" className="button button--primary button--compact" disabled={locked || unavailableIds.length > 0 || !!knownRoom || changedBasis && !basisAcknowledged || !receivedTemplate}
            onClick={() => void runWrite('room')}>{t('projects.templates.use')}</button>
            <button type="button" className="button button--ghost button--compact danger-link" disabled={locked || changedBasis && !basisAcknowledged || !receivedTemplate}
              onClick={() => { if (sameOwner() && !locked && receivedTemplate && (!changedBasis || basisAcknowledged)) setRemoving({ ...receivedTemplate, agent_ids: [...receivedTemplate.agent_ids] }) }}>{t('projects.templates.delete')}</button></div>
        </div>}
        <div className="room-template__editor stack stack--tight"><strong>{selectedTemplate ? t('projects.templates.edit') : t('projects.templates.saveCurrent')}</strong>
          <label className="field">{t('projects.templates.name')}<input data-template-field="name" aria-invalid={invalid('name') || undefined} aria-describedby={fieldId('name')} disabled={locked} maxLength={100} value={name} onChange={(event) => { setName(event.target.value); setError(''); ownIntent.current = null }} /><span id={fieldId('name')} className={`field-hint${invalid('name') ? ' metadata--danger' : ''}`}>{t('projects.templates.nameHint')}</span></label>
          <p className="metadata">{t('projects.templates.currentHelp')}</p>
          {invalid('title') && <p className="metadata metadata--danger">{t('projects.roomTitleValidation')}</p>}
          {invalid('participants') && <p className="metadata metadata--danger">{t(new Set(selected).size > 64 ? 'projects.setup.participantLimit' : 'projects.templates.membersUnavailable')}</p>}
          <details><summary>{t('projects.templates.policy')}</summary><div className="form-grid form-grid--three">
            <label className="field">{t('chat.policy.rounds')}<input data-template-field="rounds" aria-invalid={invalid('rounds') || undefined} aria-describedby={fieldId('rounds')} disabled={locked} inputMode="numeric" value={rounds} onChange={(event) => { setRounds(event.target.value); setError(''); ownIntent.current = null }} /><span id={fieldId('rounds')} className={`field-hint${invalid('rounds') ? ' metadata--danger' : ''}`}>{t('chat.policy.roundsHint')}</span></label>
            <label className="field">{t('chat.policy.tools')}<input data-template-field="tools" aria-invalid={invalid('tools') || undefined} aria-describedby={fieldId('tools')} disabled={locked} inputMode="numeric" value={tools} onChange={(event) => { setTools(event.target.value); setError(''); ownIntent.current = null }} /><span id={fieldId('tools')} className={`field-hint${invalid('tools') ? ' metadata--danger' : ''}`}>{t('chat.policy.toolsHint')}</span></label>
            <label className="field">{t('chat.policy.executor')}<input data-template-field="seconds" aria-invalid={invalid('seconds') || undefined} aria-describedby={fieldId('seconds')} disabled={locked} inputMode="numeric" value={seconds} onChange={(event) => { setSeconds(event.target.value); setError(''); ownIntent.current = null }} /><span id={fieldId('seconds')} className={`field-hint${invalid('seconds') ? ' metadata--danger' : ''}`}>{t('chat.policy.executorHint')}</span></label>
          </div></details>
          {visibleError && visibleError !== 'failure' && <p role="alert" className="metadata metadata--danger">{t(visibleError === 'validation' ? 'projects.templates.validation' : visibleError === 'name'
            ? 'projects.templates.nameConflict' : visibleError === 'members' ? 'projects.templates.membersUnavailable' : 'projects.templates.failure')}</p>}
          
          <div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={locked} onClick={() => submit('create')}>{t('projects.templates.saveNew')}</button>
            {selectedTemplate && <button type="button" className="button button--secondary button--compact" disabled={locked || changedBasis && !basisAcknowledged || !receivedTemplate} onClick={() => submit('update')}>{t('projects.templates.saveChanges')}</button>}</div>
        </div>
      </>}
    </div>}
  </details>
    {removing && open && <RoomTemplateRemoveDialog target={removing} current={receivedTemplate || undefined} projectName={project.name} agents={agents}
      ready={templates.isSuccess && templates.fetchStatus === 'idle'} blocked={locked || changedBasis && !basisAcknowledged}
      canUse={() => mounted.current && sameOwner()} onClose={() => setRemoving(null)} onConfirm={() => runWrite('delete')} />}
  </>
}
