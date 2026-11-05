import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
export type SharingBinding={room_id:number;agent_ids:number[]}
export type SharingRoom={room_id:number;title:string;agents:{id:number;name:string;available:boolean}[]}
export type SharingSnapshot={profile_id:string;revision:number;home_room_id:number;allow_delegated:boolean;bindings:SharingBinding[];rooms:SharingRoom[]}
const object=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v)
const positive=(v:unknown):v is number=>typeof v==='number'&&Number.isSafeInteger(v)&&v>0
const guard=()=>{const s=useAuthStore.getState();if(!s.token||!positive(s.user?.id))throw Error('Owner unavailable');return `${s.user.id}:${s.epoch}`}
const check=(before:string)=>{if(guard()!==before)throw Error('Owner changed')}
const base=(room:number,id:string)=>`/tools/rooms/${room}/browser-profiles/${id}/sharing`
export const browserProfileSharingApi={
 read:async(room:number,id:string,signal?:AbortSignal):Promise<SharingSnapshot>=>{const before=guard(),{data}=await api.get<unknown>(base(room,id),{signal});check(before);if(!object(data)||data.profile_id!==id||data.home_room_id!==room||typeof data.allow_delegated!=='boolean'||!positive(data.revision)||!Array.isArray(data.bindings)||data.bindings.length>8||!Array.isArray(data.rooms)||data.rooms.length>1000)throw Error('Sharing unavailable');const bs=data.bindings,rs=data.rooms;if(!bs.every(x=>object(x)&&positive(x.room_id)&&x.room_id!==room&&Array.isArray(x.agent_ids)&&x.agent_ids.length<=16&&x.agent_ids.every(positive)&&new Set(x.agent_ids).size===x.agent_ids.length)||new Set(bs.map(x=>x.room_id)).size!==bs.length||!rs.every(x=>object(x)&&positive(x.room_id)&&x.room_id!==room&&typeof x.title==='string'&&Array.isArray(x.agents)&&x.agents.length<=1000&&x.agents.every((a:unknown)=>object(a)&&positive(a.id)&&typeof a.name==='string'&&typeof a.available==='boolean')&&new Set(x.agents.map((a:{id:number})=>a.id)).size===x.agents.length)||new Set(rs.map(x=>x.room_id)).size!==rs.length)throw Error('Sharing unavailable');return data as SharingSnapshot},
 save:async(room:number,id:string,revision:number,bindings:SharingBinding[],allowDelegated:boolean,signal?:AbortSignal)=>{const before=guard(),{data}=await api.post<unknown>(base(room,id),{revision,bindings,allow_delegated:allowDelegated},{signal});check(before);if(!object(data)||data.id!==id||!positive(data.revision)||data.revision<=revision||data.home_room_id!==room||data.allow_delegated!==allowDelegated)throw Error('Sharing result unavailable');return data},
}
