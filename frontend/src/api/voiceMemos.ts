import api from '../lib/api'
import type { RoomEvent } from './events'
import { canonicalEventId } from '../lib/peerReply'

export const MAX_VOICE_MEMO_BYTES = 10 * 1024 * 1024
export type VoiceMemo = { version:1; owner_id:number; room_id:number; agent_id:number; trigger_event_id:string; action_id:string; review_id:string; grant_id:number; generation:number; media_type:'audio/wav'; size_bytes:number; sha256:string; duration_ms:number; transcript:string; text_sha256:string; voice_id:string; locale:string; rate_milli:number; deleted:boolean }
export type VoiceMemoRef = VoiceMemo & { adapter:'voice_memo'; event_seq:number }
const positive = (v:unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0
const hash = (v:unknown): v is string => typeof v === 'string' && /^[0-9a-f]{64}$/.test(v)
export function validVoiceMemo(value:unknown, owner:number, room:number, action?:string): value is VoiceMemo {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const m = value as Record<string,unknown>
  return m.version === 1 && m.owner_id === owner && m.room_id === room && positive(m.agent_id) && canonicalEventId(m.action_id) && (!action || m.action_id === action) && canonicalEventId(m.trigger_event_id) && canonicalEventId(m.review_id) && positive(m.grant_id) && positive(m.generation) && m.media_type === 'audio/wav' && positive(m.size_bytes) && m.size_bytes <= MAX_VOICE_MEMO_BYTES && hash(m.sha256) && positive(m.duration_ms) && m.duration_ms <= 300000 && typeof m.transcript === 'string' && !!m.transcript.trim() && new TextEncoder().encode(m.transcript).length <= 4000 && hash(m.text_sha256) && typeof m.voice_id === 'string' && m.voice_id.length > 0 && m.voice_id.length <= 200 && typeof m.locale === 'string' && m.locale.length > 0 && m.locale.length <= 64 && positive(m.rate_milli) && m.rate_milli <= 1000 && typeof m.deleted === 'boolean'
}
export function voiceMemoFromEvent(event:RoomEvent, owner:number, room:number): VoiceMemo | null {
  const m = event.payload.voice_memo
  if (event.room_id !== room || event.actor_type !== 'tool' || event.actor_id !== null || event.kind !== 'tool_result' || event.payload.state !== 'completed' || !canonicalEventId(event.event_id) || !positive(event.seq) || !validVoiceMemo(m, owner, room) || m.deleted || event.idempotency_key !== `tool-result:${m.action_id}` || event.payload.action_id !== m.action_id || event.payload.agent_id !== m.agent_id || event.payload.trigger_event_id !== m.trigger_event_id || event.payload.review_id !== m.review_id) return null
  return m
}
export type VoiceMemoSource = { kind:'event' } | { kind:'result'; resultId:string; version:number }
export type VoiceMemoLoader = (room:number, memo:VoiceMemo, source:VoiceMemoSource, signal:AbortSignal) => Promise<{ data:ArrayBuffer; mediaType:string }>
export const loadVoiceMemo:VoiceMemoLoader = async (room,memo,source,signal) => {
  const path = source.kind === 'event' ? `/conversations/${room}/voice-memos/${memo.action_id}/file` : `/conversations/${room}/results/${source.resultId}/versions/${source.version}/voice-memo`
  const limited=new AbortController();let exceeds=false
  const abort=()=>limited.abort();signal.addEventListener('abort',abort,{once:true});if(signal.aborted)limited.abort()
  try { const response = await api.get<ArrayBuffer>(path,{responseType:'arraybuffer',signal:limited.signal,maxContentLength:MAX_VOICE_MEMO_BYTES,onDownloadProgress:event=>{if(event.loaded>memo.size_bytes || event.total !== undefined && event.total>memo.size_bytes){exceeds=true;limited.abort()}}})
    return {data:response.data,mediaType:String(response.headers['content-type'] || '').toLowerCase().split(';')[0].trim()}
  } catch(error) { if(exceeds) throw {response:{status:409}};throw error } finally { signal.removeEventListener('abort',abort) }
}
export function validMemoWav(data:ArrayBuffer, memo:VoiceMemo): boolean {
  if (!(data instanceof ArrayBuffer) || data.byteLength !== memo.size_bytes || data.byteLength < 44 || data.byteLength > MAX_VOICE_MEMO_BYTES) return false
  const v = new DataView(data), code = (at:number) => String.fromCharCode(v.getUint8(at),v.getUint8(at+1),v.getUint8(at+2),v.getUint8(at+3))
  if (code(0) !== 'RIFF' || code(8) !== 'WAVE' || v.getUint32(4,true) + 8 !== data.byteLength) return false
  let offset = 12, fmt = false, frames = -1
  while (offset + 8 <= data.byteLength) {
    const tag = code(offset), size = v.getUint32(offset+4,true), body = offset+8, end = body+size
    if (end > data.byteLength || end < body) return false
    if (tag === 'fmt ') { if (fmt || size < 16 || v.getUint16(body,true) !== 1 || v.getUint16(body+2,true) !== 1 || v.getUint32(body+4,true) !== 22050 || v.getUint32(body+8,true) !== 44100 || v.getUint16(body+12,true) !== 2 || v.getUint16(body+14,true) !== 16) return false; fmt = true }
    if (tag === 'data') { if (frames !== -1 || size === 0 || size % 2 !== 0) return false; frames = size/2 }
    offset = end + size%2
  }
  return offset === data.byteLength && fmt && frames > 0 && Math.floor(frames*1000/22050) === memo.duration_ms
}
export async function sha256Bytes(data:BufferSource):Promise<string> { const result = await crypto.subtle.digest('SHA-256',data); return Array.from(new Uint8Array(result),byte=>byte.toString(16).padStart(2,'0')).join('') }
