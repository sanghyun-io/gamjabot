import type { RequestRetryBlockedReason, RequestRetryPreflight, RequestRetryResult } from '../api/requestRetries'
import { directoryViewGeneration } from './directoryViewMemory'
export type RetryRead = Readonly<{ id: string; roomId: number; failureId: string; phase: 'pending' | 'ready' | 'error'; plan?: RequestRetryPreflight; consumed: boolean; commandVersion: number; startedAt: string; receivedAt?: string }>
export type RetryCommand = Readonly<{ id: string; roomId: number; failureId: string; plan: RequestRetryPreflight & { plan_hash: string }; key: string; retryOf?: string; phase: 'pending' | 'accepted' | 'rejected' | 'unknown'; startedAt: string; receivedAt?: string; result?: RequestRetryResult; code?: string; reason?: RequestRetryBlockedReason; waitStopped?: boolean }>
type Workspace = Readonly<{ reads: ReadonlyMap<string, RetryRead>; commands: readonly RetryCommand[]; pending: ReadonlyMap<number, string> }>
const empty: Workspace = { reads: new Map(), commands: [], pending: new Map() }
const owners = new Map<string, Workspace>(), listeners = new Set<() => void>();let version = 0
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const emit = () => { version++;listeners.forEach(listener=>listener()) }
export const retryTarget = (roomId: number, failureId: string) => `${roomId}:${failureId}`
export const requestRetryVersion = () => version
export function subscribeRequestRetries(listener: () => void) { listeners.add(listener);return () => { listeners.delete(listener) } }
export const readRequestRetries = (identity: string) => owners.get(identity) || empty
export function clearRequestRetries() { owners.clear();emit() }
export function retryReceipt(identity: string, key: string) { return [...readRequestRetries(identity).commands].reverse().find(command=>command.key===key && command.phase==='accepted' && command.result) }
export function retryKeyRejected(identity: string, key: string) { return [...readRequestRetries(identity).commands].reverse().find(command=>command.key===key && command.phase==='rejected' && (command.code==='request_retry_idempotency_conflict'||command.code==='request_retry_idempotency_key_invalid')) }
export function retryRoomBlocked(identity: string, roomId: number) { const value=readRequestRetries(identity);return value.pending.has(roomId) || value.commands.some(command=>command.roomId===roomId && command.phase==='unknown' && !retryReceipt(identity,command.key) && !retryKeyRejected(identity,command.key)) }
export function beginRetryRead(identity: string, generation: number, roomId: number, failureId: string) {
  const previous=readRequestRetries(identity),target=retryTarget(roomId,failureId),old=previous.reads.get(target)
  if(generation!==directoryViewGeneration() || old?.phase==='pending' || previous.pending.has(roomId))return null
  const original: RetryRead={id:crypto.randomUUID(),roomId,failureId,phase:'pending',plan:old?.plan,consumed:true,commandVersion:previous.commands.filter(command=>command.roomId===roomId).length,startedAt:new Date().toISOString()}
  const reads=new Map(previous.reads);reads.set(target,original);owners.set(identity,{...previous,reads});emit();return original
}
export function finishRetryRead(identity: string,generation: number,original: RetryRead,plan?: RequestRetryPreflight) {
  const previous=readRequestRetries(identity),target=retryTarget(original.roomId,original.failureId)
  if(generation!==directoryViewGeneration() || previous.reads.get(target)?.id!==original.id)return
  const reads=new Map(previous.reads);reads.set(target,{...original,phase:plan?'ready':'error',plan:plan?copy(plan):original.plan,consumed:!plan || previous.commands.filter(command=>command.roomId===original.roomId).length!==original.commandVersion,receivedAt:new Date().toISOString()});owners.set(identity,{...previous,reads});emit()
}
export function beginRetryCommand(identity: string,generation: number,roomId: number,failureId: string,expectedReadId: string,originalId?: string) {
  const previous=readRequestRetries(identity),read=previous.reads.get(retryTarget(roomId,failureId)),original=originalId?previous.commands.find(command=>command.id===originalId):undefined
  if(generation!==directoryViewGeneration() || previous.pending.has(roomId))return null
  if(originalId && (!original || original.roomId!==roomId || original.failureId!==failureId || original.phase!=='unknown' || retryReceipt(identity,original.key) || retryKeyRejected(identity,original.key)))return null
  if(!original && (retryRoomBlocked(identity,roomId) || !read || read.id!==expectedReadId || read.phase!=='ready' || read.consumed || !read.plan?.eligible || !read.plan.plan_hash))return null
  const plan=copy(original?.plan || read!.plan!) as RetryCommand['plan']
  const command: RetryCommand={id:crypto.randomUUID(),roomId,failureId,plan,key:original?.key || crypto.randomUUID(),retryOf:original?.retryOf || original?.id,phase:'pending',startedAt:new Date().toISOString()}
  const pending=new Map(previous.pending);pending.set(roomId,command.id)
  const reads=new Map(previous.reads);for(const [target,value] of reads)if(value.roomId===roomId)reads.set(target,{...value,consumed:true})
  owners.set(identity,{...previous,reads,pending,commands:[...previous.commands,command]});emit();return command
}
export function finishRetryCommand(identity: string,generation: number,original: RetryCommand,outcome: Pick<RetryCommand,'phase'|'result'|'code'|'reason'>) {
  const previous=readRequestRetries(identity)
  if(generation!==directoryViewGeneration() || !previous.commands.some(command=>command.id===original.id && (command.phase==='pending' || command.phase==='unknown' && command.waitStopped)))return
  const pending=new Map(previous.pending);if(pending.get(original.roomId)===original.id)pending.delete(original.roomId)
  owners.set(identity,{...previous,pending,commands:previous.commands.map(command=>command.id===original.id?{...command,...copy(outcome),receivedAt:new Date().toISOString()}:command)});emit()
}
export function stopRetryWait(identity: string,generation: number,id: string) {
  const previous=readRequestRetries(identity),original=previous.commands.find(command=>command.id===id)
  if(generation!==directoryViewGeneration() || original?.phase!=='pending')return
  const pending=new Map(previous.pending);if(pending.get(original.roomId)===id)pending.delete(original.roomId)
  owners.set(identity,{...previous,pending,commands:previous.commands.map(command=>command.id===id?{...command,phase:'unknown' as const,waitStopped:true}:command)});emit()
}
