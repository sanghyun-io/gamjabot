import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import HandoffSourcePicker, { type HandoffSourceChoice } from './HandoffSourcePicker'
import { assistantVisibleMarkdown } from '../lib/assistantContent'
import { ClipboardPenLine } from 'lucide-react'
import { eventsApi, type RoomEvent } from '../api/events'
import { summariesApi } from '../api/summaries'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import HandoffSourcePreview from './HandoffSourcePreview'
import ReceivedHandoffCopy from './ReceivedHandoffCopy'
import HistoryEventLink, { type HistoryEventNavigation } from './HistoryEventLink'
import { closeOtherRoomPanels } from '../lib/roomPanels'
import { roomEventCursor } from '../lib/eventCursor'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { editorTextLength } from '../lib/editorValidation'
import { editorSaveFailure } from './EditorSaveNotice'
import { handoffFingerprint, matchesHandoffReceipt, readHandoffEditor, subscribeHandoffEditors,
  updateHandoffEditor, type HandoffEditor, type HandoffRequest, type HandoffSelection } from '../lib/handoffEditorState'

function authored(event: RoomEvent, roomId: number) {
  return event.room_id === roomId && Number.isSafeInteger(event.seq) && event.seq > 0 && !!event.event_id
    && event.kind === 'message' && (event.actor_type === 'user' || event.actor_type === 'agent')
    && typeof event.payload.content === 'string'
}
function handoff(event: RoomEvent, roomId: number) {
  return event.room_id === roomId && event.actor_type === 'system' && event.kind === 'summary' && event.payload.owner_checkpoint === true
    && Number.isSafeInteger(event.payload.source_start_seq) && Number(event.payload.source_start_seq) >= 1
    && Number.isSafeInteger(event.payload.source_end_seq) && Number(event.payload.source_end_seq) >= Number(event.payload.source_start_seq)
    && typeof event.payload.content === 'string'
}
function excerpt(value: string) {
  let text = '', count = 0
  for (const point of value.replace(/\s+/g, ' ').trim()) {
    if (count++ === 72) return text + '…'
    text += point
  }
  return text
}
const endpoint = (event: RoomEvent) => Object.freeze({ seq: event.seq, eventId: event.event_id })
const freezeSelection = (value: HandoffSelection): HandoffSelection => Object.freeze({ start: Object.freeze({ ...value.start }), end: Object.freeze({ ...value.end }) })
function sourceExists(selection: HandoffSelection | null, messages: RoomEvent[]) {
  return !!selection && selection.start.seq <= selection.end.seq
    && messages.some(event => event.seq === selection.start.seq && event.event_id === selection.start.eventId)
    && messages.some(event => event.seq === selection.end.seq && event.event_id === selection.end.eventId)
}

