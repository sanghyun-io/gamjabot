import type {InactiveAgentReference} from '../api/agents'
import {directoryViewGeneration} from './directoryViewMemory'
export type InactiveReferenceRequest=Readonly<{id:string;connection:Readonly<{id:number;label:string}>;target:Readonly<InactiveAgentReference>;startedAt:string;finishedAt?:string;phase:'sending'|'received'|'rejected'|'unknown';response?:Readonly<InactiveAgentReference>}>
type Workspace=Readonly<{requests:readonly InactiveReferenceRequest[];needsRead:readonly number[]}>
const initial:Workspace=Object.freeze({requests:Object.freeze([]),needsRead:Object.freeze([])})
const rows=new Map<string,Workspace>(),pending=new Map<string,string>(),listeners=new Set<()=>void>();let version=0
const emit=()=>{version++;listeners.forEach(listener=>listener())}
export const inactiveReferenceVersion=()=>version
export function subscribeInactiveReferences(listener:()=>void){listeners.add(listener);return()=>{listeners.delete(listener)}}
export const readInactiveReferences=(identity:string)=>rows.get(identity)||initial
export const inactiveReferenceBusy=(identity:string)=>pending.has(identity)
export function clearInactiveReferences(){rows.clear();pending.clear();emit()}
export function sameInactiveReference(a:Readonly<InactiveAgentReference>,b:InactiveAgentReference|undefined){return !!b&&(['id','name','connection_id','model_id','model','is_active','updated_at'] as const).every(key=>a[key]===b[key])}
export function beginInactiveReference(identity:string,generation:number,connection:{id:number;label:string},target:InactiveAgentReference){
 const previous=readInactiveReferences(identity)
 if(generation!==directoryViewGeneration()||pending.has(identity)||previous.needsRead.includes(target.id)||target.is_active!==false||target.connection_id!==connection.id)return null
 const request:InactiveReferenceRequest=Object.freeze({id:crypto.randomUUID(),connection:Object.freeze({...connection}),target:Object.freeze({...target}),startedAt:new Date().toISOString(),phase:'sending'})
 pending.set(identity,request.id);rows.set(identity,Object.freeze({...previous,requests:Object.freeze([...previous.requests,request])}));emit();return request
}
export function inactiveReferenceSending(identity:string,requestId:string){return pending.get(identity)===requestId&&readInactiveReferences(identity).requests.some(row=>row.id===requestId&&row.phase==='sending')}
export function settleInactiveReference(identity:string,generation:number,original:InactiveReferenceRequest,phase:'received'|'rejected'|'unknown',response?:InactiveAgentReference){
 if(generation!==directoryViewGeneration()||!inactiveReferenceSending(identity,original.id))return false
 const previous=readInactiveReferences(identity),needsRead=phase==='received'?previous.needsRead:Object.freeze([...new Set([...previous.needsRead,original.target.id])])
 rows.set(identity,Object.freeze({requests:Object.freeze(previous.requests.map(row=>row.id===original.id?Object.freeze({...row,phase,finishedAt:new Date().toISOString(),response:response?Object.freeze({...response}):undefined}):row)),needsRead}));pending.delete(identity);emit();return true
}
// Explicit successful list read permits a NEW reviewed attempt; it never rewrites
// a prior outcome or turns an unknown request into a success/acknowledgment.
export function inactiveReferencesRead(identity:string,generation:number,connectionId:number){
 if(generation!==directoryViewGeneration())return
 const previous=readInactiveReferences(identity),ids=new Set(previous.requests.filter(row=>row.connection.id===connectionId).map(row=>row.target.id))
 const next=previous.needsRead.filter(id=>!ids.has(id))
 if(next.length===previous.needsRead.length)return
 rows.set(identity,Object.freeze({...previous,needsRead:Object.freeze(next)}));emit()
}
