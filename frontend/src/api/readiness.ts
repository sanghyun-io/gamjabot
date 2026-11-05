import api from '../lib/api'
import type { Capability, ProviderKind } from './connections'

export type ReadinessOutcome = 'connected'|'missing_secret'|'missing_model'|'secret_store_unavailable'|'invalid_credential'|'unreachable'|'redirect_refused'|'provider_error'|'rate_limited'|'quota_exhausted'|'invalid_response'|'connection_disabled'|'credential_locked'|'configuration_changed'
export type CredentialObservation = 'not_required'|'unknown'|'verified'|'rejected'|'missing'|'locked'|'unavailable'
export interface ReadinessTestRecord {
  id: number; connection_id: number; tested_at: string; duration_ms: number; model_id: string|null; ok: boolean; code: ReadinessOutcome
  config_fingerprint: string; config_current: boolean; credential_observation: CredentialObservation
  verified_capabilities: Record<Capability, boolean>|null
  profile?: 'text'|'full'|null
  actual_request_count?: number|null
}
export interface ConnectionReadiness {
  connection_id: number; label: string; provider_kind: ProviderKind; enabled: boolean; auth_required: boolean; has_secret: boolean
  public_config: { base_url: string|null; default_model_id: string; declared_capabilities: Partial<Record<Capability, boolean>> }
  config_fingerprint: string; declared_state: 'disabled'|'credential_missing'|'declared'; credential_observation: CredentialObservation
  binding_counts: { active_bound: number; active_missing_model: number; inactive: number; total: number }
  agent_bindings: Array<{ agent_id: number; agent_name: string; agent_active: boolean; model_id: string|null; status: 'bound'|'model_missing'|'inactive'; observation: 'unknown'|'verified'|'failed'; latest_test_id: number|null }>
  agent_bindings_truncated: boolean; latest_test: ReadinessTestRecord|null
}
export interface ReadinessResponse { connections: ConnectionReadiness[] }
export interface TestHistory { connection_id: number; current_config_fingerprint: string; items: ReadinessTestRecord[] }

export const readinessApi = {
  get: async () => (await api.get<ReadinessResponse>('/readiness')).data,
  history: async (connectionId: number) => (await api.get<TestHistory>(`/connections/${connectionId}/test-history`, { params: { limit: 20 } })).data,
}
