import RoomPolicyRestoreDialog, { samePolicyDraft,type PolicyRestoreTarget } from './RoomPolicyRestoreDialog'
import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Gauge } from 'lucide-react'
import { schedulesApi, type RoomSchedule } from '../api/schedules'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { closeOtherRoomPanels } from '../lib/roomPanels'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { editorSaveFailure } from './EditorSaveNotice'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { readScheduleEditor, subscribeScheduleEditors, updateScheduleEditor, type DraftSchedule } from '../lib/roomScheduleEditorState'

const toDraft = (value: RoomSchedule): DraftSchedule => ({
  max_rounds: String(value.max_rounds),
  max_tool_calls: String(value.max_tool_calls),
  max_executor_seconds: String(value.max_executor_seconds),
})

function parseField(value: string, min: number, max: number) {
  if (!/^(0|[1-9]\d*)$/.test(value)) return null
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed >= min && parsed <= max ? parsed : null
}

const policyFields = [
  { name: 'max_rounds', min: 1, max: 4, label: 'chat.policy.rounds', hint: 'chat.policy.roundsHint' },
  { name: 'max_tool_calls', min: 0, max: 64, label: 'chat.policy.tools', hint: 'chat.policy.toolsHint' },
  { name: 'max_executor_seconds', min: 1, max: 3600, label: 'chat.policy.executor', hint: 'chat.policy.executorHint' },
] as const
const invalidPolicyFields = (draft: DraftSchedule) => policyFields.filter(field => parseField(draft[field.name], field.min, field.max) === null)
type PolicyField = typeof policyFields[number]['name']
const receivedPolicyDraft=(value:RoomSchedule|undefined):DraftSchedule|null=>value&&policyFields.every(field=>typeof value[field.name]==='number'&&Number.isSafeInteger(value[field.name])&&value[field.name]>=field.min&&value[field.name]<=field.max)?toDraft(value):null

