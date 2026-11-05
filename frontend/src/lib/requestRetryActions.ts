import { requestRetriesApi, type RequestRetryBlockedReason, type RequestRetryPreflight, type RequestRetryResult } from '../api/requestRetries'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from './directoryViewMemory'
import { queryClient } from './queryClient'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import { beginRetryCommand, beginRetryRead, finishRetryCommand, finishRetryRead, type RetryCommand } from './requestRetryWorkspace'
export function retryOwnerCurrent(identity: string,generation: number) { const auth=useAuthStore.getState();return !!auth.token && generation===directoryViewGeneration() && `${auth.user?.id || 0}:${auth.epoch}`===identity }
const positive=(value:number)=>Number.isSafeInteger(value)&&value>0
const uuid=(value:string)=>typeof value==='string'&&/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value)
export function validTeammateRecovery(plan:RequestRetryPreflight) { const r=plan.teammate_recovery; return !!r && r.direct_count===1 && Number.isSafeInteger(r.nested_count) && r.nested_count>=0 && r.nested_count<=63 && Number.isSafeInteger(r.total_chars) && r.total_chars>=0 && ((r.version===1 && r.single_attempt===true) || (r.version===2 && r.single_attempt===false && Number.isSafeInteger(r.attempt_number) && r.attempt_number>=2 && r.attempt_number<=33)) }
export const retryBlockedKeys: Record<RequestRetryBlockedReason,`retry.blocked.${RequestRetryBlockedReason}`>={
 teammate_result_incomplete:'retry.blocked.teammate_result_incomplete',context_unavailable:'retry.blocked.context_unavailable',room_read_only:'retry.blocked.room_read_only',source_invalid:'retry.blocked.source_invalid',source_not_terminal:'retry.blocked.source_not_terminal',failure_superseded:'retry.blocked.failure_superseded',room_busy:'retry.blocked.room_busy',agent_unavailable:'retry.blocked.agent_unavailable',agent_busy:'retry.blocked.agent_busy',model_unavailable:'retry.blocked.model_unavailable',action_unsettled:'retry.blocked.action_unsettled',action_uncertain:'retry.blocked.action_uncertain',memory_context_changed:'retry.blocked.memory_context_changed',completed_effect:'retry.blocked.completed_effect',
}
function matchesPlan(plan:RequestRetryPreflight,roomId:number,failureId:string,requireTeammateRecovery:boolean) {
 if(plan.version!==1 || plan.room_id!==roomId || plan.failure_event_id!==failureId || !positive(plan.failure_seq) || typeof plan.eligible!=='boolean')return false
 if(!plan.eligible)return !!plan.blocked_reason && Object.hasOwn(retryBlockedKeys,plan.blocked_reason) && plan.plan_hash===null
 if(requireTeammateRecovery && !validTeammateRecovery(plan))return false
 return plan.blocked_reason===null && typeof plan.plan_hash==='string' && /^[a-f0-9]{64}$/.test(plan.plan_hash) && !!plan.source_request && uuid(plan.source_request.event_id) && positive(plan.source_request.seq) && plan.source_request.seq<plan.failure_seq
  && !!plan.agent && positive(plan.agent.agent_id) && !!plan.current_model && positive(plan.current_model.connection_id) && typeof plan.current_model.model_id==='string' && !!plan.current_model.model_id.trim()
  && !!plan.limits && plan.limits.max_rounds===1 && Number.isSafeInteger(plan.limits.max_tool_calls) && plan.limits.max_tool_calls>=0 && plan.limits.max_tool_calls<=64 && Number.isSafeInteger(plan.limits.max_executor_seconds) && plan.limits.max_executor_seconds>=1 && plan.limits.max_executor_seconds<=3600
}
function matchesResult(original:RetryCommand,result:RequestRetryResult) {
 return result.version===1 && result.room_id===original.roomId && result.source_failure_event_id===original.failureId && result.source_request_event_id===original.plan.source_request?.event_id && result.source_trigger_event_id===original.plan.source_request?.event_id
  && result.agent_id===original.plan.agent?.agent_id && uuid(result.retry_event_id) && positive(result.retry_seq) && result.retry_seq>original.plan.failure_seq && typeof result.replayed==='boolean'
}
export function requestRetryPlan(identity:string,generation:number,roomId:number,failureId:string,requireTeammateRecovery=false) {
 if(!retryOwnerCurrent(identity,generation) || !positive(roomId) || !uuid(failureId))return null
 const original=beginRetryRead(identity,generation,roomId,failureId)
 if(original)void(async()=>{try{const plan=await requestRetriesApi.preflight(roomId,failureId);if(!retryOwnerCurrent(identity,generation))return;if(!matchesPlan(plan,roomId,failureId,requireTeammateRecovery))throw new Error('Retry target response mismatch');finishRetryRead(identity,generation,original,plan)}catch{if(retryOwnerCurrent(identity,generation))finishRetryRead(identity,generation,original)}})()
 return original
}
async function send(identity:string,generation:number,original:RetryCommand) {
 if(!retryOwnerCurrent(identity,generation))return
 try {
  const result=await requestRetriesApi.start(original.roomId,original.failureId,original.plan.plan_hash,original.key)
  if(!retryOwnerCurrent(identity,generation))return
  if(!matchesResult(original,result)){finishRetryCommand(identity,generation,original,{phase:'unknown',code:'response_retry_target_mismatch'});return}
  finishRetryCommand(identity,generation,original,{phase:'accepted',result})
  for(const key of [['activity',original.roomId],['members',original.roomId],['dashboard']])void queryClient.invalidateQueries({queryKey:key})
 }catch(failure){
  if(!retryOwnerCurrent(identity,generation))return
  const detail=(failure as {response?:{data?:{detail?:{code?:unknown;reason?:unknown}}}}).response?.data?.detail
  const code=typeof detail?.code==='string'?detail.code:undefined,reason=typeof detail?.reason==='string'&&Object.hasOwn(retryBlockedKeys,detail.reason)?detail.reason as RequestRetryBlockedReason:undefined
  finishRetryCommand(identity,generation,original,{phase:editorSaveFailure(failure)==='rejected'?'rejected':'unknown',code,reason})
 }
}
// Only explicit button actions call this. Original replay preserves exact room,
// failure, plan_hash and Idempotency-Key. No automatic fresh key or fallback.
export function requestTargetedRetry(identity:string,generation:number,roomId:number,failureId:string,expectedReadId:string,originalId?:string) {
 if(!retryOwnerCurrent(identity,generation))return null
 const original=beginRetryCommand(identity,generation,roomId,failureId,expectedReadId,originalId)
 if(original)void send(identity,generation,original)
 return original
}
