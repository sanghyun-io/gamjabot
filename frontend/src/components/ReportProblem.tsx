import ServerTime from './ServerTime'
import ClipboardTextFallback from './ClipboardTextFallback'
import { useClipboardText } from '../lib/useClipboardText'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useDirectoryValue } from '../lib/useDirectoryView'
import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { allowedEnvelope, allowedEvent, createManualDraft, deleteReport, exportReport,
  submitManualReport, updateManualDraft, validReceipt } from '../lib/reporting'
import type { LocalReport } from '../lib/reporting'
import { useI18n } from '../i18n'
import type { ReportingReads } from '../lib/useReportingReads'
import type { MessageKey } from '../i18n/locales/en'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { readReportEditor, subscribeReportEditor, updateReportEditor, type ReportEditorWorkspace,
  type ReportOperation, type ReportOutcome } from '../lib/reportEditorWorkspace'

const destination = 'https://gamja.top/api/gamja-bot/reports'
type SavedReportFilter = 'all' | 'attention' | 'pending' | 'accepted'
type SavedReportMode = 'all' | 'manual' | 'automatic'
function submissionError(error: unknown): MessageKey {
  if (error === 'Submission pending') return 'problem.pendingHelp'
  if (error === 'Report rejected') return 'problem.rejectedHelp'
  return 'problem.submissionUnavailable'
}

