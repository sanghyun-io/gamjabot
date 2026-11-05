import { describe, expect, it, vi } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import { createManualDraft, updateManualDraft, listReports, getReportStatus, setAutoReporting, submitManualReport, exportReport, deleteReport, recordFrontendRenderFailure, allowedEnvelope, allowedEvent } from './reporting'
import type { ReportDiagnostic } from './reporting'
vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn().mockResolvedValue(null) }))

describe('reporting native core adapter', () => {
  it('sends typed operations through one adapter without reading state or requesting a UI session', async () => {
    vi.mocked(invoke).mockClear()
    await listReports(); await getReportStatus()
    await createManualDraft([], 'description only')
    await updateManualDraft('a'.repeat(32), ['b'.repeat(32)])
    await setAutoReporting(false); await submitManualReport('a'.repeat(32))
    await exportReport('a'.repeat(32)); await deleteReport('a'.repeat(32)); await recordFrontendRenderFailure()
    expect(vi.mocked(invoke).mock.calls).toEqual([
      ['report_control', { request: { operation: 'report_list' } }],
      ['report_control', { request: { operation: 'report_status' } }],
      ['report_control', { request: { operation: 'report_create', event_ids: [], description: 'description only' } }],
      ['report_control', { request: { operation: 'report_update', report_id: 'a'.repeat(32), event_ids: ['b'.repeat(32)], description: null } }],
      ['report_control', { request: { operation: 'report_consent', enabled: false } }],
      ['report_control', { request: { operation: 'report_submit', report_id: 'a'.repeat(32) } }],
      ['report_control', { request: { operation: 'report_export', report_id: 'a'.repeat(32) } }],
      ['report_control', { request: { operation: 'report_delete', report_id: 'a'.repeat(32) } }],
      ['report_control', { request: { operation: 'report_frontend_failure' } }],
    ])
  })
})

describe('allowedEnvelope auth contract', () => {
  const previewDiagnostic = (diagnostic: ReportDiagnostic) => allowedEnvelope({
    schema_version: 1, report_id: 'b'.repeat(32), mode: 'manual',
    app_version: '1', build_id: 'test', os: 'macos', os_version: '15', arch: 'aarch64',
    occurred_at: '2026-09-30T00:00:00Z', component: 'backend', stage: 'runtime',
    code: 'backend_runtime_failed', occurrences: 1, transitions: [], diagnostics: [diagnostic],
  }).diagnostics![0]
  const baseAuthDiagnostic = {
    version: 1 as const,
    correlation_id: 'a'.repeat(32),
    occurred_at: '2026-09-30T00:00:00Z',
    stage: 'auth_remember',
    failure_code: 'credential_locked',
    reason: 'keychain_query_failed',
    error_class: 'native_os_status',
    room_id: null,
    agent_id: null,
    connection_id: null,
    trigger_event_id: null,
    action_id: null,
    exception_chain: [],
    frames: [],
  }

  it('preserves valid auth_remember with all three optional auth metadata fields', () => {
    const diagnostic = {
      ...baseAuthDiagnostic,
      os_status: -25299,
      operation: 'sec_item_copy_matching',
      cleanup: 'success',
    }
    const result = previewDiagnostic(diagnostic)
    expect(result.os_status).toBe(-25299)
    expect(previewDiagnostic({ ...diagnostic, os_status: 0 }).os_status).toBe(0)
    expect(result.operation).toBe('sec_item_copy_matching')
    expect(result.cleanup).toBe('success')
  })

  it('preserves valid auth_restore with os_status only', () => {
    const diagnostic = {
      ...baseAuthDiagnostic,
      stage: 'auth_restore',
      failure_code: 'secret_store_unavailable',
      reason: 'keychain_backend_unavailable',
      error_class: 'secret_store_unavailable',
      os_status: -25300,
    }
    const result = previewDiagnostic(diagnostic)
    expect(result.os_status).toBe(-25300)
    expect(result.operation).toBeUndefined()
    expect(result.cleanup).toBeUndefined()
  })

  it('omits undefined optional fields from output', () => {
    const diagnostic = {
      ...baseAuthDiagnostic, stage: 'auth_restore', failure_code: 'secret_store_unavailable',
      reason: 'keychain_backend_unavailable', error_class: 'secret_store_unavailable',
    }
    const result = previewDiagnostic(diagnostic)
    expect('os_status' in result).toBe(false)
    expect('operation' in result).toBe(false)
    expect('cleanup' in result).toBe(false)
  })

  it('strips unknown and private properties', () => {
    const diagnostic = {
      ...baseAuthDiagnostic,
      os_status: -25299,
      cleanup: 'success',
      unknown_field: 'should be stripped',
      private_data: 'sensitive',
    }
    const result = previewDiagnostic(diagnostic)
    expect(result.os_status).toBe(-25299)
    expect('unknown_field' in result).toBe(false)
    expect('private_data' in result).toBe(false)
  })
})

describe('allowedEvent auth contract', () => {
  const baseEvent = {
    event_id: 'e1',
    occurred_at: '2026-09-30T00:00:00Z',
    component: 'native' as const,
    stage: 'runtime' as const,
    code: 'native_action_failed' as const,
    app_version: '1.0.0',
    build_id: 'build123',
    os: 'macos' as const,
    os_version: '15.0',
    arch: 'aarch64' as const,
  }

  it('preserves auth diagnostic with metadata through allowedEvent', () => {
    const diagnostic = {
      version: 1 as const,
      correlation_id: 'a'.repeat(32),
      occurred_at: '2026-09-30T00:00:00Z',
      stage: 'auth_remember',
      failure_code: 'credential_locked',
      reason: 'keychain_query_failed',
      error_class: 'native_os_status',
      room_id: null,
      agent_id: null,
      connection_id: null,
      trigger_event_id: null,
      action_id: null,
      exception_chain: [],
      frames: [],
      os_status: -25299,
      operation: 'sec_item_add',
      cleanup: 'failed',
    }
    const event = { ...baseEvent, diagnostic }
    const result = allowedEvent(event)
    expect(result.diagnostic?.os_status).toBe(-25299)
    expect(result.diagnostic?.operation).toBe('sec_item_add')
    expect(result.diagnostic?.cleanup).toBe('failed')
  })
})
