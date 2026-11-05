import { invoke } from '@tauri-apps/api/core'

export type Component = 'host' | 'backend' | 'frontend' | 'native'
export type Stage = 'startup' | 'ready' | 'runtime' | 'tool' | 'render'
export type Code = 'sidecar_missing' | 'sidecar_spawn_failed' | 'backend_init_failed' | 'backend_not_ready' | 'sidecar_exited' | 'backend_runtime_failed' | 'frontend_render_failed' | 'native_action_failed' | 'unexpected_failure'

export interface ReportDiagnostic {
  version: 1
  correlation_id: string
  occurred_at: string
  stage: string
  failure_code: string
  reason: string
  error_class: string
  room_id: number | null
  agent_id: number | null
  connection_id: number | null
  trigger_event_id: string | null
  action_id: string | null
  exception_chain: { module: string; class: string }[]
  frames: { file: string; function: string; line: number }[]
  http_status?: number
  vendor_request_id?: string
  os_status?: number
  operation?: string
  cleanup?: string
}

export interface DiagnosticEvent {
  event_id: string
  occurred_at: string
  component: Component
  stage: Stage
  code: Code
  app_version: string
  build_id: string
  os: 'macos' | 'windows'
  os_version: string
  arch: 'aarch64' | 'x86_64'
  diagnostic?: ReportDiagnostic
}

export interface ReportEnvelope {
  schema_version: 1
  report_id: string
  mode: 'manual' | 'automatic'
  app_version: string
  build_id: string
  os: 'macos' | 'windows'
  os_version: string
  arch: 'aarch64' | 'x86_64'
  occurred_at: string
  component: Component
  stage: Stage
  code: Code
  occurrences: number
  transitions: { stage: Stage; code: Code }[]
  diagnostics?: ReportDiagnostic[]
  description?: string
}

export interface SubmissionDraft { envelope: ReportEnvelope; body: string }
export interface Receipt { report_id: string; receipt_id: string; accepted_at: string }
export interface LocalReport {
  draft: SubmissionDraft
  state: 'pending' | 'sending' | 'accepted' | 'rejected'
  attempts: number
  attempt_times: number[]
  next_attempt_at: number
  receipt: Receipt | null
}
export interface ReportSnapshot {
  events: DiagnosticEvent[]
  reports: LocalReport[]
  auto_reporting_enabled: boolean
  consent_generation: number
  discarded: number
  discarded_expired: number
  discarded_capacity: number
  auto_attempt_times: number[]
}
export interface ReportStatus {
  auto_reporting_enabled: boolean
  consent_generation: number
  submission_available: boolean
}

// Native adapter selects core control or the retained legacy host for this platform.
const reportControl = <T>(request: Record<string, unknown>) => invoke<T>('report_control', { request })
export const listReports = () => reportControl<ReportSnapshot>({ operation: 'report_list' })
export const getReportStatus = () => reportControl<ReportStatus>({ operation: 'report_status' })
export const createManualDraft = (selectedEventIds: string[], description?: string) =>
  reportControl<SubmissionDraft>({ operation: 'report_create', event_ids: selectedEventIds, description: description ?? null })
export const updateManualDraft = (reportId: string, selectedEventIds: string[], description?: string) =>
  reportControl<SubmissionDraft>({ operation: 'report_update', report_id: reportId, event_ids: selectedEventIds, description: description ?? null })
export const submitManualReport = (reportId: string) => reportControl<Receipt>({ operation: 'report_submit', report_id: reportId })
export const deleteReport = (reportId: string) => reportControl<void>({ operation: 'report_delete', report_id: reportId })
export const exportReport = (reportId: string) => reportControl<string>({ operation: 'report_export', report_id: reportId })
export const setAutoReporting = (enabled: boolean) => reportControl<[boolean, number]>({ operation: 'report_consent', enabled })
export const recordFrontendRenderFailure = () => reportControl<void>({ operation: 'report_frontend_failure' })

export function allowedEnvelope(envelope: ReportEnvelope): ReportEnvelope {
  const { schema_version, report_id, mode, app_version, build_id, os, os_version, arch,
    occurred_at, component, stage, code, occurrences, transitions, description, diagnostics } = envelope
  return { schema_version, report_id, mode, app_version, build_id, os, os_version, arch,
    occurred_at, component, stage, code, occurrences, transitions: transitions.map(({ stage, code }) => ({ stage, code })),
    ...(diagnostics?.length ? { diagnostics: diagnostics.map(allowedDiagnostic) } : {}),
    ...(description === undefined ? {} : { description }) }
}

export function allowedEvent(event: DiagnosticEvent): DiagnosticEvent {
  const { event_id, occurred_at, component, stage, code, app_version, build_id, os, os_version, arch, diagnostic } = event
  return { event_id, occurred_at, component, stage, code, app_version, build_id, os, os_version, arch,
    ...(diagnostic ? { diagnostic: allowedDiagnostic(diagnostic) } : {}) }
}

function allowedDiagnostic(detail: ReportDiagnostic): ReportDiagnostic {
  const { version, correlation_id, occurred_at, stage, failure_code, reason, error_class, room_id, agent_id, connection_id,
    trigger_event_id, action_id, exception_chain, frames, http_status, vendor_request_id, os_status, operation, cleanup } = detail
  return { version, correlation_id, occurred_at, stage, failure_code, reason, error_class, room_id, agent_id, connection_id,
    trigger_event_id, action_id,
    exception_chain: exception_chain.map(({ module, class: className }) => ({ module, class: className })),
    frames: frames.map(({ file, function: functionName, line }) => ({ file, function: functionName, line })),
    ...(http_status === undefined ? {} : { http_status }),
    ...(vendor_request_id === undefined ? {} : { vendor_request_id }),
    ...(os_status === undefined ? {} : { os_status }),
    ...(operation === undefined ? {} : { operation }),
    ...(cleanup === undefined ? {} : { cleanup }) }
}

export function validReceipt(value: Receipt | null | undefined, reportId: string): value is Receipt {
  return !!value && value.report_id === reportId
    && typeof value.receipt_id === 'string' && /^[0-9a-f]{32}$/i.test(value.receipt_id)
    && typeof value.accepted_at === 'string' && value.accepted_at.endsWith('Z')
    && !Number.isNaN(Date.parse(value.accepted_at))
}
