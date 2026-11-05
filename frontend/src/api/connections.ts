import api from '../lib/api'
import type { ReadinessTestRecord } from './readiness'

export type ProviderKind = 'openai' | 'anthropic' | 'gemini' | 'xai' | 'compatible'
export type Capability = 'max_tokens' | 'temperature' | 'tools' | 'vision'
export type VerifiedOptions = { base_url?: string | null } & Partial<Record<Capability, boolean>>
export type ProtocolId = 'openai_responses' | 'openai_chat_completions' | 'anthropic_messages' | 'gemini_generate_content'
  | 'azure_openai' | 'aws_bedrock_converse' | 'vertex_generate_content'
export type AuthMethodId = 'api_key' | 'local_none' | 'oauth_browser' | 'oauth_device' | 'native_chain'
export type PublicFieldValue = string | boolean

export interface PublicFieldDescriptor {
  field_id: string
  kind: 'text' | 'url' | 'boolean' | 'enum' | 'region' | 'project' | 'location' | 'deployment'
  required: boolean
  max_length?: number | null
}

export interface AuthMethodDescriptor {
  method_id: AuthMethodId
  code_state: 'implemented' | 'absent'
  delivery_phase: 'D1' | 'D2' | 'D3' | 'out_of_scope'
  external_eligibility: 'verified' | 'unknown' | 'unsupported'
  selectable: boolean
  evidence_ref?: string | null
  evidence_date?: string | null
}

export interface ProviderDescriptor {
  provider_id: string
  label_key: string
  icon: string
  protocols: ProtocolId[]
  fields: PublicFieldDescriptor[]
  model_source: {
    strategy: 'configured' | 'discovery' | 'native_catalog'
    manual_id: boolean
    static_model_ids: string[]
    static_model_protocols?: Partial<Record<string, ProtocolId>>
    static_evidence_ref?: string | null
    static_evidence_date?: string | null
  }
  methods: AuthMethodDescriptor[]
  default_base_url?: string | null
  technical_compatibility: 'supported' | 'unknown'
  usage_classification: 'verified' | 'unverified' | 'unknown'
  usage_scope?: 'general' | 'personal_interactive'
}

export interface ProviderCatalog { version: 1; providers: ProviderDescriptor[] }

export function isUsableAuthMethod(method: AuthMethodDescriptor) {
  return method.code_state === 'implemented'
    && method.external_eligibility === 'verified'
    && method.selectable === true
}

export interface Connection {
  chatgpt_account?: { label: string } | null
  id: number
  owner_id: number
  provider_kind: ProviderKind
  label: string
  base_url: string | null
  default_model_id: string
  enabled: boolean
  auth_required: boolean
  has_secret: boolean
  is_local: boolean
  capabilities: { declared: Partial<Record<Capability, boolean>>; verified: { models?: Record<string, VerifiedOptions> } }
  version?: 1
  provider_id?: string
  protocol_id?: ProtocolId
  auth_method_id?: AuthMethodId
  fields?: Record<string, PublicFieldValue>
  config_revision?: number
  auth_grant_revision?: number
  credential_state?: 'missing' | 'authorizing' | 'present' | 'refresh_required' | 'rejected' | 'locked' | 'unavailable' | 'not_required'
}

export interface ConnectionInput {
  provider_kind: ProviderKind
  label: string
  base_url?: string | null
  default_model_id: string
  enabled: boolean
  auth_required: boolean
  allow_local_http: boolean
  api_key?: string
  capabilities?: Partial<Record<Capability, boolean>>
}

export interface VersionedConnectionInput {
  version: 1
  provider_id: string
  protocol_id: ProtocolId
  auth_method_id: AuthMethodId
  fields: Record<string, PublicFieldValue>
  label: string
  default_model_id: string
  api_key?: string
  enabled?: boolean
  capabilities?: Partial<Record<Capability, boolean>>
}

