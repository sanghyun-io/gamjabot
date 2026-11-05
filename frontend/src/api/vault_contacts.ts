import api from '../lib/api'
export const contactFields=['name','email','phone','address_line1','address_line2','city','region','postal_code','country','nickname','hint'] as const
export type ContactFields=Record<typeof contactFields[number],string>
export type Contact=ContactFields & {item_id:string;revision:number;created_at:string;updated_at:string}
export type ContactReceipt={version:1;operation:'create'|'update'|'delete';command_key:string;item_id:string;revision:number}
const id=/^[0-9a-f]{32}$/
export const validContact=(x:Contact)=>!!x && id.test(x.item_id) && Number.isSafeInteger(x.revision) && x.revision>0 && contactFields.every(k=>typeof x[k]==='string') && typeof x.created_at==='string' && typeof x.updated_at==='string'
export const contactApi={
 list:async(after?:string)=>{const {data}=await api.get<{version:1;items:Contact[];next_cursor:string|null}>('/vault/contacts',{params:after?{after}:undefined});if(data.version!==1 || !Array.isArray(data.items) || data.items.length>20 || !data.items.every(validContact) || !(data.next_cursor===null || id.test(data.next_cursor)))throw Error('invalid');return data},
 create:async(value:ContactFields,key:string)=>(await api.post<ContactReceipt>('/vault/contacts',value,{headers:{'Idempotency-Key':key}})).data,
 update:async(item:string,value:ContactFields,revision:number,key:string)=>(await api.put<ContactReceipt>(`/vault/contacts/${item}`,{...value,expected_revision:revision},{headers:{'Idempotency-Key':key}})).data,
 remove:async(item:string,revision:number,key:string)=>(await api.delete<ContactReceipt>(`/vault/contacts/${item}`,{data:{expected_revision:revision},headers:{'Idempotency-Key':key}})).data,
 receipt:async(key:string)=>(await api.get<ContactReceipt>(`/vault/contact-commands/${key}`)).data,
}
