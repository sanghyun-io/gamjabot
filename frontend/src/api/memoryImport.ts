import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { canonicalEventId } from '../lib/peerReply'

export type MemoryImportRequest={expected_primary_revision:number;expected_agent_id:number;source_label:string;content:string}
export type MemoryImportReceipt={version:1;agent_id:number;room_id:number;href:string;event_id:string;seq:number;replayed:boolean;status:'submitted'}
export const memoryImportApi={submit:async(body:MemoryImportRequest,key:string):Promise<MemoryImportReceipt>=>{
 const initial=useAuthStore.getState();if(!initial.token||!initial.user?.id)throw Error('Owner unavailable')
 const {data}=await api.post<MemoryImportReceipt>('/bots/primary/memory-import',body,{headers:{'Idempotency-Key':key}}),now=useAuthStore.getState()
 if(!now.token||now.user?.id!==initial.user.id||now.epoch!==initial.epoch)throw Error('Owner changed')
 if(!data||data.version!==1||data.agent_id!==body.expected_agent_id||!Number.isSafeInteger(data.room_id)||data.room_id<1||!canonicalEventId(data.event_id)||!Number.isSafeInteger(data.seq)||data.seq<1||data.href!==`/chat/${data.room_id}?seq=${data.seq}&event=${data.event_id}`||typeof data.replayed!=='boolean'||data.status!=='submitted')throw Error('Receipt unavailable')
 return data
}}
