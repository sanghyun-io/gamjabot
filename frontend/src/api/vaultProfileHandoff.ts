import api from '../lib/api'
import { invoke, isTauri } from '@tauri-apps/api/core'

export type NativeState = {version:1;status:'prepared'|'active'|'parked'|'closed'|'recovered'|'unknown'|'unavailable';handoff_id:string;profile_id:string|null;profile_revision:number|null;fill_status:'not_started'|'filled'|'filled_password_only'|'not_filled'|'unknown'|'cancelled'}
export type Handoff = {handoff_id:string;owner_id:number;owner_epoch:string;profile_id:string;profile_revision:number;room_id:number;origin:string;username:string;profile_label:string;account_label:string;state:'prepared'|'returned'|'recovered';prior_outcome_unknown:boolean}
export type HandoffStatus = {version:1;profile_id:string;revision:number;handoff:Handoff|null;native_status:NativeState|null;allowed_origins:[string,string,number][];can_fill:boolean;can_return:boolean;can_recover:boolean}
export type CustodyState = {version:1;status:'parked'|'borrowed'|'unknown'|'unavailable';handoff_id:string;profile_id:string|null;profile_revision:number|null;custody_id:string|null}
const hex32=/^[0-9a-f]{32}$/
const hex64=/^[0-9a-f]{64}$/
const id=(v:unknown):v is number=>Number.isSafeInteger(v)&&Number(v)>0
const rev=(v:unknown):v is number=>Number.isSafeInteger(v)&&Number(v)>=0
const safe=(v:unknown,max:number):v is string=>typeof v==='string'&&v.length<=max&&!/[\u0000-\u001f\u007f]/.test(v)
const origin=(v:unknown):v is string=>{if(!safe(v,2048))return false;try{const u=new URL(v);return u.protocol==='https:'&&!u.username&&!u.password&&u.origin===v}catch{return false}}
// The server binds these profile origins to the current profile revision.
export function formatProfileOrigins(value:unknown):string[]|null {
 if(!Array.isArray(value)||value.length>8)return null
 const result:string[]=[]
 for(const entry of value){
  if(!Array.isArray(entry)||entry.length!==3||!['http','https'].includes(entry[0])||typeof entry[1]!=='string'||!entry[1]||entry[1].length>253||/[\u0000-\u0020\u007f/@?#\\]/.test(entry[1])||!Number.isInteger(entry[2])||entry[2]<1||entry[2]>65535)return null
  try {const host=entry[1].includes(':')?`[${entry[1]}]`:entry[1];const u=new URL(`${entry[0]}://${host}:${entry[2]}`);if(u.username||u.password||u.pathname!=='/'||u.search||u.hash)return null;result.push(u.origin)} catch{return null}
 }
 return result
}
export function validNative(value:unknown,profileId:string,handoffId:string):value is NativeState {
 if(!value||typeof value!=='object'||Array.isArray(value))return false
 const v=value as NativeState
 return v.version===1&&v.handoff_id===handoffId
  && (v.status==='unavailable' ? v.profile_id===null && v.profile_revision===null && v.fill_status==='unknown' : v.profile_id===profileId && rev(v.profile_revision))
  &&['prepared','active','parked','closed','recovered','unknown','unavailable'].includes(v.status)
  &&['not_started','filled','filled_password_only','not_filled','unknown','cancelled'].includes(v.fill_status)
}
export function validHandoffStatus(value:unknown,profileId:string,roomId:number,ownerId:number):value is HandoffStatus {
 if(!value||typeof value!=='object'||Array.isArray(value))return false
 const v=value as HandoffStatus
 if(!id(roomId)||!formatProfileOrigins(v.allowed_origins)||v.version!==1||v.profile_id!==profileId||!hex32.test(profileId)||!rev(v.revision)||![v.can_fill,v.can_return,v.can_recover].every(x=>typeof x==='boolean'))return false
 if(v.handoff===null)return v.native_status===null&&!v.can_fill&&!v.can_return&&!v.can_recover
 const h=v.handoff
 if(!h||typeof h!=='object'||!hex32.test(h.handoff_id)||h.profile_id!==profileId||h.owner_id!==ownerId||!id(h.room_id)||!id(h.owner_id)||!hex64.test(h.owner_epoch)||!rev(h.profile_revision)||!origin(h.origin)||!safe(h.username,512)||!safe(h.profile_label,200)||!safe(h.account_label,200)||!['prepared','returned','recovered'].includes(h.state)||typeof h.prior_outcome_unknown!=='boolean')return false
 if(h.state==='prepared'&&(!formatProfileOrigins(v.allowed_origins)?.includes(h.origin)||(v.can_fill||v.can_return)&&h.profile_revision!==v.revision))return false
 if(!validNative(v.native_status,profileId,h.handoff_id))return false
 const n=v.native_status
 if(n.status!=='unavailable'&&n.profile_revision!==h.profile_revision)return false
 if((n.status==='unknown'||n.status==='unavailable')&&(v.can_fill||v.can_return))return false
 if(v.can_return&&n.status!=='closed')return false
 if(v.can_fill&&(h.state!=='prepared'||!['prepared','active'].includes(n.status)))return false
 return true
}
export function validCustody(value:unknown,profileId:string,handoffId:string,profileRevision:number):value is CustodyState {
 if(!value||typeof value!=='object'||Array.isArray(value))return false
 const v=value as CustodyState
 if(v.version!==1||v.handoff_id!==handoffId||!['parked','borrowed','unknown','unavailable'].includes(v.status))return false
 if(v.status==='unavailable')return v.profile_id===null&&v.profile_revision===null&&v.custody_id===null
 return v.profile_id===profileId&&v.profile_revision===profileRevision&&typeof v.custody_id==='string'&&hex32.test(v.custody_id)
}
const base=(roomId:number,profileId:string)=>`/tools/rooms/${roomId}/browser-profiles/${profileId}/vault-handoff`
export const vaultProfileHandoffApi={
 available:()=>isTauri(),
 seal:async(roomId:number,profileId:string,handoffId:string,revision:number)=>{const {data}=await api.post<unknown>(`${base(roomId,profileId)}/seal`,{handoff_id:handoffId,revision});if(!data||typeof data!=='object'||Array.isArray(data))throw Error('invalid');const v=data as {version:unknown;status:unknown;handoff_id:unknown;custody_id:unknown};if(v.version!==1||v.handoff_id!==handoffId||v.status!=='sealed'||typeof v.custody_id!=='string'||!hex32.test(v.custody_id))throw Error('unavailable');return v.custody_id},
 custody:async(roomId:number,profileId:string,handoffId:string,profileRevision:number,signal?:AbortSignal)=>{const {data}=await api.get<unknown>(`${base(roomId,profileId)}/custody`,{signal});if(!validCustody(data,profileId,handoffId,profileRevision))throw Error('invalid');return data},
 read:async(roomId:number,profileId:string,ownerId:number,signal?:AbortSignal)=>{const {data}=await api.get<unknown>(base(roomId,profileId),{signal});if(!validHandoffStatus(data,profileId,roomId,ownerId))throw Error('invalid');return data},
 finish:async(roomId:number,profileId:string,ownerId:number,handoffId:string,revision:number)=>{const {data}=await api.post<unknown>(`${base(roomId,profileId)}/return`,{handoff_id:handoffId,revision});if(!validHandoffStatus(data,profileId,roomId,ownerId))throw Error('invalid');return data},
 recover:async(roomId:number,profileId:string,ownerId:number,handoffId:string,revision:number)=>{const {data}=await api.post<unknown>(`${base(roomId,profileId)}/recover`,{handoff_id:handoffId,revision,confirm_return:true});if(!validHandoffStatus(data,profileId,roomId,ownerId))throw Error('invalid');return data},
 native:async(operation:'open_and_fill'|'close'|'park',ownerId:number,ownerEpoch:string,handoffId:string,profileId:string)=>{const result=await invoke<unknown>('vault_profile_native',{operation,ownerId,ownerEpoch,handoffId});if(!validNative(result,profileId,handoffId))throw Error('invalid');return result},
}
