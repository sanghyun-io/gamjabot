import type {ConnectionRemovalTarget,ConnectionRemovalFailure} from '../components/connections/ConnectionRemoveDialog'
import {directoryViewGeneration} from './directoryViewMemory'
export type ConnectionRemovalRequest=Readonly<{id:string;target:Readonly<ConnectionRemovalTarget>;startedAt:string;finishedAt?:string;phase:'sending'|'succeeded'|ConnectionRemovalFailure}>
const empty:readonly ConnectionRemovalRequest[]=Object.freeze([])
const rows=new Map<string,readonly ConnectionRemovalRequest[]>(),pending=new Map<string,string>(),listeners=new Set<()=>void>();let version=0
const emit=()=>{version++;listeners.forEach(listener=>listener())}
export const connectionRemovalVersion=()=>version
export function subscribeConnectionRemovals(listener:()=>void){listeners.add(listener);return()=>{listeners.delete(listener)}}
export const readConnectionRemovals=(identity:string)=>rows.get(identity)||empty
export const connectionRemovalBusy=(identity:string)=>pending.has(identity)
export function clearConnectionRemovals(){rows.clear();pending.clear();emit()}
export function previousConnectionRemovalFailure(identity:string,id:number):ConnectionRemovalFailure|null{
 const last=readConnectionRemovals(identity).filter(row=>row.target.id===id).at(-1)
 return last&&last.phase!=='sending'&&last.phase!=='succeeded'?last.phase:null
}
export function beginConnectionRemoval(identity:string,generation:number,target:ConnectionRemovalTarget){
 if(generation!==directoryViewGeneration()||pending.has(identity))return null
 const request:ConnectionRemovalRequest=Object.freeze({id:crypto.randomUUID(),target:Object.freeze({...target}),startedAt:new Date().toISOString(),phase:'sending'})
 pending.set(identity,request.id);rows.set(identity,Object.freeze([...readConnectionRemovals(identity),request]));emit();return request
}
export function settleConnectionRemoval(identity:string,generation:number,id:string,phase:'succeeded'|ConnectionRemovalFailure){
 if(generation!==directoryViewGeneration()||pending.get(identity)!==id)return
 rows.set(identity,Object.freeze(readConnectionRemovals(identity).map(row=>row.id===id?Object.freeze({...row,phase,finishedAt:new Date().toISOString()}):row)));pending.delete(identity);emit()
}
