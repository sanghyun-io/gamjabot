import type {RoomEvent} from '../api/events'
import {isReactionEmoji,type ReactionEmoji} from '../api/messageReactions'
import {canonicalEventId} from './peerReply'
export type ReactionSnapshot={seq:number;eventId:string;emoji:ReactionEmoji|null}
export type BotReactionSnapshot={seq:number;eventId:string;agentId:number;name:string;emoji:ReactionEmoji|null}
export function botReactionActivity(event:RoomEvent,room:number):boolean {
 const p=event.payload
 if(!p||typeof p!=='object'||Array.isArray(p)||Object.keys(p).length!==9)return false
 return event.room_id===room&&event.kind==='activity'&&event.actor_type==='agent'&&Number.isSafeInteger(event.actor_id)&&Number(event.actor_id)>0
  &&canonicalEventId(event.event_id)&&Number.isSafeInteger(event.seq)&&event.seq>0&&typeof event.actor_name_snapshot==='string'&&event.actor_name_snapshot.trim().length>0&&event.actor_name_snapshot.length<=200
  &&p.category==='bot_message_reaction'&&p.version===1&&canonicalEventId(p.target_event_id)&&Number.isSafeInteger(p.target_seq)&&Number(p.target_seq)>0&&Number(p.target_seq)<event.seq
  &&(p.emoji===null||isReactionEmoji(p.emoji))&&Number.isSafeInteger(p.expected_seq)&&Number(p.expected_seq)>=0&&Number(p.expected_seq)<event.seq
  &&canonicalEventId(p.trigger_event_id)&&typeof p.call_id==='string'&&p.call_id.length>0&&p.call_id.length<=200&&Number.isSafeInteger(p.generation)&&Number(p.generation)>=0
}

export function foldBotReactions(events:RoomEvent[],room:number,owner:number,ownedAgentIds:ReadonlySet<number>):Map<string,BotReactionSnapshot[]> {
 const targets=new Map(events.filter(event=>event.room_id===room&&event.kind==='message'&&canonicalEventId(event.event_id)&&typeof event.payload?.content==='string'
  &&(event.actor_type==='user'&&event.actor_id===owner||event.actor_type==='agent'&&ownedAgentIds.has(event.actor_id!))).map(event=>[event.event_id,event]))
 const latest=new Map<string,Map<number,BotReactionSnapshot>>()
 for(const event of [...events].sort((a,b)=>a.seq-b.seq)){
  if(!botReactionActivity(event,room)||!ownedAgentIds.has(event.actor_id!))continue
  const target=targets.get(event.payload.target_event_id as string)
  if(!target||target.seq!==event.payload.target_seq)continue
  const byActor=latest.get(target.event_id)||new Map<number,BotReactionSnapshot>(),old=byActor.get(event.actor_id!)
  if(event.seq<=(old?.seq??0)||event.payload.expected_seq!==(old?.seq??0))continue
  byActor.set(event.actor_id!,{seq:event.seq,eventId:event.event_id,agentId:event.actor_id!,name:event.actor_name_snapshot,emoji:event.payload.emoji as ReactionEmoji|null})
  latest.set(target.event_id,byActor)
 }
 const result=new Map<string,BotReactionSnapshot[]>()
 for(const [id,actors] of latest)result.set(id,[...actors.values()].filter(value=>value.emoji!==null).sort((a,b)=>a.agentId-b.agentId))
 return result
}
export function reactionActivity(event:RoomEvent,room:number,owner:number):boolean {
 const p=event.payload
 if(!p||typeof p!=='object'||Array.isArray(p))return false
 return event.room_id===room&&event.kind==='activity'&&event.actor_type==='user'&&event.actor_id===owner&&canonicalEventId(event.event_id)&&Number.isSafeInteger(event.seq)&&event.seq>0&&p.category==='message_reaction'&&p.version===1&&canonicalEventId(p.target_event_id)&&(p.emoji===null||isReactionEmoji(p.emoji))
}
export function foldReactions(events:RoomEvent[],room:number,owner:number):Map<string,ReactionSnapshot>{
 const targets=new Map(events.filter(event=>event.room_id===room&&event.kind==='message'&&(event.actor_type==='agent'||event.actor_type==='user'&&event.actor_id===owner)&&canonicalEventId(event.event_id)).map(event=>[event.event_id,event]))
 const result=new Map<string,ReactionSnapshot>()
 for(const event of events){if(!reactionActivity(event,room,owner))continue;const target=targets.get(event.payload.target_event_id as string);if(!target||target.seq>=event.seq)continue;const old=result.get(target.event_id);if(!old||event.seq>old.seq)result.set(target.event_id,{seq:event.seq,eventId:event.event_id,emoji:event.payload.emoji as ReactionEmoji|null})}
 return result
}