export interface ModelItem { id: string; label: string; source: 'configured' | 'discovery' | 'native_catalog' }
export interface ModelCatalog { observed_at: string | null; stale: boolean; truncated: boolean; models: ModelItem[] }
export interface ConnectionRevisions { version: 1; config_revision: number; auth_grant_revision: number }
export type DisconnectRevokeState = 'revoked' | 'skipped' | 'not_supported' | 'already_invalid' | 'remote_revoke_failed'
export type DisconnectRestoredCredentialState = 'authorizing' | 'present' | 'refresh_required' | 'rejected' | 'locked' | 'unavailable'
export type DisconnectErrorCode =
  | 'auth_method_unavailable' | 'configuration_changed' | 'idempotency_conflict' | 'auth_operation_busy'
  | 'owner_epoch_unavailable' | 'owner_epoch_changed' | 'credential_store_unavailable'
  | 'credential_recovery_required' | 'credential_cleanup_unconfirmed' | 'provider_authorization_denied'
  | 'provider_temporary_failure' | 'provider_invalid_response' | 'operation_cancelled'
  | 'operation_expired' | 'operation_interrupted' | 'internal_error'
export interface DisconnectResult {
  credential_state: 'missing'
  revoke_state: DisconnectRevokeState
}
export interface DisconnectPending {
  version: 1
  operation_id: string
  state: 'pending' | 'finalizing'
  expires_at: null
  poll_interval: number
}
export interface DisconnectTerminalSuccess {
  version: 1
  operation_id: string
  state: 'succeeded'
  result: DisconnectResult
}
export interface DisconnectTerminalFailure {
  version: 1
  operation_id: string
  state: 'failed' | 'cancelled' | 'expired' | 'interrupted'
  result: { credential_state: DisconnectRestoredCredentialState; revoke_state: 'skipped' }
  error_code: DisconnectErrorCode
}
export type DisconnectTerminal = DisconnectTerminalSuccess | DisconnectTerminalFailure
export type DisconnectStartResponse = DisconnectResult | DisconnectPending
export type DisconnectOperationResponse = DisconnectPending | DisconnectTerminal

export interface AuthMethodAvailability { method_id: AuthMethodId; available: boolean }
export interface AuthMethodsResponse { version: 1; methods: AuthMethodAvailability[] }
export type AuthOperationState = 'prepared' | 'awaiting_user' | 'polling' | 'exchanging' | 'committing' | 'recovering'
  | 'pending' | 'finalizing' | 'succeeded' | 'cancelled' | 'expired' | 'interrupted' | 'failed'
export interface AuthOperationProgress {
  version: 1
  operation_id: string
  state: Exclude<AuthOperationState, 'succeeded'|'cancelled'|'expired'|'interrupted'|'failed'>
  expires_at: string | null
  browser_url?: string
  verification_uri?: string
  user_code?: string
  poll_interval?: number
  error_code?: DisconnectErrorCode
}
export interface AuthOperationSuccess { version: 1; operation_id: string; state: 'succeeded'; result: { credential_state: 'present' } }
export interface AuthOperationFailure { version: 1; operation_id: string; state: 'cancelled'|'expired'|'interrupted'|'failed'; error_code: DisconnectErrorCode }
export type AuthOperationResponse = AuthOperationProgress | AuthOperationSuccess | AuthOperationFailure

const disconnectRevokeStates = new Set<DisconnectRevokeState>(['revoked', 'skipped', 'not_supported', 'already_invalid', 'remote_revoke_failed'])
const restoredCredentialStates = new Set<DisconnectRestoredCredentialState>(['authorizing', 'present', 'refresh_required', 'rejected', 'locked', 'unavailable'])
const disconnectErrorCodes = new Set<DisconnectErrorCode>([
  'auth_method_unavailable', 'configuration_changed', 'idempotency_conflict', 'auth_operation_busy',
  'owner_epoch_unavailable', 'owner_epoch_changed', 'credential_store_unavailable',
  'credential_recovery_required', 'credential_cleanup_unconfirmed', 'provider_authorization_denied',
  'provider_temporary_failure', 'provider_invalid_response', 'operation_cancelled', 'operation_expired',
  'operation_interrupted', 'internal_error',
])
const operationIdPattern = /^[0-9a-f]{64}$/
const authMethodIds = new Set<AuthMethodId>(['api_key', 'local_none', 'oauth_browser', 'oauth_device', 'native_chain'])
const authProgressStates = new Set(['prepared', 'awaiting_user', 'polling', 'exchanging', 'committing', 'recovering', 'pending', 'finalizing'])
const authFailureStates = new Set(['cancelled', 'expired', 'interrupted', 'failed'])

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

