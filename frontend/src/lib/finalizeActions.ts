import { requestFinalizeApi,type FinalizeBlockedReason,type FinalizePreview,type FinalizeResult } from '../api/requestFinalize'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from './directoryViewMemory'
import { queryClient } from './queryClient'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import { beginFinalizeCommand,beginFinalizeRead,finishFinalizeCommand,finishFinalizeRead,type FinalizeCommand } from './finalizeWorkspace'
export function finalizeOwnerCurrent(identity:string,generation:number){const auth=useAuthStore.getState();return !!auth.token&&generation===directoryViewGeneration()&&`${auth.user?.id||0}:${auth.epoch}`===identity}
const positive=(value:number)=>Number.isSafeInteger(value)&&value>0
const uuid=(value:string)=>typeof value==='string'&&/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value)
export const finalizeBlockedKeys:Record<FinalizeBlockedReason,`finalize.blocked.${FinalizeBlockedReason}`>={room_read_only:'finalize.blocked.room_read_only',source_invalid:'finalize.blocked.source_invalid',source_not_terminal:'finalize.blocked.source_not_terminal',source_outcome_unknown:'finalize.blocked.source_outcome_unknown',source_no_useful_result:'finalize.blocked.source_no_useful_result',source_all_passed:'finalize.blocked.source_all_passed',source_all_failed:'finalize.blocked.source_all_failed',source_too_large:'finalize.blocked.source_too_large',action_unsettled:'finalize.blocked.action_unsettled',action_uncertain:'finalize.blocked.action_uncertain',completed_effect:'finalize.blocked.completed_effect',room_busy:'finalize.blocked.room_busy',agent_not_selected:'finalize.blocked.agent_not_selected',agent_unavailable:'finalize.blocked.agent_unavailable',agent_busy:'finalize.blocked.agent_busy',model_unavailable:'finalize.blocked.model_unavailable',context_too_large:'finalize.blocked.context_too_large'}
function matchesPlan(plan:FinalizePreview,roomId:number,sourceId:string,agentId:number){
 if(plan.version!==1||plan.room_id!==roomId||typeof plan.eligible!=='boolean'||plan.source_request&&plan.source_request.event_id!==sourceId)return false
 if(!plan.eligible)return !!plan.blocked_reason&&Object.hasOwn(finalizeBlockedKeys,plan.blocked_reason)&&plan.plan_hash===null
 return plan.blocked_reason===null&&typeof plan.plan_hash==='string'&&/^[a-f0-9]{64}$/.test(plan.plan_hash)&&plan.agent?.agent_id===agentId&&plan.source_request?.event_id===sourceId&&positive(plan.source_request.seq)
  &&plan.limits?.max_rounds===1&&plan.limits.max_tool_calls===0&&Number.isSafeInteger(plan.limits.max_executor_seconds)&&plan.limits.max_executor_seconds>=1&&plan.limits.max_executor_seconds<=3600
  &&!!plan.current_model&&positive(plan.current_model.connection_id)&&typeof plan.current_model.model_id==='string'&&!!plan.current_model.model_id.trim()
  &&Array.isArray(plan.outcomes)&&plan.outcomes.length===plan.outcome_count&&plan.outcome_count>0&&plan.outcome_count<=256&&!plan.outcomes_truncated&&new Set(plan.outcomes.map(item=>item.event_id)).size===plan.outcomes.length
  &&plan.outcomes.every(item=>uuid(item.event_id)&&positive(item.seq)&&item.seq>plan.source_request!.seq)&&plan.source_coverage?.source_request_event_id===sourceId
}
function matchesResult(original:FinalizeCommand,result:FinalizeResult){
 const expected=original.plan.outcomes.map(item=>item.event_id)
 return result.version===1&&result.room_id===original.roomId&&result.source_request_event_id===original.sourceId&&result.agent_id===original.agentId&&uuid(result.finalize_event_id)&&positive(result.finalize_seq)
  &&result.finalize_seq>Math.max(original.plan.source_request?.seq||0,...original.plan.outcomes.map(item=>item.seq))&&typeof result.replayed==='boolean'&&Array.isArray(result.source_outcome_event_ids)&&result.source_outcome_event_ids.length===expected.length&&new Set(result.source_outcome_event_ids).size===expected.length&&result.source_outcome_event_ids.every(id=>expected.includes(id))
}
export function requestFinalizePlan(identity:string,generation:number,roomId:number,sourceId:string,agentId:number,draftVersion:number){
 if(!finalizeOwnerCurrent(identity,generation)||!positive(roomId)||!uuid(sourceId)||!positive(agentId))return null
 const original=beginFinalizeRead(identity,generation,roomId,sourceId,agentId,draftVersion)
 if(original)void(async()=>{try{const plan=await requestFinalizeApi.preview(roomId,sourceId,agentId);if(!finalizeOwnerCurrent(identity,generation))return;if(!matchesPlan(plan,roomId,sourceId,agentId))throw new Error('Finalizer preview target mismatch');finishFinalizeRead(identity,generation,original,plan)}catch{if(finalizeOwnerCurrent(identity,generation))finishFinalizeRead(identity,generation,original)}})()
 return original
}
async function send(identity:string,generation:number,original:FinalizeCommand){
 if(!finalizeOwnerCurrent(identity,generation))return
 try{
  const result=await requestFinalizeApi.execute(original.roomId,original.sourceId,original.body,original.key)
  if(!finalizeOwnerCurrent(identity,generation))return
  if(!matchesResult(original,result)){finishFinalizeCommand(identity,generation,original,{phase:'unknown',code:'response_finalize_target_mismatch'});return}
  finishFinalizeCommand(identity,generation,original,{phase:'accepted',result});for(const key of [['activity',original.roomId],['members',original.roomId],['dashboard']])void queryClient.invalidateQueries({queryKey:key})
 }catch(failure){
  if(!finalizeOwnerCurrent(identity,generation))return
  const detail=(failure as {response?:{data?:{detail?:{code?:unknown;reason?:unknown}}}}).response?.data?.detail,code=typeof detail?.code==='string'?detail.code:undefined,reason=typeof detail?.reason==='string'&&Object.hasOwn(finalizeBlockedKeys,detail.reason)?detail.reason as FinalizeBlockedReason:undefined
  finishFinalizeCommand(identity,generation,original,{phase:editorSaveFailure(failure)==='rejected'?'rejected':'unknown',code,reason})
 }
}
// The server receipt digest binds room/source/Agent, NOT plan_hash. Preserve the
// actual original body and plan anyway; never substitute a newer chosen preview.
export function requestFinalization(identity:string,generation:number,roomId:number,sourceId:string,agentId:number,expectedReadId:string,originalId?:string){
 if(!finalizeOwnerCurrent(identity,generation))return null
 const original=beginFinalizeCommand(identity,generation,roomId,sourceId,agentId,expectedReadId,originalId)
 if(original)void send(identity,generation,original)
 return original
}
