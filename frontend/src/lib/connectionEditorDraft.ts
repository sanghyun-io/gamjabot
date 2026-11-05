import type {VersionedConnectionInput} from '../api/connections'
import {directoryViewGeneration} from './directoryViewMemory'
export type ConnectionDraftSource=Readonly<{config:number|null;auth:number|null}>
export type ConnectionEditorDraft=Readonly<{id:string;editing:number|null;form:Omit<VersionedConnectionInput,'api_key'>;baselineFingerprint:string|null;baselineSnapshot:string;source:ConnectionDraftSource|null;step:number;requiresSecretReentry:boolean;changedAt:string}>
export type ConnectionDraftInput=Omit<ConnectionEditorDraft,'changedAt'>
const closed=new Map<string,Set<string>>()
const rows=new Map<string,ConnectionEditorDraft>(),listeners=new Set<()=>void>();let version=0
const emit=()=>{version++;listeners.forEach(listener=>listener())}
export const connectionDraftVersion=()=>version
export function subscribeConnectionDrafts(listener:()=>void){listeners.add(listener);return()=>{listeners.delete(listener)}}
export const readConnectionDraft=(identity:string)=>rows.get(identity)||null
export function clearConnectionDrafts(){rows.clear();closed.clear();emit()}
export function keepConnectionDraft(identity:string,generation:number,value:ConnectionDraftInput){
 if(generation!==directoryViewGeneration()||closed.get(identity)?.has(value.id))return false
 const form=value.form
 // Explicit public-input allowlist: api_key and arbitrary extra properties never enter this workspace.
 const copy:Omit<VersionedConnectionInput,'api_key'>={version:1,provider_id:form.provider_id,protocol_id:form.protocol_id,auth_method_id:form.auth_method_id,fields:Object.freeze({...form.fields}),label:form.label,default_model_id:form.default_model_id,enabled:form.enabled,capabilities:Object.freeze({...form.capabilities})}
 rows.set(identity,Object.freeze({id:value.id,editing:value.editing,form:Object.freeze(copy),baselineFingerprint:value.baselineFingerprint,baselineSnapshot:value.baselineSnapshot,source:value.source?Object.freeze({...value.source}):null,step:Math.max(1,Math.min(4,value.step)),requiresSecretReentry:value.requiresSecretReentry,changedAt:new Date().toISOString()}));emit();return true
}
export function forgetConnectionDraft(identity:string,generation:number,id?:string){
 if(generation!==directoryViewGeneration())return false
 const target=id||rows.get(identity)?.id
 if(target){const ended=closed.get(identity)||new Set<string>();ended.add(target);closed.set(identity,ended)}
 if(id&&rows.get(identity)?.id!==id)return false
 const changed=rows.delete(identity);if(changed)emit();return changed
}