function exactKeys(value: Record<string, unknown>, keys: string[]) {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  return actual.length === expected.length && actual.every((key, index) => key === expected[index])
}

function invalidDisconnectResponse(): never {
  throw new Error('Invalid disconnect response')
}

function parseImmediate(value: unknown): DisconnectResult | undefined {
  const item = record(value)
  if (!item || !exactKeys(item, ['credential_state', 'revoke_state']) || item.credential_state !== 'missing'
      || typeof item.revoke_state !== 'string' || !disconnectRevokeStates.has(item.revoke_state as DisconnectRevokeState)) return undefined
  return item as unknown as DisconnectResult
}

function parsePending(value: unknown): DisconnectPending | undefined {
  const item = record(value)
  if (!item || !exactKeys(item, ['version', 'operation_id', 'state', 'expires_at', 'poll_interval']) || item.version !== 1
      || typeof item.operation_id !== 'string' || !operationIdPattern.test(item.operation_id)
      || (item.state !== 'pending' && item.state !== 'finalizing') || item.expires_at !== null
      || !Number.isInteger(item.poll_interval) || Number(item.poll_interval) < 1 || Number(item.poll_interval) > 300) return undefined
  return item as unknown as DisconnectPending
}

function parseTerminal(value: unknown): DisconnectTerminal | undefined {
  const item = record(value)
  if (!item || item.version !== 1 || typeof item.operation_id !== 'string' || !operationIdPattern.test(item.operation_id)) return undefined
  if (item.state === 'succeeded' && exactKeys(item, ['version', 'operation_id', 'state', 'result'])) {
    const result = parseImmediate(item.result)
    return result ? { version: 1, operation_id: item.operation_id, state: 'succeeded', result } : undefined
  }
  if (!['failed', 'cancelled', 'expired', 'interrupted'].includes(String(item.state))
      || !exactKeys(item, ['version', 'operation_id', 'state', 'result', 'error_code'])
      || typeof item.error_code !== 'string' || !disconnectErrorCodes.has(item.error_code as DisconnectErrorCode)) return undefined
  const result = record(item.result)
  if (!result || !exactKeys(result, ['credential_state', 'revoke_state']) || result.revoke_state !== 'skipped'
      || typeof result.credential_state !== 'string' || !restoredCredentialStates.has(result.credential_state as DisconnectRestoredCredentialState)) return undefined
  return item as unknown as DisconnectTerminalFailure
}

export function parseDisconnectStartResponse(data: unknown, status: number): DisconnectStartResponse {
  const parsed = status === 200 ? parseImmediate(data) : status === 202 ? parsePending(data) : undefined
  return parsed || invalidDisconnectResponse()
}

export function parseDisconnectOperationResponse(data: unknown, status: number): DisconnectOperationResponse {
  const parsed = status === 202 ? parsePending(data) : status === 200 ? parseTerminal(data) : undefined
  return parsed || invalidDisconnectResponse()
}

function invalidAuthMethodsResponse(): never { throw new Error('Invalid auth methods response') }
function invalidAuthOperationResponse(): never { throw new Error('Invalid auth operation response') }

export function parseAuthMethodsResponse(data: unknown): AuthMethodsResponse {
  const item = record(data)
  if (!item || !exactKeys(item, ['version', 'methods']) || item.version !== 1 || !Array.isArray(item.methods)) return invalidAuthMethodsResponse()
  const seen = new Set<string>()
  const methods: AuthMethodAvailability[] = []
  for (const value of item.methods) {
    const method = record(value)
    if (!method || !exactKeys(method, ['method_id', 'available']) || typeof method.method_id !== 'string'
        || !authMethodIds.has(method.method_id as AuthMethodId) || typeof method.available !== 'boolean' || seen.has(method.method_id)) return invalidAuthMethodsResponse()
    seen.add(method.method_id)
    methods.push({ method_id: method.method_id as AuthMethodId, available: method.available })
  }
  return { version: 1, methods }
}

