import api from '../lib/api'
import {invoke,isTauri} from '@tauri-apps/api/core'
export const billingFields=['address_line1','address_line2','city','region','postal_code','country'] as const
export type Billing=Record<typeof billingFields[number],string>
export type CardData={cardholder:string;expiry_month:number;expiry_year:number;billing:Billing;nickname:string;hint:string}
export type CardStatus={version:1;status:'present'|'missing'|'deleted'|'metadata_mismatch'|'unavailable';revision:number|null;command_id:string|null;last4:string|null}
export type Card=CardData & {item_id:string;last4:string|null;revision:number;created_at:string;updated_at:string;secret_status:CardStatus}
export type CardReceipt={version:1;operation:'create'|'update'|'confirm'|'delete';command_key:string;item_id:string;revision:number}
export type NativeCardResult={version:1;status:string;revision:number|null;command_id:string|null;last4:string|null}
const id=/^[0-9a-f]{32}$/
export const validCard=(v:Card)=>!!v && id.test(v.item_id) && typeof v.cardholder==='string' && Number.isSafeInteger(v.expiry_month) && v.expiry_month>=1 && v.expiry_month<=12 && Number.isSafeInteger(v.expiry_year) && v.expiry_year>=2000 && v.expiry_year<=9999 && !!v.billing && billingFields.every(k=>typeof v.billing[k]==='string') && typeof v.nickname==='string' && typeof v.hint==='string' && (v.last4===null || /^[0-9]{4}$/.test(v.last4)) && Number.isSafeInteger(v.revision) && v.revision>0 && !!v.secret_status && v.secret_status.version===1 && ['present','missing','deleted','metadata_mismatch','unavailable'].includes(v.secret_status.status)
export const cardApi={
 list:async(after?:string)=>{const {data}=await api.get<{version:1;items:Card[];next_cursor:string|null}>('/vault/cards',{params:after?{after}:undefined});if(data.version!==1 || !Array.isArray(data.items) || data.items.length>20 || !data.items.every(validCard) || !(data.next_cursor===null || id.test(data.next_cursor)))throw Error('invalid');return data},
 read:async(item:string)=>{const {data}=await api.get<Card>(`/vault/cards/${item}`);if(!validCard(data) || data.item_id!==item)throw Error('invalid');return data},
 create:async(value:CardData,key:string)=>(await api.post<CardReceipt>('/vault/cards',value,{headers:{'Idempotency-Key':key}})).data,
 update:async(item:string,value:CardData,revision:number,key:string)=>(await api.put<CardReceipt>(`/vault/cards/${item}`,{...value,expected_revision:revision},{headers:{'Idempotency-Key':key}})).data,
 confirm:async(item:string,revision:number,secretRevision:number,secretCommand:string,key:string)=>(await api.post<CardReceipt>(`/vault/cards/${item}/confirm`,{expected_revision:revision,secret_revision:secretRevision,secret_command:secretCommand},{headers:{'Idempotency-Key':key}})).data,
 remove:async(item:string,revision:number,key:string)=>(await api.delete<CardReceipt>(`/vault/cards/${item}`,{data:{expected_revision:revision},headers:{'Idempotency-Key':key}})).data,
 receipt:async(key:string)=>(await api.get<CardReceipt>(`/vault/card-commands/${key}`)).data,
 native:(args:{operation:'create'|'update'|'status'|'delete'|'open_and_fill';ownerId:number;ownerEpoch:string;itemId:string;expectedRevision:number|null;commandId:string|null;metadata:{cardholder:string;expiry_month:number;expiry_year:number;billing:Billing;last4:string|null}|null;website?:string})=>invoke<NativeCardResult>('vault_card_native',args),
 nativeAvailable:()=>isTauri(),
}
