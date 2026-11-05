import type { FinalizeBlockedReason, FinalizePreview, FinalizeResult } from '../api/requestFinalize'
import { directoryViewGeneration } from './directoryViewMemory'
export type FinalizeAgent = { agent_id:number;name_snapshot:string|null }
export type FinalizeDraft = Readonly<{agentId:number;version:number;open:boolean;hintAttempted:boolean}>
export type FinalizeRead = Readonly<{id:string;roomId:number;sourceId:string;agentId:number;draftVersion:number;commandVersion:number;phase:'pending'|'ready'|'error';plan?:FinalizePreview;consumed:boolean;startedAt:string;receivedAt?:string}>
export type FinalizeCommand = Readonly<{id:string;roomId:number;sourceId:string;agentId:number;draftVersion:number;plan:FinalizePreview;body:{agent_id:number;plan_hash:string};key:string;retryOf?:string;phase:'pending'|'accepted'|'rejected'|'unknown';startedAt:string;receivedAt?:string;result?:FinalizeResult;code?:string;reason?:FinalizeBlockedReason;waitStopped?:boolean}>
type Workspace=Readonly<{drafts:ReadonlyMap<string,FinalizeDraft>;reads:ReadonlyMap<string,FinalizeRead>;commands:readonly FinalizeCommand[];pending:ReadonlyMap<number,string>}>
const empty:Workspace={drafts:new Map(),reads:new Map(),commands:[],pending:new Map()}
const owners=new Map<string,Workspace>(),listeners=new Set<()=>void>();let version=0
const copy=<T,>(value:T):T=>JSON.parse(JSON.stringify(value)),emit=()=>{version++;listeners.forEach(listener=>listener())}
export const finalizeSource=(roomId:number,sourceId:string)=>`${roomId}:${sourceId}`
export const finalizeTarget=(roomId:number,sourceId:string,agentId:number)=>`${roomId}:${sourceId}:${agentId}`
export const finalizeVersion=()=>version
export function subscribeFinalizers(listener:()=>void){listeners.add(listener);return()=>{listeners.delete(listener)}}
export const readFinalizers=(identity:string)=>owners.get(identity)||empty
export function clearFinalizers(){owners.clear();emit()}
export function initializeFinalizer(identity:string,generation:number,roomId:number,sourceId:string,agents:FinalizeAgent[]){
 const previous=readFinalizers(identity),target=finalizeSource(roomId,sourceId)
 if(generation!==directoryViewGeneration()||previous.drafts.has(target))return
 const drafts=new Map(previous.drafts);drafts.set(target,{agentId:agents[0]?.agent_id||0,version:0,open:false,hintAttempted:false});owners.set(identity,{...previous,drafts});emit()
}
export function patchFinalizer(identity:string,generation:number,roomId:number,sourceId:string,patch:Partial<Pick<FinalizeDraft,'agentId'|'open'|'hintAttempted'>>){
 const previous=readFinalizers(identity),target=finalizeSource(roomId,sourceId),old=previous.drafts.get(target)
 if(generation!==directoryViewGeneration()||!old)return false
 const draft={...old,...patch,version:patch.agentId!==undefined&&patch.agentId!==old.agentId?old.version+1:old.version}
 if(JSON.stringify(draft)===JSON.stringify(old))return true
 const drafts=new Map(previous.drafts);drafts.set(target,draft);owners.set(identity,{...previous,drafts});emit();return true
}
export function finalizeReceipt(identity:string,key:string){return [...readFinalizers(identity).commands].reverse().find(command=>command.key===key&&command.phase==='accepted'&&command.result)}
export function finalizeKeyRejected(identity:string,key:string){return [...readFinalizers(identity).commands].reverse().find(command=>command.key===key&&command.phase==='rejected'&&(command.code==='finalize_idempotency_conflict'||command.code==='request_finalize_idempotency_key_invalid'))}
export function finalizeRoomBlocked(identity:string,roomId:number){const previous=readFinalizers(identity);return previous.pending.has(roomId)||previous.commands.some(command=>command.roomId===roomId&&command.phase==='unknown'&&!finalizeReceipt(identity,command.key)&&!finalizeKeyRejected(identity,command.key))}
export function beginFinalizeRead(identity:string,generation:number,roomId:number,sourceId:string,agentId:number,draftVersion:number){
 const previous=readFinalizers(identity),draft=previous.drafts.get(finalizeSource(roomId,sourceId)),target=finalizeTarget(roomId,sourceId,agentId),old=previous.reads.get(target)
 if(generation!==directoryViewGeneration()||!draft||draft.agentId!==agentId||draft.version!==draftVersion||previous.pending.has(roomId)||old?.phase==='pending')return null
 const original:FinalizeRead={id:crypto.randomUUID(),roomId,sourceId,agentId,draftVersion,commandVersion:previous.commands.filter(command=>command.roomId===roomId).length,phase:'pending',plan:old?.plan,consumed:true,startedAt:new Date().toISOString()}
 const reads=new Map(previous.reads);reads.set(target,original);owners.set(identity,{...previous,reads});emit();return original
}
export function finishFinalizeRead(identity:string,generation:number,original:FinalizeRead,plan?:FinalizePreview){
 const previous=readFinalizers(identity),target=finalizeTarget(original.roomId,original.sourceId,original.agentId)
 if(generation!==directoryViewGeneration()||previous.reads.get(target)?.id!==original.id)return
 const reads=new Map(previous.reads);reads.set(target,{...original,phase:plan?'ready':'error',plan:plan?copy(plan):original.plan,consumed:!plan||previous.commands.filter(command=>command.roomId===original.roomId).length!==original.commandVersion,receivedAt:new Date().toISOString()});owners.set(identity,{...previous,reads});emit()
}
export function beginFinalizeCommand(identity:string,generation:number,roomId:number,sourceId:string,agentId:number,expectedReadId:string,originalId?:string){
 const previous=readFinalizers(identity),draft=previous.drafts.get(finalizeSource(roomId,sourceId)),read=previous.reads.get(finalizeTarget(roomId,sourceId,agentId)),original=originalId?previous.commands.find(command=>command.id===originalId):undefined
 if(generation!==directoryViewGeneration()||previous.pending.has(roomId))return null
 if(originalId&&(!original||original.roomId!==roomId||original.sourceId!==sourceId||original.agentId!==agentId||original.phase!=='unknown'||finalizeReceipt(identity,original.key)||finalizeKeyRejected(identity,original.key)))return null
 if(!original&&(finalizeRoomBlocked(identity,roomId)||!draft||draft.agentId!==agentId||!read||read.id!==expectedReadId||read.phase!=='ready'||read.consumed||read.draftVersion!==draft.version||!read.plan?.eligible||!read.plan.plan_hash))return null
 const plan=copy(original?.plan||read!.plan!),body=copy(original?.body||{agent_id:agentId,plan_hash:plan.plan_hash!})
 const command:FinalizeCommand={id:crypto.randomUUID(),roomId,sourceId,agentId,draftVersion:original?.draftVersion??draft!.version,plan,body,key:original?.key||crypto.randomUUID(),retryOf:original?.retryOf||original?.id,phase:'pending',startedAt:new Date().toISOString()}
 const pending=new Map(previous.pending);pending.set(roomId,command.id);const reads=new Map(previous.reads);for(const [target,value]of reads)if(value.roomId===roomId)reads.set(target,{...value,consumed:true})
 owners.set(identity,{...previous,pending,reads,commands:[...previous.commands,command]});emit();return command
}
export function finishFinalizeCommand(identity:string,generation:number,original:FinalizeCommand,outcome:Pick<FinalizeCommand,'phase'|'result'|'code'|'reason'>){
 const previous=readFinalizers(identity)
 if(generation!==directoryViewGeneration()||!previous.commands.some(command=>command.id===original.id&&(command.phase==='pending'||command.phase==='unknown'&&command.waitStopped)))return
 const pending=new Map(previous.pending);if(pending.get(original.roomId)===original.id)pending.delete(original.roomId)
 owners.set(identity,{...previous,pending,commands:previous.commands.map(command=>command.id===original.id?{...command,...copy(outcome),receivedAt:new Date().toISOString()}:command)});emit()
}
export function stopFinalizeWait(identity:string,generation:number,id:string){
 const previous=readFinalizers(identity),original=previous.commands.find(command=>command.id===id)
 if(generation!==directoryViewGeneration()||original?.phase!=='pending')return
 const pending=new Map(previous.pending);if(pending.get(original.roomId)===id)pending.delete(original.roomId)
 owners.set(identity,{...previous,pending,commands:previous.commands.map(command=>command.id===id?{...command,phase:'unknown' as const,waitStopped:true}:command)});emit()
}
