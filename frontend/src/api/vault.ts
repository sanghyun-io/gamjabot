import api from '../lib/api'
import { invoke, isTauri } from '@tauri-apps/api/core'

export type VaultStatus = { version:1; status:'present'|'missing'|'deleted'|'metadata_mismatch'|'unavailable'; revision:number|null; command_id:string|null }
export type VaultSession = { version:1; owner_id:number; owner_epoch:string|null; identity_attested:boolean }
export type VaultItem = { version:1; item_id:string; website:string; username:string; nickname:string; hint:string; revision:number; is_active:true; created_at:string; updated_at:string; secret_status:VaultStatus }
export type VaultImportReceipt = {version:1;operation:'import_reserve';command_key:string;state:'created'|'duplicate';item_id:string;revision:number;draft_id:string;row:number}
export type VaultCsvRow = {row:number;website:string;username:string;nickname:string;hint:string;state:'pending'|'stored'|'unknown'|'rejected'}
export type VaultCsvDraft = {version:1;draft_id:string;total:number;invalid_count:number;expires_in_seconds:number}
export type VaultCsvPage = {version:1;rows:VaultCsvRow[];errors:{row:number;reason:string}[];next_offset:number|null}
export type VaultCsvResult = {version:1;status:'stored'|'unknown'|'rejected'|'unavailable';revision:number|null;command_id:string|null}
export type VaultMetadata = Pick<VaultItem,'website'|'username'|'nickname'|'hint'>
export type VaultReceipt = {version:1; operation:'create'|'update'|'delete';command_key:string;item_id:string;revision:number}
export type VaultCloseResult = {version:1;status:'closed'|'unavailable';revision:null;command_id:string|null}
export type VaultFillResult = {version:1;status:'filled'|'filled_password_only'|'not_filled'|'cancelled'|'unknown'|'unavailable';revision:number|null;command_id:string|null}
export type NativeResult = {version:1;status:string;revision:number|null;command_id:string|null}
const uuid = /^[0-9a-f]{32}$/
export function validStatus(value:unknown):value is VaultStatus {
 if(!value || typeof value!=='object')return false
 const v=value as VaultStatus
 return v.version===1 && ['present','missing','deleted','metadata_mismatch','unavailable'].includes(v.status) && (v.revision===null || Number.isSafeInteger(v.revision) && v.revision>=0) && (v.command_id===null || typeof v.command_id==='string' && /^[A-Za-z0-9_-]{1,128}$/.test(v.command_id))
}
export function validItem(value:unknown):value is VaultItem {
 if(!value || typeof value!=='object')return false
 const v=value as VaultItem
 return v.version===1 && v.is_active===true && uuid.test(v.item_id) && [v.website,v.username,v.nickname,v.hint,v.created_at,v.updated_at].every(x=>typeof x==='string') && Number.isSafeInteger(v.revision) && v.revision>0 && validStatus(v.secret_status)
}
export const vaultApi={
 session:async()=>{const {data}=await api.get<VaultSession>('/vault/session');if(data.version!==1 || !Number.isSafeInteger(data.owner_id) || data.owner_id<1 || !(data.owner_epoch===null || typeof data.owner_epoch==='string' && /^[0-9a-f]{64}$/.test(data.owner_epoch)) || typeof data.identity_attested!=='boolean')throw Error('invalid');return data},
 list:async(after?:string)=>{const {data}=await api.get<{version:1;items:VaultItem[];next_cursor:string|null}>('/vault/logins',{params:after?{after}:undefined});if(data.version!==1 || !Array.isArray(data.items) || data.items.length>20 || !data.items.every(validItem) || !(data.next_cursor===null || uuid.test(data.next_cursor)))throw Error('invalid');return data},
 read:async(id:string)=>{const {data}=await api.get<VaultItem>(`/vault/logins/${id}`);if(!validItem(data) || data.item_id!==id)throw Error('invalid');return data},
 create:async(value:VaultMetadata,key:string)=>{const {data}=await api.post<VaultReceipt>('/vault/logins',value,{headers:{'Idempotency-Key':key}});return data},
 update:async(id:string,value:VaultMetadata,revision:number,key:string)=>{const {data}=await api.put<VaultReceipt>(`/vault/logins/${id}`,{...value,expected_revision:revision},{headers:{'Idempotency-Key':key}});return data},
 delete:async(id:string,revision:number,key:string)=>{const {data}=await api.delete<VaultReceipt>(`/vault/logins/${id}`,{data:{expected_revision:revision},headers:{'Idempotency-Key':key}});return data},
 importReserve:async(value:VaultMetadata,key:string,draftId:string,row:number)=>{const {data}=await api.post<VaultImportReceipt>('/vault/import/reservations',{...value,draft_id:draftId,row},{headers:{'Idempotency-Key':key}});return data},
 importBind:async(key:string)=>{const {data}=await api.post<{version:1;operation:'import_bind';command_key:string;native_ready:boolean}>(`/vault/import/reservations/${encodeURIComponent(key)}/bind`);return data},
 importReceipt:async(key:string)=>{const {data}=await api.get<VaultImportReceipt>(`/vault/commands/${encodeURIComponent(key)}`);return data},
 receipt:async(key:string)=>{const {data}=await api.get<VaultReceipt>(`/vault/commands/${encodeURIComponent(key)}`);return data},
 csvChoose:(args:{ownerId:number;ownerEpoch:string})=>invoke<VaultCsvDraft>('vault_csv_choose',args),
 csvPage:(args:{draftId:string;ownerId:number;ownerEpoch:string;offset:number;limit:number})=>invoke<VaultCsvPage>('vault_csv_action',{operation:'csv_page',...args}),
 csvSkip:(args:{draftId:string;ownerId:number;ownerEpoch:string;row:number})=>invoke<VaultCsvResult>('vault_csv_action',{operation:'csv_skip_row',...args}),
 csvStore:(args:{draftId:string;ownerId:number;ownerEpoch:string;row:number;itemId:string;commandId:string})=>invoke<VaultCsvResult>('vault_csv_action',{operation:'csv_store_row',...args}),
 csvReconcile:(args:{draftId:string;ownerId:number;ownerEpoch:string;row:number;itemId:string;commandId:string})=>invoke<VaultCsvResult>('vault_csv_action',{operation:'csv_reconcile',...args}),
 csvCancel:(args:{draftId:string;ownerId:number;ownerEpoch:string})=>invoke<{version:1;status:'cancelled'}>('vault_csv_action',{operation:'csv_cancel',...args}),
 nativeAvailable:()=>isTauri(),
 closeBrowser:(args:{ownerId:number;ownerEpoch:string;itemId:string;commandId:string})=>invoke<VaultCloseResult>('vault_native',{operation:'close_browser',...args}),
 fill:(args:{ownerId:number;ownerEpoch:string;itemId:string;expectedRevision:number;commandId:string;website:string;username:string})=>invoke<VaultFillResult>('vault_native',{operation:'open_and_fill',...args}),
 native:(args:{operation:'status'|'replace'|'delete';ownerId:number;ownerEpoch:string;itemId:string;expectedRevision?:number;commandId?:string;website?:string;username?:string})=>invoke<NativeResult>('vault_native',args),
}
