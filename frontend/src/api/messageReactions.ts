import api from '../lib/api'
import {canonicalEventId} from '../lib/peerReply'
import {useAuthStore} from '../stores/authStore'
import type {RoomEvent} from './events'
export const REACTION_EMOJI=['👍','❤️','😂','🎉','😮','👀'] as const
export type ReactionEmoji=typeof REACTION_EMOJI[number]
export type ReactionValue={version:1;room_id:number;target_event_id:string;owner_id:number;current_seq:number;current_event_id:string|null;emoji:ReactionEmoji|null;replayed:boolean;applied_seq?:number;applied_event_id?:string}
const object=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v)
export const isReactionEmoji=(v:unknown):v is ReactionEmoji=>typeof v==='string'&&(REACTION_EMOJI as readonly string[]).includes(v)
const owner=()=>{const a=useAuthStore.getState();if(!a.token||!Number.isSafeInteger(a.user?.id)||(!a.user?.id||a.user.id<1))throw Error('Reaction owner unavailable');return {id:a.user.id as number,epoch:a.epoch}}
const unchanged=(before:ReturnType<typeof owner>)=>{const a=useAuthStore.getState();if(!a.token||a.user?.id!==before.id||a.epoch!==before.epoch)throw Error('Reaction owner changed')}
const base=(room:number,target:string)=>`/conversations/${room}/messages/${target}/reaction`
function valid(value:unknown,room:number,target:string,ownerId:number,receipt:boolean):value is ReactionValue {
 if(!object(value)||value.version!==1||value.room_id!==room||value.target_event_id!==target||value.owner_id!==ownerId||!Number.isSafeInteger(value.current_seq)||Number(value.current_seq)<0||typeof value.replayed!=='boolean'||!(value.emoji===null||isReactionEmoji(value.emoji)))return false
 if(value.current_seq===0 ? value.current_event_id!==null || value.emoji!==null : !canonicalEventId(value.current_event_id))return false
 if(receipt && (!Number.isSafeInteger(value.applied_seq)||Number(value.applied_seq)<=0||Number(value.applied_seq)>Number(value.current_seq)||!canonicalEventId(value.applied_event_id)||(value.applied_seq===value.current_seq&&value.applied_event_id!==value.current_event_id)))return false
 return true
}
export const messageReactionsApi={
 applied:async(room:number,seq:number,signal?:AbortSignal):Promise<RoomEvent|null>=>{const a=owner(),{data}=await api.get<unknown>(`/conversations/${room}/events`,{params:{after_seq:seq-1,limit:1},signal});unchanged(a);if(!Array.isArray(data)||data.length>1)throw Error('Reaction event unavailable');return data.length===1?data[0] as RoomEvent:null},
 read:async(room:number,target:string,signal?:AbortSignal):Promise<ReactionValue>=>{const a=owner(),{data}=await api.get<unknown>(base(room,target),{signal});unchanged(a);if(!valid(data,room,target,a.id,false))throw Error('Reaction unavailable');return data},
 put:async(room:number,target:string,emoji:ReactionEmoji|null,expectedSeq:number,key:string,signal?:AbortSignal):Promise<ReactionValue>=>{const a=owner(),{data}=await api.put<unknown>(base(room,target),{emoji,expected_seq:expectedSeq},{headers:{'Idempotency-Key':key},signal});unchanged(a);if(!valid(data,room,target,a.id,true)||Number(data.applied_seq)<=expectedSeq||(data.applied_seq===data.current_seq&&data.emoji!==emoji))throw Error('Reaction receipt unavailable');return data},
 command:async(room:number,target:string,key:string,signal?:AbortSignal):Promise<ReactionValue>=>{const a=owner(),{data}=await api.get<unknown>(`${base(room,target)}/commands/${encodeURIComponent(key)}`,{signal});unchanged(a);if(!valid(data,room,target,a.id,true))throw Error('Reaction receipt unavailable');return data},
}
