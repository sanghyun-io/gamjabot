import api from '../lib/api'

export type PinSource = {kind:'result';room_id:number;result_id:string;version:number} | {kind:'upload';upload_id:string;sha256:string}
export type PinAvailability = 'available'|'removed'|'unavailable'|'unknown'
export type PinStatus = {source:PinSource;revision:number;pinned:boolean;availability:PinAvailability}
export type PinOperation = {operation_id:string;source:PinSource;expected_revision:number;pinned:boolean;expected_owner_epoch:string}
export type PinReceipt = {state:'applied'|'rejected'|'fenced';revision:number|null;pinned:boolean|null;code:string|null;operation_id:string;source:PinSource;expected_revision:number;requested_pinned:boolean;expected_owner_epoch:string}
export const pinKey = (s:PinSource) => s.kind === 'result' ? `result:${s.room_id}:${s.result_id}:${s.version}` : `upload:${s.upload_id}:${s.sha256}`
const uuid = (s:unknown):s is string => typeof s === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(s)
const integer = (n:unknown):n is number => Number.isSafeInteger(n) && Number(n) > 0
export function validPinSource(v:unknown):v is PinSource {
 if(!v || typeof v!=='object' || Array.isArray(v))return false
 const s=v as Record<string,unknown>, keys=Object.keys(s).sort().join(',')
 return s.kind==='result' ? keys==='kind,result_id,room_id,version'&&uuid(s.result_id)&&integer(s.room_id)&&integer(s.version) : s.kind==='upload'&&keys==='kind,sha256,upload_id'&&uuid(s.upload_id)&&typeof s.sha256==='string'&&/^[a-f0-9]{64}$/.test(s.sha256)
}
function status(v:unknown):PinStatus {
 if(!v || typeof v!=='object' || Array.isArray(v))throw Error('Invalid pin status')
 const s=v as PinStatus
 if(!validPinSource(s.source)||!Number.isSafeInteger(s.revision)||s.revision<0||typeof s.pinned!=='boolean'||!['available','removed','unavailable','unknown'].includes(s.availability))throw Error('Invalid pin status')
 return s
}
function receipt(v:unknown, operation:PinOperation):PinReceipt {
 if(!v || typeof v!=='object' || Array.isArray(v))throw Error('Invalid pin receipt')
 const r=v as PinReceipt
 if(!['applied','rejected','fenced'].includes(r.state)||!(r.revision===null||Number.isSafeInteger(r.revision)&&r.revision>=0)||!(r.pinned===null||typeof r.pinned==='boolean')||!(r.code===null||typeof r.code==='string'))throw Error('Invalid pin receipt')
 if(r.operation_id!==operation.operation_id||!validPinSource(r.source)||pinKey(r.source)!==pinKey(operation.source)||r.expected_revision!==operation.expected_revision||r.requested_pinned!==operation.pinned||r.expected_owner_epoch!==operation.expected_owner_epoch)throw Error('Pin receipt mismatch')
 if(r.state==='applied' && (r.revision===null||r.pinned===null))throw Error('Invalid pin receipt')
 return r
}
const base=(bot:number)=>`/bots/${bot}/library/pins`
export const libraryPinsApi={
 context:async(bot:number,owner:number)=>{const {data}=await api.get(`${base(bot)}/context`);if(!data||data.owner_id!==owner||typeof data.owner_epoch!=='string'||!/^[a-f0-9]{64}$/.test(data.owner_epoch))throw Error('Invalid owner context');return data as {owner_id:number;owner_epoch:string}},
 list:async(bot:number,after?:string)=>{const {data}=await api.get<{items:unknown[];next_cursor:string|null}>(base(bot),{params:{limit:50,...(after?{after}:{})}});if(!data||!Array.isArray(data.items)||data.items.length>50||!(data.next_cursor===null||typeof data.next_cursor==='string'&&data.next_cursor.length>0))throw Error('Invalid pin page');const items=data.items.map(status);if(items.some(x=>!x.pinned)||new Set(items.map(x=>pinKey(x.source))).size!==items.length)throw Error('Invalid pin page');return {items,next_cursor:data.next_cursor}},
 status:async(bot:number,source:PinSource)=>{const {data}=await api.post(`${base(bot)}/status`,{source});const value=status(data);if(pinKey(value.source)!==pinKey(source))throw Error('Invalid pin status');return value},
 operate:async(bot:number,operation:PinOperation)=>receipt((await api.post(`${base(bot)}/operations`,operation)).data,operation),
 resolve:async(bot:number,operation:PinOperation)=>receipt((await api.post(`${base(bot)}/resolve`,operation)).data,operation),
}