export default function RoomScheduleSettings({ roomId, active, authEpoch }: {
  roomId: number
  active: boolean
  authEpoch: number
}) {
  const { t } = useI18n()
  const fieldPrefix = useId(), formRef = useRef<HTMLFormElement>(null),panelRef=useRef<HTMLDetailsElement>(null)
  const [review,setReview]=useState<PolicyRestoreTarget|null>(null),reviewRef=useRef<PolicyRestoreTarget|null>(null)
  const [focusIssue, setFocusIssue] = useState<{ field: PolicyField; revision: number; draft: DraftSchedule; origin: Element | null } | null>(null)
  const user = useAuthStore((state) => state.user)
  const cache = useQueryClient()
  const identity = `${roomId}:${user?.id ?? 'none'}:${authEpoch}`
  const mounted = useRef(true), scope = useRef(identity)
  const generation = directoryViewGeneration()
  scope.current = identity
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const sameOwner = (sent = identity) => {
    const owner = useAuthStore.getState()
    return directoryViewGeneration() === generation && `${roomId}:${owner.user?.id ?? 'none'}:${owner.epoch}` === sent
  }
  const current = (sent = identity) => mounted.current && scope.current === sent && sameOwner(sent)
  const editor = useSyncExternalStore(subscribeScheduleEditors, () => readScheduleEditor(identity, roomId))
  const { draft, validationError, phase, inspecting, saved, dirty } = editor
  const edit = (update: Parameters<typeof updateScheduleEditor>[3]) => updateScheduleEditor(identity, roomId, generation, update)
  const imeGuard = useImeFormGuard(`${identity}:policy`)
  const key = ['room-schedule', roomId, user?.id, authEpoch] as const
  const schedule = useQuery({
    queryKey: key,
    queryFn: async () => {
      const sent = identity
      if (!sameOwner(sent)) throw new Error('Owner changed')
      const result = await schedulesApi.get(roomId)
      if (!sameOwner(sent)) throw new Error('Owner changed')
      return result
    },
    staleTime: Infinity, retry: false,
  })
  useEffect(() => {
    if (!schedule.data || !sameOwner()) return
    const value = toDraft(schedule.data)
    edit(previous => previous.draft === null ? { ...previous, draft: value } : previous)
  }, [schedule.data, identity])

  const inspect = async () => {
    if (!current()) return
    const claimed = edit(previous => previous.phase === 'saving' || previous.inspecting
      ? previous : { ...previous, inspecting: true, saved: false })
    if (!claimed) return
    const sent = identity
    try { await schedule.refetch({ throwOnError: true }) }
    catch { /* A failed read leaves the current-session input intact. */ }
    finally { if (sameOwner(sent)) edit(previous => ({ ...previous, inspecting: false })) }
  }
  const closeReview=()=>{if(!current())return;reviewRef.current=null;setReview(null)}
  const restoreSaved = () => {
    const previous=readScheduleEditor(identity,roomId),value=receivedPolicyDraft(schedule.data)
    if(!current()||previous.phase==='saving'||previous.operation!==null||previous.inspecting||schedule.isFetching||!schedule.isSuccess||schedule.isStale||!value||!previous.draft||!panelRef.current?.open||!panelRef.current.getClientRects().length)return
    const target:PolicyRestoreTarget=Object.freeze({id:crypto.randomUUID(),identity,generation,roomId,revision:previous.revision,draft:Object.freeze({...previous.draft}),received:Object.freeze({...value})})
    reviewRef.current=target;setReview(target)
  }
  const applySaved=(target:PolicyRestoreTarget)=>{
    if(!current()||reviewRef.current!==target||target.identity!==identity||target.generation!==generation||target.roomId!==roomId||!panelRef.current?.open||!panelRef.current.getClientRects().length)return
    const source=cache.getQueryState<RoomSchedule>(key),received=receivedPolicyDraft(source?.data)
    if(source?.status!=='success'||source.fetchStatus==='fetching'||source.isInvalidated||!samePolicyDraft(target.received,received))return
    const applied=edit(live=>live.phase==='saving'||live.operation!==null||live.inspecting||live.revision!==target.revision||!samePolicyDraft(live.draft,target.draft)?live:{...live,draft:{...target.received},revision:live.revision+1,
      dirty:false,validationError:false,phase:live.phase==='unknown'?'unknown':'idle',saved:false})
    if(applied){setFocusIssue(null);reviewRef.current=null;setReview(null)}
  }
  useEffect(()=>{reviewRef.current=null;setReview(null)},[identity])
  const busy = phase === 'saving' || inspecting
  const invalidFields = draft && validationError ? invalidPolicyFields(draft) : []
  const fieldId = (name: PolicyField) => `${fieldPrefix}-${name}`
  const requestFieldFocus = (field: PolicyField, value: NonNullable<typeof editor.draft>, revision: number) => {
    setFocusIssue({ field, draft: value, revision, origin: document.activeElement })
  }
  useLayoutEffect(() => {
    if (!focusIssue || !current() || !active || busy || schedule.isFetching || !schedule.isSuccess) return
    const form = formRef.current, element = document.activeElement, live = readScheduleEditor(identity, roomId)
    if (!form || !form.isConnected || !element || element !== focusIssue.origin || !form.contains(element)
      || !live.draft || live.revision !== focusIssue.revision || live.draft !== focusIssue.draft || !live.validationError
      || !invalidPolicyFields(live.draft).some(field => field.name === focusIssue.field)) return
    const input = form.querySelector<HTMLInputElement>(`[data-policy-field="${focusIssue.field}"]`)
    // Only this visible current form may receive focus; another dialog or a closed
    // management panel must not be reopened by an old validation request.
    if (!input || input.disabled || !input.getClientRects().length
      || input.closest('[aria-modal="true"]') !== element.closest('[aria-modal="true"]')) return
    input.focus({ preventScroll: true }); input.scrollIntoView({ block: 'nearest' })
  }, [focusIssue])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    const previous = readScheduleEditor(identity, roomId)
    if (imeGuard.isSubmitBlocked(event) || previous.phase === 'saving' || previous.inspecting
      || schedule.isFetching || !schedule.isSuccess || !current() || !active || !previous.draft) return
    const maxRounds = parseField(previous.draft.max_rounds, 1, 4)
    const maxToolCalls = parseField(previous.draft.max_tool_calls, 0, 64)
    const maxExecutorSeconds = parseField(previous.draft.max_executor_seconds, 1, 3600)
    if (maxRounds === null || maxToolCalls === null || maxExecutorSeconds === null) {
      if (edit(live => live !== previous ? live : { ...live, validationError: true, saved: false })) {
        const first = invalidPolicyFields(previous.draft)[0]
        if (first) requestFieldFocus(first.name, previous.draft, previous.revision)
      }
      return
    }
    if (previous.phase === 'unknown' && !window.confirm(t('chat.policy.retryUnknown'))) return
    if (!current()) return
    const sent = identity, operation = crypto.randomUUID()
    const value = { max_rounds: maxRounds, max_tool_calls: maxToolCalls, max_executor_seconds: maxExecutorSeconds }
    if (!edit(live => live !== previous ? live : { ...live, phase: 'saving', operation, validationError: false, saved: false })) return
    try {
      await cache.cancelQueries({ queryKey: key })
      if (!sameOwner(sent)) return
      const result = await schedulesApi.put(roomId, value)
      if (!sameOwner(sent) || readScheduleEditor(identity, roomId).operation !== operation) return
      cache.setQueryData(key, result)
      edit(live => live.operation !== operation ? live : { ...live,
        draft: live.revision === previous.revision ? toDraft(result) : live.draft,
        dirty: live.revision !== previous.revision, phase: 'idle', operation: null,
        saved: live.revision === previous.revision })
    } catch (failure) {
      if (sameOwner(sent)) edit(live => live.operation !== operation ? live : {
        ...live, phase: editorSaveFailure(failure), operation: null, saved: false })
    }
  }

  const update = (field: keyof RoomSchedule, value: string) => {
    if (!current()) return
    setFocusIssue(null)
    edit(previous => {
      if (previous.phase === 'saving' || !previous.draft) return previous
      const changed = { ...previous.draft, [field]: value }
      return { ...previous, draft: changed, revision: previous.revision + 1,
        dirty: true, validationError: previous.validationError && invalidPolicyFields(changed).length > 0, saved: false }
    })
  }

  return <details ref={panelRef} name="room-panel" className="room-options compact-details"
    onToggle={(event) => {if(event.target!==event.currentTarget)return;closeOtherRoomPanels(event.currentTarget);if(!event.currentTarget.open){reviewRef.current=null;setReview(null)}}}>
    <summary><Gauge size={16} aria-hidden="true" />{t('chat.policy.options')}</summary>
    <div className="room-options__body">
      <div><h2 className="section-title">{t('chat.policy.title')}</h2><p className="section-copy">{t('chat.policy.future')}</p></div>
      <div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={busy || schedule.isFetching} onClick={() => void inspect()}>{t(inspecting || schedule.isFetching ? 'common.loading' : 'chat.policy.refreshValues')}</button></div>
      <p className="metadata">{t('chat.policy.keepDraftHelp')}</p>
      {(dirty || phase !== 'idle') && <p role="status" className="notice notice--info">{t('chat.policy.sessionDraft')}</p>}
      <p className="field-hint">{t('chat.policy.sessionLimit')}</p>
      {schedule.isPending && <p role="status" className="metadata">{t('chat.policy.loading')}</p>}
      {schedule.isError && <p role="alert" className="notice notice--warning">{t('chat.policy.loadFailure')}</p>}
      {draft && <form ref={formRef} {...imeGuard.handlers} className="stack stack--tight" noValidate onSubmit={submit}>
        {schedule.data && <p className="metadata room-policy__saved">{t('chat.policy.savedValues', {
          rounds: schedule.data.max_rounds, tools: schedule.data.max_tool_calls, seconds: schedule.data.max_executor_seconds,
        })}</p>}
        <p className="metadata">{t('chat.policy.snapshotHelp')}</p>
        <button type="button" className="text-link" disabled={busy || schedule.isFetching || !schedule.isSuccess || schedule.isStale || !receivedPolicyDraft(schedule.data)} onClick={restoreSaved}>{t('chat.policy.compare.open')}</button>
        {!active && <p className="notice notice--info">{t('chat.policy.archived')}</p>}
        <div className="room-policy__fields">
          {policyFields.map(field => {
            const invalid = invalidFields.some(problem => problem.name === field.name)
            return <label className="field" key={field.name} htmlFor={fieldId(field.name)}>{t(field.label)}
              <input id={fieldId(field.name)} data-policy-field={field.name} aria-label={t(field.label)} type="number" inputMode="numeric"
                min={field.min} max={field.max} step="1" aria-invalid={invalid || undefined} aria-describedby={`${fieldId(field.name)}-help`}
                disabled={!active || busy} value={draft[field.name]} onChange={event => update(field.name, event.target.value)}/>
              <span id={`${fieldId(field.name)}-help`} className={`field-hint${invalid ? ' notice--danger' : ''}`}>{t(field.hint)}</span>
            </label>
          })}
        </div>
        <details className="compact-details room-policy__advanced"><summary>{t('chat.policy.advanced')}</summary>
          <p className="section-copy">{t('chat.policy.advancedHelp')}</p></details>
        {invalidFields.length > 0 && <div role="alert" className="notice notice--warning stack stack--tight"><p>{t('chat.policy.validation')}</p>
          <ul>{invalidFields.map(field => <li key={field.name}><button type="button" className="text-link" onClick={() => {
            const live = readScheduleEditor(identity, roomId)
            if (current() && active && !busy && live.draft && live.validationError) requestFieldFocus(field.name, live.draft, live.revision)
          }}>{t(field.label)} · {t(field.hint)}</button></li>)}</ul>
        </div>}
        {(phase === 'unknown' || phase === 'rejected') && <p role="alert" className="notice notice--warning">{t(phase === 'unknown' ? 'editor.unknown' : 'editor.rejected')}</p>}
        {saved && <p role="status" className="metadata status-text--success">{t('chat.policy.saved')}</p>}
        {active && <button type="submit" className="button button--secondary" disabled={busy || schedule.isFetching || !schedule.isSuccess}>
          {phase === 'saving' ? t('common.saving') : inspecting || schedule.isFetching ? t('common.loading') : phase === 'rejected' ? t('chat.policy.retrySave') : t('chat.policy.save')}
        </button>}
      </form>}
    </div>
    {review&&review.identity===identity&&review.generation===generation&&review.roomId===roomId&&<RoomPolicyRestoreDialog target={review} roomId={roomId} draft={draft} revision={editor.revision} received={receivedPolicyDraft(schedule.data)} ready={schedule.isSuccess&&!schedule.isError&&!schedule.isStale} busy={busy||schedule.isFetching} unknown={phase==='unknown'} canUse={()=>current()&&reviewRef.current===review&&review.identity===identity&&review.generation===generation&&review.roomId===roomId} onClose={closeReview} onRead={()=>{if(current())void inspect()}} onApply={()=>applySaved(review)}/>}
  </details>
}
