import { validAvatar } from '../lib/avatarRecipe'
import { canonicalEventId } from '../lib/peerReply'
import { validSchedule, type RoutineSchedule } from './routines'
import api from '../lib/api'

export type DuplicatePreview={version:1;source_agent_id:number;name:string;plan_hash:string;profile:{description:string;system_prompt:string;connection_id:number|null;model_id:string|null;temperature:string|number|null;max_tokens:number|null;avatar:unknown};skills:{skill_id:string;revision:number;binding_revision:number;name:string}[];routines:{routine_id:string;revision:number;name:string;instruction:string;schedule:RoutineSchedule;enabled:boolean;next_run_at:string|null;webhook_setup_required:boolean}[];mcp_reconnect_required:boolean}
export type DuplicateReceipt={version:1;source_agent_id:number;name:string;plan_hash:string;agent_id:number;room_id:number;project_id:number;href:string;skill_count:number;routine_count:number;webhook_setup_required:boolean;mcp_reconnect_required:boolean;replayed:boolean}
export type DuplicateBody={name:string;plan_hash:string}
const positive=(v:unknown):v is number=>typeof v==='number'&&Number.isSafeInteger(v)&&v>0
const hash=(v:unknown):v is string=>typeof v==='string'&&/^[0-9a-f]{64}$/.test(v)
export function validDuplicatePreview(value:unknown,id:number,name:string):value is DuplicatePreview {
 const v=value as DuplicatePreview
 return !!v&&v.version===1&&v.source_agent_id===id&&v.name===name&&hash(v.plan_hash)&&!!v.profile&&validAvatar(v.profile.avatar)&&typeof v.profile.description==='string'&&typeof v.profile.system_prompt==='string'&&(v.profile.connection_id===null||positive(v.profile.connection_id))&&(v.profile.model_id===null||typeof v.profile.model_id==='string')&&Array.isArray(v.skills)&&v.skills.every(x=>x&&canonicalEventId(x.skill_id)&&Number.isSafeInteger(x.revision)&&Number.isSafeInteger(x.binding_revision)&&typeof x.name==='string')&&Array.isArray(v.routines)&&v.routines.every(x=>x&&canonicalEventId(x.routine_id)&&Number.isSafeInteger(x.revision)&&typeof x.name==='string'&&typeof x.instruction==='string'&&typeof x.enabled==='boolean'&&validSchedule(x.schedule)&&(x.next_run_at===null||typeof x.next_run_at==='string'&&Number.isFinite(Date.parse(x.next_run_at)))&&(!x.enabled||x.schedule.kind==='webhook'?x.next_run_at===null:true)&&typeof x.webhook_setup_required==='boolean')&&typeof v.mcp_reconnect_required==='boolean'
}
export function validDuplicateReceipt(value:unknown,id:number,body:DuplicateBody):value is DuplicateReceipt {
 const v=value as DuplicateReceipt
 return !!v&&v.version===1&&v.source_agent_id===id&&v.name===body.name&&v.plan_hash===body.plan_hash&&hash(v.plan_hash)&&positive(v.agent_id)&&v.agent_id!==id&&positive(v.room_id)&&positive(v.project_id)&&v.href===`/chat/${v.room_id}`&&Number.isSafeInteger(v.skill_count)&&v.skill_count>=0&&Number.isSafeInteger(v.routine_count)&&v.routine_count>=0&&typeof v.webhook_setup_required==='boolean'&&typeof v.mcp_reconnect_required==='boolean'&&typeof v.replayed==='boolean'
}
export const botDuplicateApi={
 preview:async(id:number,name:string)=>{const {data}=await api.post<unknown>(`/bots/${id}/clone/preview`,{name});if(!validDuplicatePreview(data,id,name))throw Error('Invalid preview');return data},
 submit:async(id:number,body:DuplicateBody,key:string)=>{const {data}=await api.post<unknown>(`/bots/${id}/clone`,body,{headers:{'Idempotency-Key':key}});return data},
 receipt:async(id:number,key:string)=>{const {data}=await api.get<unknown>(`/bots/${id}/clone/commands/${encodeURIComponent(key)}`);return data},
}
