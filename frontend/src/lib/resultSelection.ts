import type { RoomEvent } from '../api/events'
import { voiceMemoFromEvent } from '../api/voiceMemos'
import type { ResultDetail } from '../api/roomResults'
import { canonicalEventId, peerReplies } from './peerReply'
import { assistantVisibleMarkdown } from './assistantContent'

export function validGeneratedFile(value:unknown,room:number,action:string):value is {version:1;ref:string;filename:string;size_bytes:number;sha256:string;media_type:'text/plain; charset=utf-8';capture_kind:'approved_write_payload'} {
 if(!value||typeof value!=='object'||Array.isArray(value)||!canonicalEventId(action))return false
 const v=value as Record<string,unknown>
 return Object.keys(v).sort().join(',')==='capture_kind,filename,media_type,ref,sha256,size_bytes,version'&&v.version===1&&v.ref===`generated-files/${room}/${action}.utf8`&&typeof v.filename==='string'&&v.filename.length>0&&v.filename.length<=200&&!/[\\/\x00-\x1f\x7f]/.test(v.filename)&&v.filename!=='.'&&v.filename!=='..'&&typeof v.size_bytes==='number'&&Number.isSafeInteger(v.size_bytes)&&v.size_bytes>=0&&v.size_bytes<=4000000&&typeof v.sha256==='string'&&/^[a-f0-9]{64}$/.test(v.sha256)&&v.media_type==='text/plain; charset=utf-8'&&v.capture_kind==='approved_write_payload'
}
export function resultSource(event: RoomEvent, room: number): 'text' | 'file' | null {
  if (event.room_id !== room || !canonicalEventId(event.event_id)) return null
  if (event.actor_type === 'agent' && event.kind === 'message' && typeof event.payload.content === 'string'
    && assistantVisibleMarkdown(event.payload.content).trim()) return 'text'
  const memo = event.payload.voice_memo as { owner_id?: unknown } | undefined
  if (memo && typeof memo.owner_id === 'number' && voiceMemoFromEvent(event, memo.owner_id, room)) return 'file'
  if (event.actor_type === 'tool' && event.kind === 'tool_result' && event.payload.state === 'completed'
    && canonicalEventId(event.payload.action_id)
    && (event.payload.screenshot_ref === `rooms/${room}/screenshots/${event.payload.action_id}.png` || event.actor_id === null && validGeneratedFile(event.payload.generated_file,room,event.payload.action_id))) return 'file'
  return null
}

/** Candidates are hints from already loaded events; the server validates the exact immutable version. */
export function resultOpinionCandidates(detail: ResultDetail, events: RoomEvent[]) {
  if (detail.review_support !== 'source_reply_only') return []
  return events.filter((event) => event.room_id === detail.room_id && event.actor_id !== detail.author_agent_id
    && peerReplies(event, events)?.references.some(({ id, source }) => id === detail.source.event_id && !!source))
}

export function resultHttpUrl(value: string): string | null {
  if (/[\s\\\u0000-\u001f\u007f]/.test(value)) return null
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? value : null
  } catch { return null }
}
