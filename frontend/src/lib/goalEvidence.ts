import type { RoomEvent } from '../api/events'
import type { PlanData } from './roomPlanEditorState'
import { assistantVisibleMarkdown } from './assistantContent'
export type GoalEvidenceChoice={seq:number;eventId:string;kind:string;actor:string;text:string}
const kinds=new Set(['message','tool_result','summary','activity'])
export function evidenceExcerpt(text:string,limit=80){
 const prefix=text.slice(0,limit*8).replace(/\s+/g,' ').trim(),points=Array.from(prefix)
 return points.slice(0,limit).join('')+(points.length>limit||text.length>limit*8?'…':'')
}
export function goalEvidenceChoices(roomId:number,events:readonly RoomEvent[],listed:PlanData['evidence'],visible?:ReadonlyMap<RoomEvent,string>):GoalEvidenceChoice[]{
 const rows=new Map<number,GoalEvidenceChoice>(),conflicts=new Set<number>()
 const add=(row:GoalEvidenceChoice)=>{
  if(!Number.isSafeInteger(row.seq)||row.seq<=0||!row.eventId||!kinds.has(row.kind)||conflicts.has(row.seq))return
  const previous=rows.get(row.seq)
  if(previous&&previous.eventId!==row.eventId){rows.delete(row.seq);conflicts.add(row.seq);return}
  rows.set(row.seq,row)
 }
 // Server-only labels may be truncated original Agent text. Until the original
 // event is received, expose metadata only instead of guessing its visible body.
 for(const row of listed)add({seq:row.seq,eventId:row.event_id,kind:row.kind,actor:row.actor,text:''})
 for(const event of events){
  if(event.room_id!==roomId||!kinds.has(event.kind))continue
  const raw=typeof event.payload.content==='string'?event.payload.content:typeof event.payload.text==='string'?event.payload.text:''
  const text=event.kind==='message'?(visible?.get(event)??(event.actor_type==='agent'?assistantVisibleMarkdown(raw):raw)):event.kind==='summary'?raw:''
  add({seq:event.seq,eventId:event.event_id,kind:event.kind,actor:event.actor_name_snapshot,text})
 }
 return [...rows.values()].sort((a,b)=>b.seq-a.seq)
}
