import api from '../lib/api'

export type RoutineReference = {agent_id:number;routine_id:string;revision:number}
export const sameRoutineReference=(a:RoutineReference|null|undefined,b:unknown)=>{if(!a)return b==null;if(!b||typeof b!=='object'||Array.isArray(b))return false;const x=b as Record<string,unknown>;return Object.keys(x).sort().join(',')==='agent_id,revision,routine_id'&&x.agent_id===a.agent_id&&x.routine_id===a.routine_id&&x.revision===a.revision}
export type PageTarget = {result_id:string;version:number}
export type ReplyReference = { event_id: string; seq: number }
export const sameReply = (a: ReplyReference | null | undefined, b: ReplyReference | null | undefined) => !a && !b || !!a && !!b && a.event_id === b.event_id && a.seq === b.seq
export const validReply = (value: unknown): value is ReplyReference => { const r = value as ReplyReference | null; return !!r && typeof r === 'object' && typeof r.event_id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(r.event_id) && Number.isSafeInteger(r.seq) && r.seq > 0 }

export interface RoomEvent {
  event_id: string
  device_id: string | null
  room_id: number
  seq: number
  actor_type: 'user' | 'agent' | 'tool' | 'system'
  actor_id: number | null
  actor_name_snapshot: string
  kind: 'message' | 'tool_result' | 'failure' | 'summary' | 'activity'
  payload: Record<string, unknown>
  created_at: string
  idempotency_key: string | null
}

export interface ReplyThreadPage { version: 1; root: ReplyReference; upper_seq: number; items: RoomEvent[]; has_more: boolean; next_after_seq: number | null }
export interface ReplyThreadItem { event: RoomEvent; parent: ReplyReference; depth: number }
export interface DescendantThreadPage { version: 2; root: ReplyReference; upper_seq: number; items: ReplyThreadItem[]; has_more: boolean; next_after_seq: number | null; depth_limited: boolean }

export const eventsApi = {
  list: async (roomId: number, afterSeq: number, limit = 100) =>
    (await api.get<RoomEvent[]>(`/conversations/${roomId}/events`, { params: { after_seq: afterSeq, limit } })).data,
  replies: async (roomId: number, root: ReplyReference, cursor?: { afterSeq: number; upperSeq: number }, limit = 20) =>
    (await api.get<ReplyThreadPage>(`/conversations/${roomId}/events/${root.event_id}/replies`, { params: { root_seq: root.seq, limit, ...(cursor ? { after_seq: cursor.afterSeq, upper_seq: cursor.upperSeq } : {}) } })).data,
  thread: async (roomId: number, root: ReplyReference, cursor?: { afterSeq: number; upperSeq: number }, limit = 20) =>
    (await api.get<DescendantThreadPage>(`/conversations/${roomId}/events/${root.event_id}/thread`, { params: { root_seq: root.seq, limit, ...(cursor ? { after_seq: cursor.afterSeq, upper_seq: cursor.upperSeq } : {}) } })).data,
  sendPage: async (roomId:number, content:string, target:PageTarget, key:string, selection?:{index:number;type:'heading'|'markdown';text:string;sha256:string}) =>
    (await api.post<RoomEvent>(`/conversations/${roomId}/events`,{content,output_kind:'auto',reply_to:null,page_target:target,...(selection?{page_selection:selection}:{})},{headers:{'Idempotency-Key':key}})).data,
  resolveRoutine: async (roomId:number,content:string,key:string,routineRef:RoutineReference) => {
    const {data}=await api.post<unknown>(`/conversations/${roomId}/routine-reference/resolve`,{content,output_kind:'auto',reply_to:null,routine_ref:routineRef},{headers:{'Idempotency-Key':key}})
    if(!data||typeof data!=='object'||Array.isArray(data))throw Error('Routine resolution unavailable')
    const r=data as Record<string,unknown>
    if(r.version!==1||!['accepted','pending','rejected'].includes(String(r.state)))throw Error('Routine resolution unavailable')
    if(r.state==='accepted'&&!validReply({event_id:r.event_id,seq:r.seq}))throw Error('Routine resolution unavailable')
    if(r.state==='rejected'&&(typeof r.code!=='string'||!['routine_not_found','routine_agent_not_found','routine_reference_changed','routine_reference_room_changed'].includes(r.code)))throw Error('Routine resolution unavailable')
    return r as {version:1;state:'accepted'|'pending'|'rejected';event_id?:string;seq?:number;code?:string}
  },
  pageReceipt: async (roomId:number,key:string) =>
    (await api.get<RoomEvent>(`/conversations/${roomId}/page-message/receipt`,{headers:{'Idempotency-Key':key}})).data,
  send: async (roomId: number, content: string, idempotencyKey: string, outputKind: 'auto'|'text'|'image_generation'|'speech_generation'='auto', replyTo: ReplyReference | null = null, routineRef: RoutineReference | null = null) =>
    (await api.post<RoomEvent>(`/conversations/${roomId}/events`, { content, output_kind:outputKind, reply_to: replyTo, ...(routineRef?{routine_ref:routineRef}:{}) },
      { headers: { 'Idempotency-Key': idempotencyKey } })).data,
}
