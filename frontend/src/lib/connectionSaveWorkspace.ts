import type {ConnectionSaveFeedback} from '../components/ConnectionSaveNotice'
import {directoryViewGeneration} from './directoryViewMemory'
export type ConnectionSaveRecord=Readonly<{id:string;original:ConnectionSaveFeedback;feedback:ConnectionSaveFeedback;startedAt:string;finishedAt?:string}>
type Workspace=Readonly<{requests:readonly ConnectionSaveRecord[];active:ConnectionSaveFeedback|null}>
const initial:Workspace=Object.freeze({requests:Object.freeze([]),active:null})
const rows=new Map<string,Workspace>(),pending=new Map<string,number>(),listeners=new Set<()=>void>();let version=0
const emit=()=>{version++;listeners.forEach(listener=>listener())}
export const connectionSavesVersion=()=>version
export function subscribeConnectionSaves(listener:()=>void){listeners.add(listener);return()=>{listeners.delete(listener)}}
export const readConnectionSaves=(identity:string)=>rows.get(identity)||initial
export const connectionSaveBusy=(identity:string)=>pending.has(identity)
export const nextConnectionSaveAttempt=(identity:string)=>(readConnectionSaves(identity).requests.at(-1)?.original.attempt||0)+1
export function clearConnectionSaves(){rows.clear();pending.clear();emit()}
// Only explicitly selected nonsecret feedback fields enter the journal. Never
// retain the submitted API input, key value, URL/fields, prompts or credentials.
function snapshot(value:ConnectionSaveFeedback):ConnectionSaveFeedback{
 return Object.freeze({identity:value.identity,attempt:value.attempt,phase:value.phase,target:Object.freeze({id:value.target.id,label:value.target.label,providerId:value.target.providerId,modelId:value.target.modelId}),hadKey:value.hadKey,requestDetails:value.requestDetails?Object.freeze({authMethodId:value.requestDetails.authMethodId,protocolId:value.requestDetails.protocolId,enabled:value.requestDetails.enabled,requestedOptions:Object.freeze([...value.requestDetails.requestedOptions]) as unknown as typeof value.requestDetails.requestedOptions,keyHandling:value.requestDetails.keyHandling}):undefined})
}
export function updateConnectionSave(identity:string,generation:number,value:ConnectionSaveFeedback|null){
 if(generation!==directoryViewGeneration())return false
 const previous=readConnectionSaves(identity)
 if(!value){if(pending.has(identity))return false;rows.set(identity,Object.freeze({...previous,active:null}));emit();return true}
 if(value.identity!==identity)return false
 const feedback=snapshot(value)
 if(value.phase==='saving'){
  if(pending.has(identity)||value.attempt!==nextConnectionSaveAttempt(identity))return false
  const request:ConnectionSaveRecord=Object.freeze({id:crypto.randomUUID(),original:feedback,feedback,startedAt:new Date().toISOString()})
  pending.set(identity,value.attempt);rows.set(identity,Object.freeze({requests:Object.freeze([...previous.requests,request]),active:feedback}));emit();return true
 }
 if(pending.get(identity)!==value.attempt)return false
 rows.set(identity,Object.freeze({requests:Object.freeze(previous.requests.map(row=>row.original.attempt===value.attempt?Object.freeze({...row,feedback,finishedAt:new Date().toISOString()}):row)),active:feedback}));pending.delete(identity);emit();return true
}