function safeAuthUrl(value: unknown): value is string {
  if (typeof value !== 'string' || value.length < 1 || value.length > 2048 || [...value].some((char) => char.charCodeAt(0) <= 32 || char.charCodeAt(0) === 127)) return false
  try {
    const parsed = new URL(value)
    return (parsed.protocol === 'https:' || parsed.protocol === 'http:') && !parsed.username && !parsed.password
  } catch { return false }
}

export function parseAuthOperationResponse(data: unknown, status: number): AuthOperationResponse {
  if (status !== 200) return invalidAuthOperationResponse()
  const item = record(data)
  if (!item || item.version !== 1 || typeof item.operation_id !== 'string' || !operationIdPattern.test(item.operation_id) || typeof item.state !== 'string') return invalidAuthOperationResponse()
  if (authProgressStates.has(item.state)) {
    const allowed = new Set(['version', 'operation_id', 'state', 'expires_at', 'browser_url', 'verification_uri', 'user_code', 'poll_interval', 'error_code'])
    if (Object.keys(item).some((key) => !allowed.has(key)) || !Object.hasOwn(item, 'expires_at')
        || !(item.expires_at === null || typeof item.expires_at === 'string' && item.expires_at.length <= 64
          && !Number.isNaN(Date.parse(item.expires_at)))) return invalidAuthOperationResponse()
    const browser = Object.hasOwn(item, 'browser_url')
    const deviceFields = ['verification_uri', 'user_code', 'poll_interval'].filter((key) => Object.hasOwn(item, key))
    if (browser && (!safeAuthUrl(item.browser_url) || deviceFields.length > 0)) return invalidAuthOperationResponse()
    if (deviceFields.length > 0 && (deviceFields.length !== 3 || !safeAuthUrl(item.verification_uri)
        || typeof item.user_code !== 'string' || item.user_code.length < 1 || item.user_code.length > 64
        || [...item.user_code].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
        || !Number.isInteger(item.poll_interval) || Number(item.poll_interval) < 1 || Number(item.poll_interval) > 300)) return invalidAuthOperationResponse()
    if (Object.hasOwn(item, 'error_code') && (typeof item.error_code !== 'string' || !disconnectErrorCodes.has(item.error_code as DisconnectErrorCode))) return invalidAuthOperationResponse()
    return item as unknown as AuthOperationProgress
  }
  if (item.state === 'succeeded' && exactKeys(item, ['version', 'operation_id', 'state', 'result'])) {
    const result = record(item.result)
    if (result && exactKeys(result, ['credential_state']) && result.credential_state === 'present') return item as unknown as AuthOperationSuccess
  }
  if (authFailureStates.has(item.state) && exactKeys(item, ['version', 'operation_id', 'state', 'error_code'])
      && typeof item.error_code === 'string' && disconnectErrorCodes.has(item.error_code as DisconnectErrorCode)) return item as unknown as AuthOperationFailure
  return invalidAuthOperationResponse()
}

export interface TestStatus {
  // Existing /test response fields; optional for older responses/fixtures.
  model_id?: string
  profile?: 'text'|'full'
  max_request_count?: number
  actual_request_count?: number
  ok: boolean
  code: string
  message: string
  verified?: VerifiedOptions
  record?: ReadinessTestRecord
}

export interface UnlockStatus { state: 'ready' | 'pending' | 'locked' | 'busy' }
export interface TestPreview { profile: 'text'|'full'; model_id: string; max_request_count: number }

export interface ModelFeatureReport {
 model_id:string;base_url:string|null;local:boolean;source:string;tested_at:string|null;observed_at:string|null;actual_generation_requests:number|null
 features:Record<string,{reported:boolean|null;tested:boolean|null;available:boolean;reason:'verified'|'unknown'|'unsupported'|'failed'|'app_unsupported'}>
}

