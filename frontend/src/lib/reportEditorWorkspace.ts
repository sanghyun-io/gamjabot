import type { Receipt, SubmissionDraft } from './reporting'
import type { MessageKey } from '../i18n/locales/en'
import { directoryViewGeneration } from './directoryViewMemory'

export type ReportForm = Readonly<{ selected: readonly string[]; description: string; revision: number }>
export type PreparedReport = Readonly<{ draft: SubmissionDraft; revision: number }>
export type ReportOutcome = Readonly<{
  phase: 'sending' | 'accepted' | 'unknown' | 'rejected'; receipt?: Receipt;
  body?: string; mismatch?: boolean; acknowledged?: boolean;
}>
export type ReportNotice = Readonly<{ target: string; message: MessageKey; receipt?: Receipt; exportPath?: string }>
export type ReportOperation = Readonly<{ token: string; kind: 'prepare' | 'submit' | 'delete' | 'export'; target: string | null }>
export type ReportEditorWorkspace = Readonly<{
  form: ReportForm; prepared: PreparedReport | null; outcomes: Readonly<Record<string, ReportOutcome>>;
  selectionUndo: Readonly<{ before: readonly string[]; after: readonly string[] }> | null;
  selectionInitialized: boolean; notice: ReportNotice | null; error: MessageKey | '';
  operation: ReportOperation | null;
}>
const initial: ReportEditorWorkspace = Object.freeze({ form: Object.freeze({ selected: Object.freeze([]), description: '', revision: 0 }),
  prepared: null, outcomes: {}, selectionUndo: null, selectionInitialized: false, notice: null, error: '', operation: null })
let current: { identity: string; value: ReportEditorWorkspace } | null = null
const listeners = new Set<() => void>()
// One current login/anonymous epoch, in memory only. Native OS-profile reports are a separate store.
export function readReportEditor(identity: string) { return current?.identity === identity ? current.value : initial }
export function subscribeReportEditor(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearReportEditor() { current = null; listeners.forEach(listener => listener()) }
export function updateReportEditor(identity: string, generation: number,
  update: (previous: ReportEditorWorkspace) => ReportEditorWorkspace) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readReportEditor(identity), next = update(previous)
  if (previous === next) return false
  current = { identity, value: Object.freeze(next) }; listeners.forEach(listener => listener()); return true
}
