import type { ActionPreview, ExecutionBinding, RoutineBinding, SlackBinding, ToolActivity, ToolExecutionFields } from '../api/tools'
import type { BotMessageRef } from '../api/botMessages'
import type { RoomEvent } from '../api/events'
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const nonnegative = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const uuid = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value)
export function validToolRootRef(value: unknown, owner: number): value is BotMessageRef {
  return object(value) && positive(value.room_id) && uuid(value.event_id) && positive(value.seq)
    && value.actor_type === 'user' && value.actor_id === owner && value.kind === 'message'
}
export const sameToolRootRef = (a: BotMessageRef, b: BotMessageRef) => a.room_id === b.room_id && a.event_id === b.event_id && a.seq === b.seq && a.actor_type === b.actor_type && a.actor_id === b.actor_id && a.kind === b.kind
export function executionBindingKey(value: unknown): string | null {
  if (!object(value) || value.version !== 1 || !uuid(value.root_scope_id) || !uuid(value.scope_id) || !positive(value.owner_id)
    || !positive(value.root_room_id) || !uuid(value.root_request_event_id) || !positive(value.execution_room_id) || !positive(value.agent_id)
    || !nonnegative(value.run_generation) || !(value.job_id === null || uuid(value.job_id))
    || ![null, 'response', 'return'].includes(value.phase as null | string) || !['user_room', 'delegated'].includes(String(value.context_mode))
    || typeof value.deadline_at !== 'string' || !Number.isFinite(Date.parse(value.deadline_at))
    || !Array.isArray(value.lineage) || !value.lineage.length || !value.lineage.every(item => object(item) && uuid(item.scope_id) && nonnegative(item.epoch))) return null
  const lineage = value.lineage as Array<{ scope_id: string; epoch: number }>
  if (lineage[0].scope_id !== value.root_scope_id || lineage.at(-1)!.scope_id !== value.scope_id || new Set(lineage.map(item => item.scope_id)).size !== lineage.length) return null
  return JSON.stringify([1, value.root_scope_id, value.scope_id, value.owner_id, value.root_room_id, value.root_request_event_id,
    value.execution_room_id, value.agent_id, value.run_generation, value.job_id, value.phase, value.context_mode, value.deadline_at, lineage.map(item => [item.scope_id, item.epoch])])
}
export function routineBindingKey(v:unknown):string|null {
  if(!object(v) || v.version!==1 || !positive(v.owner_id) || !positive(v.agent_id) || !positive(v.room_id) || !uuid(v.routine_id) || !uuid(v.run_id) || !positive(v.config_revision) || !nonnegative(v.generation) || !uuid(v.root_event_id) || !['scheduled','test','webhook'].includes(String(v.origin)) || typeof v.scheduled_at!=='string' || !Number.isFinite(Date.parse(v.scheduled_at))) return null
  return JSON.stringify([v.version,v.owner_id,v.agent_id,v.room_id,v.routine_id,v.run_id,v.config_revision,v.generation,v.root_event_id,v.origin,v.scheduled_at])
}
export function routineMcpRootMatches(root:RoomEvent, context:ValidToolExecution, action:ToolActivity):boolean {
 if(context.kind!=='routine'||context.status!=='active'||action.state!=='waiting'||!action.mcp_binding)return false
 const r=context.binding, info=root.payload?.routine
 if(!object(info)||Object.keys(info).length!==8)return false
 return root.room_id===r.room_id&&root.event_id===r.root_event_id&&root.event_id===action.trigger_event_id&&root.seq===context.root.seq&&root.actor_type==='system'&&root.actor_id===null&&root.kind==='activity'&&root.payload.category==='owner_routine'&&root.payload.status==='started'&&info.version===1&&info.owner_id===r.owner_id&&info.agent_id===r.agent_id&&info.routine_id===r.routine_id&&info.run_id===r.run_id&&info.config_revision===r.config_revision&&info.origin===r.origin&&info.scheduled_at===r.scheduled_at&&action.mcp_binding.owner_id===r.owner_id&&action.mcp_binding.agent_id===r.agent_id&&action.mcp_binding.root_event_id===r.root_event_id&&action.mcp_binding.generation===r.generation
}
export function slackBindingKey(v:unknown):string|null {
 if(!object(v)||v.version!==1||!positive(v.owner_id)||!positive(v.agent_id)||!positive(v.room_id)||!uuid(v.run_id)||!positive(v.config_revision)||!positive(v.credential_revision)||!nonnegative(v.generation)||!uuid(v.root_event_id)||![v.workspace_id,v.channel_id,v.sender_id].every(x=>typeof x==='string'&&/^[A-Z0-9]{1,64}$/.test(x))||typeof v.event_id!=='string'||!/^[A-Za-z0-9]{1,128}$/.test(v.event_id)||typeof v.thread_ts!=='string'||!/^\d{1,20}\.\d{1,12}$/.test(v.thread_ts))return null
 return JSON.stringify([v.version,v.owner_id,v.agent_id,v.room_id,v.run_id,v.config_revision,v.credential_revision,v.generation,v.root_event_id,v.workspace_id,v.channel_id,v.sender_id,v.event_id,v.thread_ts])
}
export function slackToolRootMatches(root:RoomEvent, context:ValidToolExecution, action:ToolActivity):boolean {
 if(context.kind!=='slack'||context.status!=='active'||action.state!=='waiting')return false
 const r=context.binding,info=root.payload?.slack
 if(!object(info)||Object.keys(info).length!==8)return false
 return root.room_id===r.room_id&&root.event_id===r.root_event_id&&root.event_id===action.trigger_event_id&&root.seq===context.root.seq&&root.actor_type==='system'&&root.actor_id===null&&root.kind==='activity'&&root.payload.category==='slack_external'&&info.version===1&&info.run_id===r.run_id&&info.event_id===r.event_id&&info.workspace_id===r.workspace_id&&info.channel_id===r.channel_id&&info.user_id===r.sender_id&&info.thread_ts===r.thread_ts&&info.config_revision===r.config_revision&&(!action.mcp_binding||(action.mcp_binding.owner_id===r.owner_id&&action.mcp_binding.agent_id===r.agent_id&&action.mcp_binding.root_event_id===r.root_event_id&&action.mcp_binding.generation===r.generation))
}
export function mcpBindingKey(v:unknown):string|null {
 if(!object(v)||!uuid(v.server_id)||!positive(v.config_revision)||!positive(v.binding_revision)||typeof v.tool_id!=='string'||!/^[a-f0-9]{64}$/.test(v.tool_id)||typeof v.tool_name!=='string'||!v.tool_name||typeof v.schema_sha256!=='string'||!/^[a-f0-9]{64}$/.test(v.schema_sha256)||!positive(v.owner_id)||!positive(v.agent_id)||!uuid(v.root_event_id)||!nonnegative(v.generation)||typeof v.transport_sha256!=='string'||!/^[a-f0-9]{64}$/.test(v.transport_sha256)||typeof v.input_sha256!=='string'||!/^[a-f0-9]{64}$/.test(v.input_sha256)||!['http','stdio'].includes(String(v.transport_type))||v.transport_type==='stdio'&&(typeof v.executable!=='string'||!v.executable.startsWith('/'))||v.transport_type==='http'&&v.executable!==undefined)return null
 const teammate=v.teammate_binding
 if(teammate!=null && (!object(teammate)||!uuid(teammate.handoff_id)||!['response','return'].includes(String(teammate.phase))))return null
 return JSON.stringify([v.server_id,v.config_revision,v.binding_revision,v.tool_id,v.tool_name,v.schema_sha256,v.owner_id,v.agent_id,v.root_event_id,v.generation,v.transport_sha256,v.input_sha256,v.transport_type,v.executable??null,teammate ? [teammate.handoff_id,teammate.phase] : null])
}
export type ValidToolExecution = { kind: 'legacy' } | { kind: 'invalid' } | {
  kind: 'scoped'; binding: ExecutionBinding; root: BotMessageRef; status: 'active' | 'revoked' | 'expired'; revocation: 'pending' | 'settled' | 'unknown' | null
} | { kind:'routine'; binding:RoutineBinding; root:BotMessageRef; status:'active'|'revoked' } | { kind:'slack'; binding:SlackBinding; root:BotMessageRef; status:'active' }
export function executionContext(action: Pick<ToolActivity, 'action_id' | 'room_id' | 'agent_id' | 'trigger_event_id'> & ToolExecutionFields, owner: number, fields: ToolExecutionFields = action): ValidToolExecution {
  if (fields.slack_binding != null) {
    const r=fields.slack_binding,ref=fields.root_ref
    if(!positive(owner)||!uuid(action.action_id)||!slackBindingKey(r)||fields.execution_binding!=null||fields.routine_binding!=null||!ref||!positive(ref.room_id)||!positive(ref.seq)||!uuid(ref.event_id)||ref.actor_type!=='system'||ref.actor_id!==null||ref.kind!=='activity'||r.owner_id!==owner||r.agent_id!==action.agent_id||r.room_id!==action.room_id||r.room_id!==ref.room_id||r.root_event_id!==ref.event_id||action.trigger_event_id!==ref.event_id||fields.scope_status!=='active'||fields.revocation_state!=null)return {kind:'invalid'}
    return {kind:'slack',binding:r,root:ref,status:'active'}
  }
  if (fields.routine_binding != null) {
    const r=fields.routine_binding, ref=fields.root_ref
    if(!routineBindingKey(r) || fields.execution_binding != null || !ref || !positive(ref.room_id) || !positive(ref.seq) || !uuid(ref.event_id) || ref.actor_type!=='system' || ref.actor_id!==null || ref.kind!=='activity' || r.owner_id!==owner || r.agent_id!==action.agent_id || r.room_id!==action.room_id || r.room_id!==ref.room_id || r.root_event_id!==ref.event_id || action.trigger_event_id!==ref.event_id || !['active','revoked'].includes(String(fields.scope_status)) || fields.revocation_state != null) return {kind:'invalid'}
    return {kind:'routine',binding:r,root:ref,status:fields.scope_status as 'active'|'revoked'}
  }
  if (fields.execution_binding == null && fields.slack_binding == null && fields.root_ref == null && fields.scope_status == null && fields.revocation_state == null) return { kind: 'legacy' }
  const binding = fields.execution_binding
  if (!positive(owner) || !uuid(action.action_id) || !binding || !executionBindingKey(binding) || !validToolRootRef(fields.root_ref, owner)
    || binding.owner_id !== owner || binding.root_room_id !== fields.root_ref.room_id || binding.root_request_event_id !== fields.root_ref.event_id
    || action.room_id !== binding.execution_room_id || action.agent_id !== binding.agent_id || action.trigger_event_id !== binding.root_request_event_id
    || !['active', 'revoked', 'expired'].includes(String(fields.scope_status)) || ![null, 'pending', 'settled', 'unknown'].includes(fields.revocation_state ?? null)) return { kind: 'invalid' }
  return { kind: 'scoped', binding, root: fields.root_ref, status: fields.scope_status as 'active'|'revoked'|'expired', revocation: fields.revocation_state ?? null }
}
export function toolActionIdentity(action: ToolActivity) {
  return JSON.stringify([action.action_id, action.room_id, action.agent_id, action.trigger_event_id, action.family, action.target, action.expected_effect,
    action.slack_binding ? slackBindingKey(action.slack_binding) || 'invalid' : null, action.execution_binding ? executionBindingKey(action.execution_binding) || 'invalid' : null, action.routine_binding ? routineBindingKey(action.routine_binding) || 'invalid' : null, action.mcp_binding ? mcpBindingKey(action.mcp_binding) || 'invalid' : null, action.root_ref, action.scope_status, action.revocation_state,
    action.execution_binding || action.routine_binding || action.slack_binding || action.mcp_binding ? action.review_id : null])
}
export function actionPreviewMatches(action: ToolActivity, preview: ActionPreview, owner: number) {
  if (!preview || preview.action_id !== action.action_id || !preview.review_id || !preview.approval_policy || !Array.isArray(preview.approval_policy.scopes)
    || typeof preview.approval_policy.unverified_context_required !== 'boolean') return false
  const expected = executionContext(action, owner), received = executionContext(action, owner, preview)
  const review = object(preview.review) ? preview.review : null
  if(action.family==='mcp_tool_call') {
    const binding=action.mcp_binding, key=mcpBindingKey(binding)
    if(!key || !binding || binding.owner_id!==owner || binding.agent_id!==action.agent_id || binding.root_event_id!==action.trigger_event_id || key!==mcpBindingKey(preview.mcp_binding) || !action.review_id || action.review_id!==preview.review_id || preview.family!==action.family || preview.target!==action.target || preview.expected_effect!==action.expected_effect || preview.approval_policy.scopes.length!==1 || preview.approval_policy.scopes[0]!=='once' || !preview.approval_policy.unverified_context_required || !object(preview.arguments) || !review || !object(review.action) || !object(review.authority) || !object(review.authority.proposer))return false
    if(expected.kind==='invalid'||received.kind==='invalid'||expected.kind!==received.kind)return false
    if(expected.kind==='slack' && (received.kind!=='slack'||action.state!=='waiting'||binding.teammate_binding!=null||binding.generation!==expected.binding.generation||slackBindingKey(expected.binding)!==slackBindingKey(received.binding)||!sameToolRootRef(expected.root,received.root)))return false
    if(expected.kind==='routine' && (received.kind!=='routine'||expected.status!=='active'||received.status!=='active'||action.state!=='waiting'||binding.teammate_binding!=null||binding.generation!==expected.binding.generation||routineBindingKey(expected.binding)!==routineBindingKey(received.binding)||!sameToolRootRef(expected.root,received.root)))return false
    if(binding.teammate_binding && expected.kind!=='scoped')return false
    if(expected.kind==='scoped' && (received.kind!=='scoped'||expected.binding.run_generation!==binding.generation||executionBindingKey(expected.binding)!==executionBindingKey(received.binding)||!sameToolRootRef(expected.root,received.root)||expected.status!==received.status||expected.revocation!==received.revocation||expected.binding.context_mode==='user_room' && (expected.root.room_id!==action.room_id||expected.binding.job_id!==null||expected.binding.phase!==null)||expected.binding.context_mode==='delegated' && (!expected.binding.job_id||!expected.binding.phase||!!binding.teammate_binding)))return false
    const saved=review.action, authority=review.authority, proposer=authority.proposer as Record<string,unknown>
    return review.version===(expected.kind==='scoped'?2:1) && (expected.kind==='scoped'?executionBindingKey(saved.execution_binding)===executionBindingKey(expected.binding):saved.execution_binding==null) && saved.action_id===action.action_id && saved.room_id===action.room_id && saved.agent_id===action.agent_id && saved.trigger_event_id===action.trigger_event_id && saved.family===action.family && saved.target===preview.target && saved.scope_target===preview.scope_target && saved.expected_effect===preview.expected_effect && saved.payload_digest===preview.payload_digest && mcpBindingKey(saved.mcp_binding)===key && authority.owner_id===owner && authority.room_id===action.room_id && authority.agent_id===action.agent_id && proposer.owner_id===owner && proposer.room_id===action.room_id && proposer.agent_id===action.agent_id
  }
  if(expected.kind==='slack' && received.kind==='slack') {
    if(!action.review_id||action.review_id!==preview.review_id||action.family!==preview.family||action.target!==preview.target||action.expected_effect!==preview.expected_effect||slackBindingKey(expected.binding)!==slackBindingKey(received.binding)||!sameToolRootRef(expected.root,received.root)||action.state!=='waiting'||review?.version!==1||!object(review.action)||!object(review.authority)||!object(review.authority.proposer))return false
    const saved=review.action,authority=review.authority,proposer=authority.proposer as Record<string,unknown>
    return saved.action_id===action.action_id&&saved.room_id===action.room_id&&saved.agent_id===action.agent_id&&saved.trigger_event_id===action.trigger_event_id&&saved.execution_binding==null&&saved.family===preview.family&&saved.target===preview.target&&saved.scope_target===preview.scope_target&&saved.expected_effect===preview.expected_effect&&saved.payload_digest===preview.payload_digest&&authority.owner_id===owner&&authority.room_id===action.room_id&&authority.agent_id===action.agent_id&&proposer.owner_id===owner&&proposer.room_id===action.room_id&&proposer.agent_id===action.agent_id
  }
  if (expected.kind === 'routine' && received.kind === 'routine') {
    if(!action.review_id || action.review_id!==preview.review_id || action.family!==preview.family || action.target!==preview.target || action.expected_effect!==preview.expected_effect || routineBindingKey(expected.binding)!==routineBindingKey(received.binding) || !sameToolRootRef(expected.root,received.root) || expected.status!==received.status || review?.version!==1 || !object(review.action) || !object(review.authority) || !object(review.authority.proposer)) return false
    const saved=review.action, authority=review.authority, proposer=authority.proposer as Record<string,unknown>
    return saved.action_id===action.action_id && saved.room_id===action.room_id && saved.agent_id===action.agent_id && saved.trigger_event_id===action.trigger_event_id && saved.execution_binding==null && saved.family===preview.family && saved.target===preview.target && saved.scope_target===preview.scope_target && saved.expected_effect===preview.expected_effect && saved.payload_digest===preview.payload_digest && authority.owner_id===owner && authority.room_id===action.room_id && authority.agent_id===action.agent_id && proposer.owner_id===owner && proposer.room_id===action.room_id && proposer.agent_id===action.agent_id
  }
  if (expected.kind === 'legacy' && received.kind === 'legacy') return review?.version !== 2
  if (expected.kind !== 'scoped' || received.kind !== 'scoped' || executionBindingKey(expected.binding) !== executionBindingKey(received.binding)
    || !sameToolRootRef(expected.root, received.root) || expected.status !== received.status || expected.revocation !== received.revocation
    || review?.version !== 2 || !object(review.action) || !object(review.authority) || !object(review.authority.proposer)) return false
  const saved = review.action, authority = review.authority
  const proposer = object(authority.proposer) ? authority.proposer : null
  if (!proposer) return false
  return saved.action_id === action.action_id && saved.room_id === action.room_id && saved.agent_id === action.agent_id
    && saved.trigger_event_id === action.trigger_event_id && executionBindingKey(saved.execution_binding) === executionBindingKey(received.binding)
    && saved.family === preview.family && saved.target === preview.target && saved.scope_target === preview.scope_target
    && saved.expected_effect === preview.expected_effect && saved.payload_digest === preview.payload_digest
    && authority.owner_id === owner && authority.room_id === action.room_id && authority.agent_id === action.agent_id
    && proposer.owner_id === owner && proposer.room_id === action.room_id && proposer.agent_id === action.agent_id
    && (received.binding.context_mode !== 'delegated' || preview.approval_policy.scopes.length === 1 && preview.approval_policy.scopes[0] === 'once')
}
export function toolApprovalAvailable(context: ValidToolExecution) {
  return context.kind === 'legacy' || context.kind === 'slack' && context.status === 'active' || context.kind === 'routine' && context.status === 'active' || context.kind === 'scoped' && context.status === 'active' && context.revocation !== 'pending' && context.revocation !== 'unknown'
}