export const connectionsApi = {
  localModels:async(provider_id:string,base_url:string)=>(await api.post<ModelCatalog>('/connections/local/models',{provider_id,base_url})).data,
  features:async(id:number,model_id:string)=>(await api.get<ModelFeatureReport>(`/connections/${id}/features`,{params:{model_id}})).data,
  checkFeatures:async(id:number,model_id:string)=>(await api.post<ModelFeatureReport>(`/connections/${id}/features/check`,{model_id},{timeout:190000})).data,
  list: async () => (await api.get<Connection[]>('/connections')).data,
  catalog: async () => (await api.get<ProviderCatalog>('/connections/catalog')).data,
  revisions: async (id: number) => (await api.get<ConnectionRevisions>(`/connections/${id}/revisions`)).data,
  models: async (id: number, refresh = false, revisions?: { configRevision: number; authGrantRevision: number }) => {
    if (!refresh) return (await api.get<ModelCatalog>(`/connections/${id}/models`)).data
    if (!revisions) throw new Error('Current connection revisions are required for model refresh.')
    return (await api.post<ModelCatalog>(`/connections/${id}/models/refresh`, {
      version: 1, config_revision: revisions.configRevision, auth_grant_revision: revisions.authGrantRevision,
    })).data
  },
  create: async (data: ConnectionInput | VersionedConnectionInput) => (await api.post<Connection>('/connections', data)).data,
  update: async (id: number, data: Partial<ConnectionInput> | VersionedConnectionInput) => (await api.patch<Connection>(`/connections/${id}`, data)).data,
  delete: async (id: number) => { await api.delete(`/connections/${id}`) },
  disconnect: async (id: number, revoke: boolean, idempotencyKey: string) => {
    const response = await api.post<unknown>(`/connections/${id}/disconnect`, { version: 1, revoke_remote: revoke },
      { headers: { 'Idempotency-Key': idempotencyKey } })
    return parseDisconnectStartResponse(response.data, response.status)
  },
  disconnectStatus: async (id: number, operationId: string) => {
    const response = await api.get<unknown>(`/connections/${id}/auth/operations/${operationId}`)
    return parseDisconnectOperationResponse(response.data, response.status)
  },
  cancelDisconnect: async (id: number, operationId: string) => {
    const response = await api.delete<unknown>(`/connections/${id}/auth/operations/${operationId}`)
    return parseDisconnectOperationResponse(response.data, response.status)
  },
  authMethods: async (id: number) => parseAuthMethodsResponse((await api.get<unknown>(`/connections/${id}/auth/methods`)).data),
  startAuth: async (id: number, method: AuthMethodId, revisions: ConnectionRevisions, idempotencyKey: string) => {
    const response = await api.post<unknown>(`/connections/${id}/auth/${method}/operations`, revisions,
      { headers: { 'Idempotency-Key': idempotencyKey } })
    return parseAuthOperationResponse(response.data, response.status)
  },
  authStatus: async (id: number, operationId: string) => {
    const response = await api.get<unknown>(`/connections/${id}/auth/operations/${operationId}`)
    return parseAuthOperationResponse(response.data, response.status)
  },
  cancelAuth: async (id: number, operationId: string) => {
    const response = await api.delete<unknown>(`/connections/${id}/auth/operations/${operationId}`)
    return parseAuthOperationResponse(response.data, response.status)
  },
  test: async (id: number, modelId?: string, profile?: 'text'|'full') => (await api.post<TestStatus>(`/connections/${id}/test`, null,
    { params: modelId || profile ? { ...(modelId ? { model_id: modelId } : {}), ...(profile ? { profile } : {}) } : undefined })).data,
  testPreview: async (id: number, modelId: string, profile: 'text'|'full') => (await api.get<TestPreview>(`/connections/${id}/test-preview`,
    { params: { model_id: modelId, profile } })).data,
  unlock: async (id: number) => (await api.post<UnlockStatus>(`/connections/${id}/unlock`)).data,
  unlockStatus: async (id: number) => (await api.get<UnlockStatus>(`/connections/${id}/unlock`)).data,
}