export default function HandoffNote({ roomId, active, authEpoch, events, historyState, onSaved, onEvents, copyAllowed, onViewSource }: {
  roomId: number; active: boolean; authEpoch: number; events: RoomEvent[];
  historyState: 'loading' | 'complete' | 'error'; onSaved: (event: RoomEvent) => void; onEvents?: (batch: RoomEvent[]) => void; copyAllowed?: () => boolean; onViewSource?: HistoryEventNavigation
}) {
  const { t, formatDateTime } = useI18n()
  const ownerId = useAuthStore(state => state.user?.id || 0), identity = `${ownerId}:${authEpoch}`, generation = directoryViewGeneration()
  const editor = useSyncExternalStore(subscribeHandoffEditors, () => readHandoffEditor(identity, roomId))
  const edit = (update: (state: HandoffEditor) => HandoffEditor) => updateHandoffEditor(identity, roomId, generation, update)
  const [expanded, setExpanded] = useState(false), mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const sameOwner = () => { const auth = useAuthStore.getState(); return auth.user?.id === ownerId && auth.epoch === authEpoch && directoryViewGeneration() === generation }
  const current = () => mounted.current && sameOwner()
  const messages = useMemo(() => events.filter(event => authored(event, roomId)).sort((a, b) => a.seq - b.seq), [events, roomId])
  const view = useRef({ messages, historyState, active }); view.current = { messages, historyState, active }
  const previewReadView = useRef({ current, expanded, copyAllowed, onViewSource })
  previewReadView.current = { current, expanded, copyAllowed, onViewSource }
  const previewCanRead = useCallback(() => {
    const view = previewReadView.current
    return view.current() && view.expanded && !!view.copyAllowed?.() && typeof view.onViewSource === 'function'
  }, [])
  const previewOpenSource = useCallback<HistoryEventNavigation>((seq, eventId) => {
    const view = previewReadView.current
    if (view.current() && view.expanded && view.copyAllowed?.()) view.onViewSource?.(seq, eventId)
  }, [])
  const selectionKey = `${editor.selection?.start.eventId || ''}:${editor.selection?.start.seq || 0}:${editor.selection?.end.eventId || ''}:${editor.selection?.end.seq || 0}`
  const previewMemory = useMemo(() => ({ identity, generation, key: `handoff-source-preview:${roomId}:${selectionKey}` }), [identity, generation, roomId, selectionKey])
  const ime = useImeFormGuard(`${roomId}:${identity}:handoff`), noteField = useId()
  // Count the exact untrimmed request body, matching Python str length.
  const noteChars = useMemo(() => editorTextLength(editor.content), [editor.content])
  const noteValid = !!editor.content.trim() && noteChars <= 8000
  const noteInvalid = !noteValid && (editor.content.length > 0 || editor.validationError)
  const busy = editor.requests.some(request => request.status === 'sending') || !!editor.readingKey
  const available = historyState === 'complete' && sourceExists(editor.selection, messages)
  const selectedMessages = useMemo(() => editor.selection ? messages.filter(event => event.seq >= editor.selection!.start.seq && event.seq <= editor.selection!.end.seq) : [], [messages, editor.selection])
  // Project only received display text once per message batch, never the canonical source body.
  const sourceChoices = useMemo<HandoffSourceChoice[]>(() => messages.map(event => {
    const raw = event.payload.content as string, text = event.actor_type === 'agent' ? assistantVisibleMarkdown(raw) : raw, preview = excerpt(text)
    return { id: event.event_id, seq: event.seq, author: event.actor_name_snapshot, text, excerpt: preview,
      label: `#${event.seq} · ${t('chat.handoff.messageOption', { actor: event.actor_name_snapshot, preview, time: formatDateTime(event.created_at, { hour: '2-digit', minute: '2-digit' }) })}` }
  }), [messages, t, formatDateTime])
  const sourceChoiceMap = useMemo(() => new Map(sourceChoices.map(choice => [choice.id, choice])), [sourceChoices])
  const possibleEnds = useMemo(() => editor.selection ? sourceChoices.filter(choice => choice.seq >= editor.selection!.start.seq) : [], [sourceChoices, editor.selection?.start.seq])
  const received = editor.requests.filter(request => request.status === 'confirmed' && request.receipt).map(request => request.receipt!)
  const latest = [...events, ...received].filter(event => handoff(event, roomId)).reduce<RoomEvent | undefined>((before, event) => !before || event.seq > before.seq ? event : before, undefined)
  const latestStart = latest && messages.find(event => event.seq === latest.payload.source_start_seq)
  const latestEnd = latest && messages.find(event => event.seq === latest.payload.source_end_seq)
  const firstId = messages[0]?.event_id, lastId = messages.at(-1)?.event_id
  useEffect(() => {
    if (!expanded || historyState !== 'complete' || !messages.length || !sameOwner()) return
    edit(previous => previous.selection ? previous : { ...previous, selection: freezeSelection({ start: endpoint(messages[0]), end: endpoint(messages.at(-1)!) }) })
  }, [expanded, historyState, firstId, lastId, editor.selection])

  const patchRequest = (key: string, patch: Partial<HandoffRequest>) => edit(previous => !previous.requests.some(request => request.key === key) ? previous : {
    ...previous, requests: previous.requests.map(request => request.key === key ? Object.freeze({ ...request, ...patch }) : request),
  })
  const accept = (key: string, event: RoomEvent) => {
    if (!sameOwner()) return false
    const request = readHandoffEditor(identity, roomId).requests.find(item => item.key === key)
    if (!request) return false
    if (request.status === 'confirmed') return matchesHandoffReceipt(request, event, roomId)
    if (!matchesHandoffReceipt(request, event, roomId)) {
      if (!request.mismatch || request.receipt?.event_id !== event.event_id) patchRequest(key, { status: 'unknown', mismatch: true, receipt: event })
      return false
    }
    roomEventCursor.add(roomId, [event])
    edit(previous => {
      const unchanged = previous.revision === request.revision && previous.selection
        && handoffFingerprint(previous.selection, previous.content) === request.fingerprint
      return { ...previous, content: unchanged ? '' : previous.content, dirty: unchanged ? false : previous.dirty,
        revision: previous.revision + (unchanged ? 1 : 0), validationError: false,
        requests: previous.requests.map(item => item.key === key ? Object.freeze({ ...item, status: 'confirmed' as const, mismatch: false, receipt: event, lookup: '' as const }) : item) }
    })
    onSaved(event)
    return true
  }
  useEffect(() => {
    if (!sameOwner()) return
    for (const request of editor.requests) {
      if (request.status === 'confirmed') continue
      const found = events.find(event => event.room_id === roomId && event.idempotency_key === request.key)
      if (found) accept(request.key, found)
    }
  }, [events, editor.requests])

  const launch = async (request: HandoffRequest, snapshot: HandoffEditor, acknowledgeEarlier = false) => {
    if (!current() || !view.current.active || view.current.historyState !== 'complete'
      || !sourceExists(request.selection, view.current.messages) || request.mismatch) return
    if (!edit(previous => previous !== snapshot || previous.readingKey || previous.requests.some(item => item.status === 'sending') ? previous : {
      ...previous, validationError: false,
      requests: previous.requests.some(item => item.key === request.key)
        ? previous.requests.map(item => item.key === request.key ? Object.freeze({ ...item, status: 'sending' as const, lookup: '' as const }) : item)
        : [...previous.requests.filter(item => item.status === 'unknown' || item.mismatch).map(item => acknowledgeEarlier ? Object.freeze({ ...item, acknowledged: true }) : item), Object.freeze({ ...request, status: 'sending' as const })],
    })) return
    try {
      const result = await summariesApi.create(roomId, request.body, request.key)
      if (sameOwner()) accept(request.key, result)
    } catch (failure) {
      if (!sameOwner()) return
      const live = readHandoffEditor(identity, roomId).requests.find(item => item.key === request.key)
      if (!live || live.status === 'confirmed') return
      const code = (failure as { response?: { data?: { detail?: { code?: string } } } }).response?.data?.detail?.code
      patchRequest(request.key, { status: code === 'checkpoint_source_busy' ? 'busy' : editorSaveFailure(failure) === 'unknown' ? 'unknown' : 'rejected' })
    }
  }
  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    if (ime.isSubmitBlocked(event) || !current()) return
    const snapshot = readHandoffEditor(identity, roomId), selection = snapshot.selection
    if (!view.current.active || view.current.historyState !== 'complete' || !sourceExists(selection, view.current.messages)
      || !snapshot.content.trim() || editorTextLength(snapshot.content) > 8000) {
      edit(previous => ({ ...previous, validationError: !previous.content.trim() || previous.content.length > 8000 })); return
    }
    const fingerprint = handoffFingerprint(selection!, snapshot.content)
    const prior = [...snapshot.requests].reverse().find(request => request.fingerprint === fingerprint && request.status !== 'confirmed' && !request.mismatch)
    const acknowledgeEarlier = !prior && snapshot.requests.some(request => (request.status === 'unknown' || request.mismatch) && !request.acknowledged)
    if (acknowledgeEarlier && !window.confirm(t('chat.handoff.newRequestConfirm'))) return
    const request: HandoffRequest = prior || Object.freeze({ key: crypto.randomUUID(),
      body: Object.freeze({ source_start_seq: selection!.start.seq, source_end_seq: selection!.end.seq, content: snapshot.content }),
      selection: freezeSelection(selection!), fingerprint, revision: snapshot.revision,
      afterSeq: roomEventCursor.afterSeq(roomId), status: 'unknown' as const })
    void launch(request, snapshot, acknowledgeEarlier)
  }
  const inspect = async (key: string) => {
    if (!current()) return
    const snapshot = readHandoffEditor(identity, roomId), request = snapshot.requests.find(item => item.key === key)
    if (!request || request.status === 'confirmed' || snapshot.readingKey || snapshot.requests.some(item => item.status === 'sending')) return
    if (!edit(previous => previous !== snapshot ? previous : { ...previous, readingKey: key })) return
    patchRequest(key, { lookup: '' })
    try {
      const cached = roomEventCursor.events(roomId).find(event => event.idempotency_key === key)
      if (cached) { accept(key, cached); return }
      let after = request.afterSeq
      for (let page = 0; page < 10; page++) {
        const batch = await eventsApi.list(roomId, after, 100)
        if (!sameOwner()) return
        if (batch.some((event, index) => event.room_id !== roomId || event.seq !== after + index + 1 || !event.event_id)) throw new Error('Unexpected history page')
        roomEventCursor.add(roomId, batch); onEvents?.(batch)
        const found = batch.find(event => event.idempotency_key === key)
        if (found) { accept(key, found); return }
        if (batch.length) after = batch.at(-1)!.seq
        patchRequest(key, { afterSeq: after })
        if (batch.length < 100) break
      }
      if (sameOwner()) patchRequest(key, { lookup: 'absent' })
    } catch { if (sameOwner()) patchRequest(key, { lookup: 'failure' }) }
    finally { if (sameOwner()) edit(previous => previous.readingKey === key ? { ...previous, readingKey: null } : previous) }
  }
  const changeContent = (content: string) => { if (current()) edit(previous => ({ ...previous, content, revision: previous.revision + 1, dirty: true, validationError: false })) }
  const changeRange = (which: 'start' | 'end', id: string) => {
    if (!current()) return
    const selected = view.current.messages.find(event => event.event_id === id)
    if (!selected) return
    edit(previous => {
      if (!previous.selection) return previous
      const next = endpoint(selected)
      const selection = which === 'start' ? { start: next, end: previous.selection.end.seq >= next.seq ? previous.selection.end : next }
        : { ...previous.selection, end: next }
      return { ...previous, selection: freezeSelection(selection), revision: previous.revision + 1, dirty: true, validationError: false }
    })
  }
  const discard = () => {
    if (!current() || !window.confirm(t('chat.handoff.discardConfirm')) || !current()) return
    edit(previous => ({ ...previous, content: '', selection: null, dirty: false, revision: previous.revision + 1, validationError: false }))
  }
  const option = (event: RoomEvent) => sourceChoiceMap.get(event.event_id)?.label || `#${event.seq}`
  const requestStatus = (request: HandoffRequest) => t(request.status === 'sending' ? 'common.saving' : request.status === 'confirmed' ? 'chat.handoff.saved'
    : request.mismatch ? 'chat.handoff.receiptMismatch' : request.status === 'unknown' ? 'editor.unknown'
    : request.status === 'busy' ? 'chat.handoff.busy' : 'editor.rejected')

  return <details name="room-panel" className="handoff-note compact-details" onToggle={event => {
    if (event.target !== event.currentTarget) return
    closeOtherRoomPanels(event.currentTarget); setExpanded(event.currentTarget.open)
  }}>
    <summary><ClipboardPenLine size={16} aria-hidden="true" />{t('chat.handoff.open')}</summary>
    {expanded && <div className="handoff-note__body">
      <p className="field-hint">{t('chat.handoff.sessionLimit')}</p>
      {editor.dirty && <p role="status" className="notice notice--info">{t('chat.handoff.sessionDraft')}</p>}
      {historyState === 'loading' && <p role="status" className="metadata">{t('chat.handoff.loading')}</p>}
      {historyState === 'error' && <p role="alert" className="notice notice--warning">{t('chat.handoff.historyUnavailable')}</p>}
      <div><h2 className="section-title">{t('chat.handoff.title')}</h2><p className="section-copy">{t('chat.handoff.future')}</p></div>
      {latest && <section className="handoff-note__latest" aria-label={t(historyState === 'complete' ? 'chat.handoff.latest' : 'chat.handoff.received')}>
        <strong>{t(historyState === 'complete' ? 'chat.handoff.latest' : 'chat.handoff.received')}</strong><p>{latest.payload.content as string}</p>
        <ReceivedHandoffCopy event={latest} scope={`${identity}:${roomId}:${generation}`} canUse={()=>current()&&expanded&&!!copyAllowed?.()}/>
        <p className="metadata">{t('chat.handoff.coverage', { count: Number(latest.payload.source_end_seq) - Number(latest.payload.source_start_seq) + 1 })}</p>
        {latestStart && latestEnd && <details className="handoff-note__included compact-details"><summary>{t('chat.handoff.included')}</summary><dl>
          <dt>{t('chat.handoff.first')}</dt><dd><HistoryEventLink roomId={roomId} seq={latestStart.seq} eventId={latestStart.event_id} canUse={()=>current()&&expanded&&!!copyAllowed?.()} onNavigate={onViewSource}><span className="sr-only">{t('chat.handoff.readSource',{seq:latestStart.seq})} · </span>{option(latestStart)}</HistoryEventLink></dd>
          <dt>{t('chat.handoff.last')}</dt><dd><HistoryEventLink roomId={roomId} seq={latestEnd.seq} eventId={latestEnd.event_id} canUse={()=>current()&&expanded&&!!copyAllowed?.()} onNavigate={onViewSource}><span className="sr-only">{t('chat.handoff.readSource',{seq:latestEnd.seq})} · </span>{option(latestEnd)}</HistoryEventLink></dd></dl><p className="field-hint">{t('chat.handoff.readRangeHelp')}</p></details>}
      </section>}
      {!active && <p className="notice notice--info">{t('chat.handoff.archived')}</p>}
      {active && historyState === 'complete' && !messages.length && !editor.selection && <p>{t('chat.handoff.noMessages')}</p>}
      {active && (messages.length > 0 || editor.selection || editor.content) && <form {...ime.handlers} noValidate className="stack stack--tight" onSubmit={submit}>
        <p className="field-hint">{t('chat.handoff.sourceSearch.help')}</p>
        <div className="handoff-note__range">
          <HandoffSourcePicker label={t('chat.handoff.first')} choices={sourceChoices} selected={editor.selection?.start} disabled={historyState !== 'complete'} onChange={id => changeRange('start', id)}/>
          <HandoffSourcePicker label={t('chat.handoff.last')} choices={possibleEnds} selected={editor.selection?.end} disabled={historyState !== 'complete'} onChange={id => changeRange('end', id)}/>
        </div>
        {editor.selection && historyState === 'complete' && !available && <p role="alert" className="notice notice--warning">{t('chat.handoff.sourceMissing')}</p>}
        <label className="field" htmlFor={noteField}>{t('chat.handoff.note')}<textarea id={noteField} rows={4} value={editor.content}
          aria-invalid={noteInvalid || undefined} aria-describedby={`${noteField}-count ${noteField}-rule ${noteField}-unicode`}
          onChange={event => changeContent(event.target.value)} placeholder={t('chat.handoff.placeholder')}/>
          <span id={`${noteField}-count`} className={`metadata${noteInvalid ? ' metadata--danger' : ''}`}>{t('chat.handoff.input.count', { count: noteChars })}</span>
          <span id={`${noteField}-rule`} className={`field-hint${noteInvalid ? ' metadata--danger' : ''}`}>{t('chat.handoff.input.rule')}</span>
          <span id={`${noteField}-unicode`} className="field-hint">{t('chat.handoff.input.unicode')}</span>
        </label>
        {noteChars > 8000 && <p role="alert" className="notice notice--warning">{t('chat.handoff.input.tooLong')}</p>}
        <section className="handoff-note__preview" aria-label={t('chat.handoff.preview')}><strong>{t('chat.handoff.preview')}</strong><p className={editor.content ? '' : 'metadata'}>{editor.content || t('chat.handoff.previewEmpty')}</p>
          {available && editor.selection && <><p className="metadata">{t('chat.handoff.coverage', { count: editor.selection.end.seq - editor.selection.start.seq + 1 })}</p><HandoffSourcePreview key={`${identity}:${generation}:${roomId}:${selectionKey}`} messages={selectedMessages} choices={sourceChoiceMap} roomId={roomId} canRead={previewCanRead} onViewSource={previewOpenSource} memory={previewMemory}/></>}
        </section>
        {editor.validationError && <p role="alert" className="metadata metadata--danger">{t('chat.handoff.validation')}</p>}
        <div className="page-actions"><button type="submit" className="button button--secondary" disabled={busy || !available || !noteValid}>{t('chat.handoff.save')}</button>
          {editor.dirty && <button type="button" className="button button--secondary" onClick={discard}>{t('chat.handoff.discardDraft')}</button>}</div>
      </form>}
      {editor.requests.map(request => <section key={request.key} className="notice notice--warning stack stack--tight">
        <p role={request.status === 'unknown' || request.mismatch ? 'alert' : 'status'}>{requestStatus(request)}</p>
        <details><summary>{t('chat.handoff.requestOriginal')}</summary><pre className="technical handoff-note__request">{request.body.content}</pre><p>{t('chat.handoff.coverage', { count: request.body.source_end_seq - request.body.source_start_seq + 1 })}</p></details>
        {request.status !== 'confirmed' && request.status !== 'sending' && <div className="page-actions">
          <button type="button" className="button button--secondary" disabled={busy} onClick={() => { void inspect(request.key) }}>{t(editor.readingKey === request.key ? 'common.loading' : 'chat.handoff.lookup')}</button>
          {!request.mismatch && <button type="button" className="button button--secondary" disabled={busy || !active || historyState !== 'complete' || !sourceExists(request.selection, messages)} onClick={() => { const snapshot = readHandoffEditor(identity, roomId); const live = snapshot.requests.find(item => item.key === request.key); if (live && live.status !== 'confirmed') void launch(live, snapshot) }}>{t('chat.handoff.retryOriginal')}</button>}
        </div>}
        {request.status !== 'confirmed' && historyState === 'complete' && !sourceExists(request.selection, messages) && <p role="status">{t('chat.handoff.sourceMissing')}</p>}
        {request.lookup && <p role="status">{t(request.lookup === 'absent' ? 'chat.handoff.lookupAbsent' : 'chat.handoff.lookupFailure')}</p>}
      </section>)}
    </div>}
  </details>
}