export default function ReportProblem({ identity, reads }: { identity: string; reads: ReportingReads }) {
  const { snapshot, status, snapshotPhase, statusPhase, readStatus, readSnapshot } = reads
  const [generation] = useState(directoryViewGeneration)
  const mounted = useRef(false)
  const rootRef = useRef<HTMLElement>(null), descriptionRef = useRef<HTMLTextAreaElement>(null)
  const [descriptionFocus, setDescriptionFocus] = useState<{ revision: number; origin: Element | null } | null>(null)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const sameScope = () => {
    const auth = useAuthStore.getState()
    return `${auth.user?.id ?? 0}:${auth.epoch}` === identity && directoryViewGeneration() === generation
  }
  const workspace = useSyncExternalStore(subscribeReportEditor, () => readReportEditor(identity))
  const edit = (update: (previous: ReportEditorWorkspace) => ReportEditorWorkspace) =>
    sameScope() && updateReportEditor(identity, generation, update)
  const { form, prepared, notice, error, operation } = workspace
  const exportPathCopy = useClipboardText<{ text: string; reportId: string }>(`${identity}:${generation}:report-export-path`, item => {
    const received = readReportEditor(identity).notice
    return mounted.current && sameScope() && received?.target === item.reportId && received.exportPath === item.text
  })
  const exportPathSnapshot = () => notice?.exportPath ? { text: notice.exportPath, reportId: notice.target } : null
  const busy = operation !== null
  const { t, formatDateTime } = useI18n()
  const { selected, description } = form
  const descriptionLength = Array.from(description).length, descriptionTooLong = descriptionLength > 1000
  const diagnosticListId = useId()
  const [diagnosticQuery, setDiagnosticQuery] = useDirectoryValue<string>(identity, 'reports:diagnostic-query', '')
  const savedListId = useId()
  const [savedQuery, setSavedQuery] = useDirectoryValue<string>(identity, 'reports:saved-query', '')
  const [savedFilter, setSavedFilter] = useDirectoryValue<SavedReportFilter>(identity, 'reports:saved-filter', 'all')
  const [savedMode, setSavedMode] = useDirectoryValue<SavedReportMode>(identity, 'reports:saved-mode', 'all')
  const draft = prepared?.draft, currentId = draft?.envelope.report_id
  const stale = !!prepared && prepared.revision !== form.revision
  const changeForm = (patch: Partial<Pick<typeof form, 'selected' | 'description'>>) => {
    if (!mounted.current) return
    edit(previous => ({ ...previous, selectionInitialized: true, selectionUndo: patch.selected === undefined ? previous.selectionUndo : null,
      error: previous.error === 'problem.descriptionTooLong' && patch.description !== undefined && Array.from(patch.description).length <= 1000 ? '' : previous.error, form: Object.freeze({ ...previous.form, ...patch,
      selected: Object.freeze([...(patch.selected ?? previous.form.selected)]), revision: previous.form.revision + 1 }) }))
  }
  useLayoutEffect(() => {
    if (!descriptionFocus || !mounted.current || !sameScope() || operation || document.visibilityState !== 'visible') return
    const root = rootRef.current, input = descriptionRef.current, active = document.activeElement, live = readReportEditor(identity)
    if (!root || !root.isConnected || !input || !input.getClientRects().length || !active || active !== descriptionFocus.origin || !root.contains(active)
      || live.operation || live.form.revision !== descriptionFocus.revision || Array.from(live.form.description).length <= 1000
      || input.closest('[aria-modal="true"]') !== active.closest('[aria-modal="true"]')) return
    input.focus({ preventScroll: true }); input.scrollIntoView({ block: 'nearest' })
  }, [descriptionFocus])
  const outcomeFor = (id: string): ReportOutcome | undefined => {
    const known = readReportEditor(identity).outcomes[id]
    if (known?.phase === 'accepted') return known
    const stored = snapshot?.reports.find(report => report.draft.envelope.report_id === id)
    if (stored?.state === 'accepted' && (!known?.body || known.body === stored.draft.body)) return validReceipt(stored.receipt, id)
      ? { phase: 'accepted', receipt: stored.receipt, body: stored.draft.body } : { phase: 'unknown', mismatch: true, body: stored.draft.body }
    if (known) return known
    return stored?.state === 'rejected' ? { phase: 'rejected', body: stored.draft.body } : stored?.state === 'sending' ? { phase: 'sending', body: stored.draft.body }
      : stored && stored.attempts > 0 ? { phase: 'unknown', body: stored.draft.body } : undefined
  }
  const localBodyMatches = (id: string, body: string) => snapshotPhase === 'ready' && snapshot?.reports.some(report =>
    report.draft.envelope.report_id === id && report.draft.envelope.mode === 'manual' && report.draft.body === body)
  const begin = (kind: ReportOperation['kind'], target: string | null, acknowledgeEarlier = false): ReportOperation | null => {
    if (!mounted.current || !sameScope()) return null
    const next = Object.freeze({ token: crypto.randomUUID(), kind, target })
    return edit(previous => previous.operation ? previous : { ...previous, operation: next, error: '', notice: null,
      outcomes: acknowledgeEarlier ? Object.fromEntries(Object.entries(previous.outcomes).map(([id, value]) => [id,
        id !== target && value.phase === 'unknown' ? Object.freeze({ ...value, acknowledged: true }) : value])) : previous.outcomes }) ? next : null
  }
  const during = (sent: ReportOperation, update: (previous: ReportEditorWorkspace) => ReportEditorWorkspace) =>
    edit(previous => previous.operation?.token === sent.token ? update(previous) : previous)
  const finish = (sent: ReportOperation) => during(sent, previous => ({ ...previous, operation: null }))
  const lastOperation = useRef(operation?.token ?? null)
  useEffect(() => {
    if (!operation && lastOperation.current && sameScope()) void readSnapshot(true)
    lastOperation.current = operation?.token ?? null
  }, [operation?.token, readSnapshot])

  useEffect(() => {
    if (!snapshot || snapshotPhase !== 'ready') return
    edit(previous => previous.selectionInitialized ? previous : { ...previous, selectionInitialized: true,
      form: Object.freeze({ ...previous.form, selected: Object.freeze(snapshot.events.slice(0, 16).map(event => event.event_id)), revision: previous.form.revision + 1 }) })
    // A qualified native receipt for the exact retained body can confirm a prior uncertain attempt.
    edit(previous => {
      let changed = false
      const outcomes = { ...previous.outcomes }
      for (const report of snapshot.reports) {
        const id = report.draft.envelope.report_id, known = outcomes[id]
        if (!known || known.phase === 'accepted' || report.state !== 'accepted' || !validReceipt(report.receipt, id)
          || known.body && known.body !== report.draft.body) continue
        changed = true; outcomes[id] = Object.freeze({ ...known, phase: 'accepted', receipt: report.receipt, body: report.draft.body })
      }
      return changed ? { ...previous, outcomes } : previous
    })
  }, [snapshot, snapshotPhase])

  function changeSelection(eventId: string) {
    const current = readReportEditor(identity).form.selected
    if (!current.includes(eventId) && current.length >= 16) return
    changeForm({ selected: current.includes(eventId) ? current.filter(id => id !== eventId) : [...current, eventId] })
  }

  async function preview() {
    const before = readReportEditor(identity), sent = before.form
    if (snapshotPhase !== 'ready' || !snapshot || sent.selected.some(id => !snapshot.events.some(event => event.event_id === id))) return
    if (Array.from(sent.description).length > 1000) {
      if (mounted.current && sameScope() && !before.operation && edit(previous => previous !== before ? previous : ({ ...previous, error: 'problem.descriptionTooLong' }))) {
        setDescriptionFocus({ revision: sent.revision, origin: document.activeElement })
      }
      return
    }
    const request = begin('prepare', before.prepared?.draft.envelope.report_id ?? null)
    if (!request) return
    const prior = before.prepared?.draft, priorId = prior?.envelope.report_id
    const stored = snapshot.reports.find(report => report.draft.envelope.report_id === priorId)
    const mayUpdate = priorId && prior && localBodyMatches(priorId, prior.body) && !outcomeFor(priorId) && stored?.attempts === 0
    try {
      const next = mayUpdate
        ? await updateManualDraft(priorId!, [...sent.selected], sent.description || undefined)
        : await createManualDraft([...sent.selected], sent.description || undefined)
      during(request, previous => ({ ...previous, prepared: Object.freeze({ draft: Object.freeze(next), revision: sent.revision }) }))
    } catch (failure) {
      during(request, previous => ({ ...previous, error: String(failure).includes('wire limit') ? 'problem.reportTooLarge' : 'problem.prepareFailure' }))
    } finally { finish(request) }
  }

  async function submit(reportId: string, shownBody: string, retry = false) {
    if (!sameScope() || statusPhase !== 'ready' || !status?.submission_available) return
    const previous = outcomeFor(reportId), body = previous?.body ?? shownBody
    if (previous?.phase === 'accepted' || previous?.phase === 'sending' || previous?.phase === 'rejected' || previous?.mismatch) return
    if (!localBodyMatches(reportId, body)) {
      edit(value => ({ ...value, notice: { target: reportId, message: 'problem.localDraftUnconfirmed' } })); return
    }
    const current = readReportEditor(identity)
    if (!retry && current.prepared?.draft.envelope.report_id === reportId && current.prepared.revision !== current.form.revision) return
    const acknowledgeEarlier = Object.entries(current.outcomes).some(([id, value]) => id !== reportId && value.phase === 'unknown' && !value.acknowledged)
    if (acknowledgeEarlier && !window.confirm(t('problem.newSubmissionConfirm'))) return
    const request = begin('submit', reportId, acknowledgeEarlier)
    if (!request) return
    during(request, value => ({ ...value, outcomes: { ...value.outcomes, [reportId]: Object.freeze({ phase: 'sending' as const, body }) } }))
    try {
      const result = await submitManualReport(reportId)
      during(request, value => {
        if (value.outcomes[reportId]?.phase === 'accepted') return value
        if (!validReceipt(result, reportId)) return { ...value, outcomes: { ...value.outcomes, [reportId]: Object.freeze({ phase: 'unknown' as const, mismatch: true, body }) },
          notice: { target: reportId, message: 'problem.receiptMismatch' } }
        return { ...value, outcomes: { ...value.outcomes, [reportId]: Object.freeze({ phase: 'accepted' as const, receipt: result, body }) },
          notice: { target: reportId, message: 'problem.submitted', receipt: result } }
      })
    } catch (failure) {
      during(request, value => value.outcomes[reportId]?.phase === 'accepted' ? value : ({ ...value,
        outcomes: { ...value.outcomes, [reportId]: Object.freeze({ phase: failure === 'Report rejected' ? 'rejected' as const : 'unknown' as const, body }) },
        notice: { target: reportId, message: submissionError(failure) } }))
    } finally { finish(request) }
  }

  async function remove(reportId: string) {
    const before = readReportEditor(identity), retained = snapshot?.reports.find(report => report.draft.envelope.report_id === reportId)
    if (!mounted.current || !sameScope() || before.operation || snapshotPhase !== 'ready' || !snapshot || !retained
      || retained.state === 'sending' || outcomeFor(reportId)?.phase === 'sending') return
    const message = snapshot.events.some(event => event.event_id === reportId) ? 'problem.stored.deleteWithDiagnosticConfirm' : 'problem.stored.deleteConfirm'
    if (!window.confirm(t(message, { id: reportId })) || !mounted.current || !sameScope()) return
    const request = begin('delete', reportId)
    if (!request) return
    try {
      await deleteReport(reportId)
      during(request, value => ({ ...value, prepared: value.prepared?.draft.envelope.report_id === reportId ? null : value.prepared,
        notice: { target: reportId, message: 'problem.deleted' } }))
    } catch { during(request, value => ({ ...value, error: 'problem.deleteFailure' })) }
    finally { finish(request) }
  }

  async function exportLocal(reportId: string) {
    const request = begin('export', reportId)
    if (!request) return
    try {
      const path = await exportReport(reportId)
      during(request, value => ({ ...value, notice: { target: reportId, message: 'problem.export', exportPath: path } }))
    } catch { during(request, value => ({ ...value, error: 'problem.exportFailure' })) }
    finally { finish(request) }
  }

  const resetForm = () => {
    const before = readReportEditor(identity)
    if (!mounted.current || before.operation || !window.confirm(t('problem.clearSessionConfirm'))) return
    edit(value => value !== before || value.operation ? value : { ...value, selectionInitialized: true, selectionUndo: null,
      form: Object.freeze({ selected: Object.freeze([]), description: '', revision: value.form.revision + 1 }), prepared: null, error: '' })
  }
  const previewPresent = !!draft && !!localBodyMatches(draft.envelope.report_id, draft.body)
  const saved = snapshot?.reports.filter(report => report.draft.envelope.report_id !== currentId || !previewPresent) ?? []
  const savedSummary = (report: LocalReport) => {
    const id = report.draft.envelope.report_id, outcome = outcomeFor(id)
    const accepted = validReceipt(outcome?.receipt, id) ? outcome.receipt : null
    const sending = report.state === 'sending' || outcome?.phase === 'sending'
    const label: MessageKey = accepted ? 'problem.submitted' : outcome?.mismatch ? 'problem.receiptMismatch'
      : outcome?.phase === 'rejected' ? 'problem.rejected' : outcome?.phase === 'unknown' ? 'problem.stored.unknown'
        : sending ? 'problem.stored.sending' : 'problem.pending'
    const category: SavedReportFilter = accepted ? 'accepted'
      : outcome?.mismatch || outcome?.phase === 'rejected' || outcome?.phase === 'unknown' || sending ? 'attention' : 'pending'
    return { outcome, accepted, sending, category, label }
  }
  const matchesSaved = directoryMatcher(savedQuery)
  const savedRows = saved.map(report => {
    const envelope = report.draft.envelope, summary = savedSummary(report), id = envelope.report_id
    // Only metadata already displayed on the card. Never search descriptions,
    // nested diagnostics or the original report body/transport payload.
    const matches = (savedFilter === 'all' || savedFilter === summary.category)
      && (savedMode === 'all' || savedMode === envelope.mode) && matchesSaved([
        id, envelope.component, envelope.stage, envelope.code, envelope.occurred_at,
        envelope.mode, t(envelope.mode === 'manual' ? 'problem.stored.manual' : 'problem.stored.automatic'),
        t(summary.label, summary.accepted ? { receipt: summary.accepted.receipt_id } : {}),
        summary.accepted?.receipt_id, summary.accepted?.accepted_at,
      ])
    // Reading filters must not hide uncertain/rejected/sending reports or the
    // current operation/result. Preserve the native received order throughout.
    const pinned = summary.category === 'attention' || operation?.target === id || notice?.target === id
    return { report, summary, matches, pinned }
  })
  const matchingSavedCount = savedRows.filter(row => row.matches).length
  const visibleSaved = savedRows.filter(row => row.matches || row.pinned)
  const pinnedSavedCount = visibleSaved.filter(row => !row.matches).length
  const savedFiltered = !!savedQuery || savedFilter !== 'all' || savedMode !== 'all'
  const showAllSaved = () => {
    if (!mounted.current || !sameScope()) return
    setSavedQuery(''); setSavedFilter('all'); setSavedMode('all')
  }
  const currentOutcome = currentId ? outcomeFor(currentId) : undefined
  const receipt = currentId && validReceipt(currentOutcome?.receipt, currentId) ? currentOutcome.receipt : null
  const missingSelection = !!snapshot && selected.some(id => !snapshot.events.some(event => event.event_id === id))
  const matchesDiagnostic = directoryMatcher(diagnosticQuery)
  const diagnosticRows = (snapshot?.events || []).map((event, index) => ({ event, index }))
  // Search displayed envelope metadata only, never nested diagnostics, frames,
  // request identifiers, description text or any report body.
  const matchingDiagnostics = new Set(diagnosticRows.filter(({ event }) => matchesDiagnostic([
    event.event_id, event.component, event.stage, event.code, event.occurred_at,
    event.app_version, event.build_id, event.os, event.os_version, event.arch,
  ])).map(({ event }) => event.event_id))
  const visibleDiagnostics = diagnosticRows.filter(({ event }) => matchingDiagnostics.has(event.event_id) || selected.includes(event.event_id))
  const pinnedDiagnostics = visibleDiagnostics.length - matchingDiagnostics.size
  const matchingIds = diagnosticRows.filter(({ event }) => matchingDiagnostics.has(event.event_id)).map(({ event }) => event.event_id)
  const sameSelection = (left: readonly string[], right: readonly string[]) => left.length === right.length && left.every(id => right.includes(id))
  const sameOrderedSelection = (left: readonly string[], right: readonly string[]) => left.length === right.length && left.every((id, index) => id === right[index])
  const canUndoSelection = !!workspace.selectionUndo && sameOrderedSelection(selected, workspace.selectionUndo.after)
  const replaceSelection = (next: readonly string[]) => {
    const before = readReportEditor(identity)
    if (!mounted.current || !sameScope() || before.operation || next.length > 16 || sameSelection(before.form.selected, next)) return
    const ids = Object.freeze([...next])
    edit(previous => previous !== before || previous.operation ? previous : { ...previous, selectionInitialized: true,
      selectionUndo: Object.freeze({ before: Object.freeze([...previous.form.selected]), after: ids }),
      form: Object.freeze({ ...previous.form, selected: ids, revision: previous.form.revision + 1 }) })
  }
  const chooseMatching = () => {
    if (snapshotPhase !== 'ready' || !snapshot || matchingIds.length === 0 || matchingIds.length > 16) return
    replaceSelection(matchingIds)
  }
  const undoSelection = () => {
    const before = readReportEditor(identity), undo = before.selectionUndo
    if (!mounted.current || !sameScope() || before.operation || !undo || !sameOrderedSelection(before.form.selected, undo.after)) return
    // Restore only IDs in their original order. Keep any newer description and
    // the prepared/original report/unknown outcomes; a new preview is explicit.
    edit(previous => previous !== before || previous.operation ? previous : { ...previous, selectionUndo: null,
      form: Object.freeze({ ...previous.form, selected: Object.freeze([...undo.before]), revision: previous.form.revision + 1 }) })
  }



  function reportActions({ report, summary, matches }: typeof savedRows[number]) {
    const envelope = report.draft.envelope, id = envelope.report_id
    const { outcome, accepted, sending, label } = summary
    const attempted = report.attempts > 0 || outcome?.phase === 'unknown'
    const statusLabel = t(label, accepted ? { receipt: accepted.receipt_id } : {})
    return <article key={id} className="surface stack stack--tight" style={{ overflowWrap: 'anywhere' }}>
      <h3 className="section-title">{statusLabel}</h3>
      {!matches && <p className="metadata">{t('problem.savedFind.pinnedRow')}</p>}
      <p className="metadata">{t('problem.stored.receivedState')} · {t(envelope.mode === 'manual' ? 'problem.stored.manual' : 'problem.stored.automatic')} · {envelope.component} / {envelope.stage} / {envelope.code}</p>
      <p className="metadata">{t('problem.operationTarget', { id })}</p>
      <p className="metadata">{t('problem.stored.occurred')} · <ServerTime value={envelope.occurred_at}/></p>
      <p className="metadata">{t('problem.stored.attempts', { count: report.attempts })}</p>
      {accepted && <p className="metadata">{t('problem.stored.acceptedAt')} · <ServerTime value={accepted.accepted_at}/></p>}
      <details className="compact-details"><summary>{t('problem.stored.full')}</summary>
        <pre className="technical">{JSON.stringify(allowedEnvelope(envelope), null, 2)}</pre>
        {!accepted && !outcome?.mismatch && outcome?.phase !== 'rejected' && report.state === 'pending' && envelope.mode === 'manual' && <button type="button" aria-label={`${t(attempted ? 'problem.retryOriginal' : 'problem.submit')} · ${id}`} disabled={statusPhase !== 'ready' || !status?.submission_available || busy || sending} onClick={() => void submit(id, report.draft.body, attempted)} className="button button--primary">{t(attempted ? 'problem.retryOriginal' : 'problem.submit')}</button>}
      </details>
      <p className="field-hint">{t('problem.stored.localDelete')}</p>
      {sending && <p role="status" className="field-hint">{t('problem.stored.deleteSending')}</p>}
      <div className="page-actions"><button type="button" disabled={busy} aria-label={`${t('problem.export')} · ${id}`} onClick={() => void exportLocal(id)} className="button button--secondary">{t('problem.export')}</button>
        <button type="button" disabled={busy || sending || snapshotPhase !== 'ready'} aria-label={`${t('problem.delete')} · ${id}`} onClick={() => void remove(id)} className="button button--danger">{t('problem.delete')}</button></div>
    </article>
  }

  return <section ref={rootRef} className="stack">
    <div className="stack stack--tight"><h1 className="page-title">{t('problem.title')}</h1>
      <p>{t('problem.destination', { destination })} {t(statusPhase !== 'ready' ? 'report.readStatusUnknown' : status?.submission_available ? 'problem.directAvailable' : 'problem.directUnavailable')}</p>
      <p>{t('problem.privacy')}</p></div>
    <section className="stack stack--tight" aria-label={t('problem.compose.title')}>
      <h2 className="section-title">{t('problem.compose.title')}</h2>
      <label htmlFor="report-description" className="field">{t('problem.description')}
        <textarea ref={descriptionRef} id="report-description" value={description} onChange={event => changeForm({ description: event.target.value })} rows={4}
          aria-invalid={descriptionTooLong || undefined} aria-describedby={`report-description-count${descriptionTooLong ? ' report-description-limit' : ''}`}/>
      </label>
      <p id="report-description-count" className="field-hint">{t('problem.characters', { count: descriptionLength })}</p>
      {descriptionTooLong && <p id="report-description-limit" className="field-hint notice--danger">{t('problem.descriptionTooLong')}</p>}
      <p className="metadata">{t('problem.sessionContinuity')}</p>
    </section>

    {error && <p role="alert" className="notice notice--danger">{t(error)}</p>}
    {notice && <div role="status" className="notice notice--info"><p>{t('problem.operationTarget', { id: notice.target })}</p>
      <p>{t(notice.message, notice.receipt ? { receipt: notice.receipt.receipt_id } : {})}{notice.exportPath && ` · ${notice.exportPath}`}</p>
      {notice.exportPath && <div className="stack stack--tight">
        <div className="page-actions">
          <button type="button" className="button button--secondary button--compact" disabled={exportPathCopy.busy} onClick={() => { const value = exportPathSnapshot(); if (value) void exportPathCopy.copy(value) }}>{t(exportPathCopy.busy ? 'common.loading' : 'projects.fileExport.copyPath')}</button>
          <button type="button" className="text-link" onClick={() => { const value = exportPathSnapshot(); if (value) exportPathCopy.openManual(value) }}>{t('clipboard.path.select')}</button>
        </div>
        {exportPathCopy.result && <p role={exportPathCopy.result.status === 'copied' ? 'status' : 'alert'}>{t(exportPathCopy.result.status === 'copied' ? 'projects.fileExport.pathCopied' : 'projects.fileExport.copyFailed')}</p>}
        {exportPathCopy.manual && <ClipboardTextFallback rows={3} text={exportPathCopy.manual.text} label={t('clipboard.path.label')} caption={t('clipboard.path.help')} canUse={exportPathCopy.manualAllowed} onClose={exportPathCopy.closeManualAndRestore} onInteract={exportPathCopy.touchManual} busy={exportPathCopy.busy} onRetry={() => { if (exportPathCopy.manual) void exportPathCopy.copy(exportPathCopy.manual) }}/>}
      </div>}
    </div>}
    {operation?.kind === 'prepare' && <p role="status">{t('problem.preparing')}</p>}
    {operation && operation.kind !== 'prepare' && <p role="status">{t('problem.sessionOperation')} {operation.target && t('problem.operationTarget', { id: operation.target })}</p>}
    <div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={busy || statusPhase === 'loading'} onClick={() => void readStatus()}>{t('report.refreshStatus')}</button>
      <button type="button" className="button button--secondary button--compact" disabled={busy || snapshotPhase === 'loading'} onClick={() => void readSnapshot()}>{t('report.refreshRecords')}</button></div>
    {snapshotPhase === 'loading' && <p role="status">{t('problem.loading')}</p>}
    {snapshotPhase === 'error' && <p role="alert">{t('report.recordsUnavailable')}</p>}
    {snapshot && snapshotPhase !== 'ready' && <p className="metadata">{t('report.lastRecords')}</p>}
    {missingSelection && snapshot && <div className="notice notice--warning stack stack--tight"><p role="status">{t('problem.missingSelection')}</p>
      <button type="button" className="button button--secondary button--compact" disabled={busy || snapshotPhase !== 'ready'} onClick={() => changeForm({ selected: readReportEditor(identity).form.selected.filter(id => snapshot.events.some(event => event.event_id === id)) })}>{t('problem.removeMissingSelection')}</button></div>}
    <details className="surface compact-details">
      <summary>{t('problem.compose.diagnostics', { count: selected.length })}</summary>
      <div className="stack stack--tight">
    <section className="stack stack--tight" aria-label={t('problem.selection.title')}>
      <h3 className="section-title">{t('problem.selection.title')}</h3>
      <DirectorySearch value={diagnosticQuery} onChange={value => { if (mounted.current && sameScope()) setDiagnosticQuery(value) }}
        label={t('problem.find.title')} hint={t('problem.find.placeholder')} shown={matchingDiagnostics.size} total={snapshot?.events.length || 0}
        known={!!snapshot} loading={snapshotPhase === 'loading'} controls={snapshot ? diagnosticListId : undefined}/>
      <p className="field-hint">{t('problem.find.scope')}</p>
      <p role="status" className="metadata">{t('problem.selectedCount', { count: selected.length })}</p>
      <p className="field-hint">{t('problem.selection.help')}</p>
      <div className="page-actions">
        <button type="button" className="button button--secondary button--compact" disabled={busy || selected.length === 0} onClick={() => replaceSelection([])}>{t('problem.selection.clear')}</button>
        <button type="button" className="button button--secondary button--compact" disabled={busy || snapshotPhase !== 'ready' || !snapshot || matchingIds.length === 0 || matchingIds.length > 16 || sameSelection(selected, matchingIds)} onClick={chooseMatching}>{t('problem.selection.match', { count: matchingIds.length })}</button>
        <button type="button" className="text-link" disabled={busy || !canUndoSelection} onClick={undoSelection}>{t('problem.selection.undo')}</button>
      </div>
      {selected.length >= 16 && <p role="status" className="notice notice--info">{t('problem.maxEntries')}</p>}
      {snapshot && matchingIds.length > 16 && <p className="field-hint">{t('problem.selection.tooMany', { count: matchingIds.length })}</p>}
    </section>
    {snapshot && <div className="stack stack--tight"><h3 className="section-title">{t('problem.localEntries')}</h3>
        {pinnedDiagnostics > 0 && <p role="status" className="notice notice--info">{t('problem.find.pinned', { count: pinnedDiagnostics })}</p>}
        {snapshot.events.length > 0 && diagnosticQuery.trim() && matchingDiagnostics.size === 0 && <p role="status">{t('directory.noMatches')}</p>}

        {snapshot.events.length === 0 && <p>{t('problem.noEntries')}</p>}
        <div id={diagnosticListId} className="stack stack--tight">{visibleDiagnostics.map(({ event, index }) => { const label = `${index + 1}: ${event.component} / ${event.stage} / ${event.code} at ${event.occurred_at}`; return <div key={event.event_id} className="surface stack stack--tight" style={{ overflowWrap: 'anywhere' }}>
          <label className="check-row"><input type="checkbox" checked={selected.includes(event.event_id)} disabled={!selected.includes(event.event_id) && selected.length >= 16} onChange={() => changeSelection(event.event_id)} /> {t('problem.include')} {label}</label>
          <button type="button" aria-label={`${t('problem.removeDiagnostic')} ${label}`} onClick={() => changeSelection(event.event_id)} disabled={!selected.includes(event.event_id)} className="text-link">{t('problem.removeDiagnostic')} {index + 1}</button>
          {!matchingDiagnostics.has(event.event_id) && <p className="metadata">{t('problem.find.pinnedRow')}</p>}
          <details className="compact-details"><summary>{t('common.technicalDetails')}</summary><pre className="technical">{JSON.stringify(allowedEvent(event), null, 2)}</pre></details>
        </div> })}</div>
        {snapshot.events.length > 16 && selected.length < 16 && <p>{t('problem.maxEntries')}</p>}
      </div>}
      </div>
    </details>
    <p className="field-hint">{t('problem.compose.diagnosticHelp')}</p>
      <p className="metadata">{t('problem.selectedCount', { count: selected.length })}</p>
      <button type="button" className="button button--secondary button--compact" disabled={busy} onClick={resetForm}>{t('problem.selection.resetAll')}</button>
      <button type="button" disabled={busy || snapshotPhase !== 'ready' || missingSelection} onClick={() => void preview()} className="button button--primary">{t('problem.preview')}</button>
      {draft && <div className="surface stack stack--tight">
        <h2 className="section-title">{t('problem.fullPreview')}</h2><p className="metadata">{t('problem.operationTarget', { id: draft.envelope.report_id })}</p>
        <pre className="technical">{JSON.stringify(allowedEnvelope(draft.envelope), null, 2)}</pre>
        {!previewPresent && <p role="status">{t('problem.localDraftUnconfirmed')}</p>}
        {stale && <p role="status">{t('problem.previewChanged')}</p>}
        {receipt && <p>{t('problem.submitted', { receipt: receipt.receipt_id })} · {formatDateTime(receipt.accepted_at)}</p>}
        {currentOutcome && <p>{t('problem.sentOriginal')}</p>}
        {!receipt && <>
          {currentOutcome ? <p>{t(currentOutcome.mismatch ? 'problem.receiptMismatch' : currentOutcome.phase === 'rejected' ? 'problem.rejectedNoRetry' : 'problem.pendingNoAuto')}</p>
            : <button type="button" disabled={busy || stale || !previewPresent || statusPhase !== 'ready' || !status?.submission_available} onClick={() => void submit(draft.envelope.report_id, draft.body)} className="button button--primary">{t('problem.submit')}</button>}
          {currentOutcome?.phase === 'unknown' && !currentOutcome.mismatch && <button type="button" disabled={busy || !previewPresent || statusPhase !== 'ready' || !status?.submission_available} onClick={() => void submit(draft.envelope.report_id, draft.body, true)} className="button button--secondary">{t('problem.retryOriginal')}</button>}
        </>}
        <button type="button" disabled={busy || !previewPresent} onClick={() => void exportLocal(draft.envelope.report_id)} className="button button--secondary">{t('problem.export')}</button>
        <p className="field-hint">{t('problem.stored.localDelete')}</p>
        {(currentOutcome?.phase === 'sending' || snapshot?.reports.some(report => report.draft.envelope.report_id === draft.envelope.report_id && report.state === 'sending')) && <p className="field-hint">{t('problem.stored.deleteSending')}</p>}
        <button type="button" disabled={busy || !previewPresent || currentOutcome?.phase === 'sending' || snapshot?.reports.some(report => report.draft.envelope.report_id === draft.envelope.report_id && report.state === 'sending')} aria-label={`${t('problem.delete')} · ${draft.envelope.report_id}`} onClick={() => void remove(draft.envelope.report_id)} className="button button--danger">{t('problem.delete')}</button>
      </div>}
      {saved.length > 0 && <section className="stack" aria-label={t('problem.saved')}>
        <h2 className="section-title">{t('problem.saved')}</h2>
        <DirectorySearch value={savedQuery} onChange={value => { if (mounted.current && sameScope()) setSavedQuery(value) }}
          label={t('problem.savedFind.title')} hint={t('problem.savedFind.placeholder')} shown={matchingSavedCount} total={saved.length}
          known={!!snapshot} loading={snapshotPhase === 'loading'} controls={savedListId}/>
        <div className="page-actions">
          <label className="field">{t('problem.savedFind.state')}
            <select value={savedFilter} aria-controls={savedListId} onChange={event => { if (mounted.current && sameScope()) setSavedFilter(event.target.value as SavedReportFilter) }}>
              <option value="all">{t('problem.savedFind.all')}</option>
              <option value="attention">{t('problem.savedFind.attention')}</option>
              <option value="pending">{t('problem.pending')}</option>
              <option value="accepted">{t('problem.savedFind.accepted')}</option>
            </select>
          </label>
          <label className="field">{t('problem.savedFind.mode')}
            <select value={savedMode} aria-controls={savedListId} onChange={event => { if (mounted.current && sameScope()) setSavedMode(event.target.value as SavedReportMode) }}>
              <option value="all">{t('problem.savedFind.all')}</option>
              <option value="manual">{t('problem.stored.manual')}</option>
              <option value="automatic">{t('problem.stored.automatic')}</option>
            </select>
          </label>
          {savedFiltered && <button type="button" className="button button--secondary button--compact" onClick={showAllSaved}>{t('problem.savedFind.reset')}</button>}
        </div>
        <p className="field-hint">{t('problem.savedFind.scope')}</p>
        {pinnedSavedCount > 0 && <p role="status" className="notice notice--info">{t('problem.savedFind.pinned', { count: pinnedSavedCount })}</p>}
        {matchingSavedCount === 0 && <p role="status">{t('directory.noMatches')}</p>}
        <div id={savedListId} className="stack">{visibleSaved.map(reportActions)}</div>
      </section>}
  </section>
}
