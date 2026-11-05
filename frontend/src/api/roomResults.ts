import api from '../lib/api'
import type { RoomEvent } from './events'
import type { VoiceMemoRef } from './voiceMemos'

export type ResultKind = 'text' | 'link' | 'file' | 'page'
export type ResultState = 'draft' | 'reviewing' | 'final'
export interface ResultSummary {
  result_id: string; room_id: number; version: number; kind: ResultKind; title: string
  author_agent_id: number; author_name_snapshot: string; state: ResultState; state_revision: number
  created_at: string; updated_at?: string; linked_opinion_count: number
}
export interface ResultEvidence { event_id?: string; seq?: number; url?: string; verification?: 'unchecked' }
export interface GeneratedFileRef { adapter:'generated_file'; action_id:string; event_seq:number; version:1; ref:string; filename:string; size_bytes:number; sha256:string; media_type:'text/plain; charset=utf-8'; capture_kind:'approved_write_payload' }
export interface ResultDetail extends ResultSummary {
  current_version: number; source: { event_id: string; seq: number; href: string }
  execution: { trigger_event_id: string | null; round: number | null; generation: number | null
    model_use: { connection_id: number; model_id: string; provider_kind: string; watermark_seq: number } | null }
  content: string; file_ref: { adapter: 'screenshot'; action_id: string; event_seq: number
    image_width: number | null; image_height: number | null } | GeneratedFileRef | VoiceMemoRef | null
  file_status: 'unchecked' | 'removed' | 'unavailable' | null; evidence: ResultEvidence[]
  evidence_status: 'none' | 'provided_unverified'; review_support: 'source_reply_only' | 'unsupported'
  link_report: 'unchecked' | 'reported_bad' | null; state_owner_id: number | null; state_changed_at: string | null
  link_report_owner_id: number | null; link_reported_at: string | null
}
export interface ResultOpinion {
  review_id: string; version: number; source_event_id: string; review_event_id: string; review_seq: number
  reviewer_agent_id: number; reviewer_name_snapshot: string; linked_by_owner_id: number; created_at: string
  coverage: 'source_reply_only'; href: string
}
export interface ResultSave {
  source_event_id: string; kind: ResultKind; title: string; url?: string
  evidence: Array<{ event_id: string } | { url: string }>
}
export interface ResultCommand {
  room_id: number; result_id: string; result_version: number; state_revision: number
  state: ResultState; operation: string; replayed: boolean; activity_event_id: string; activity_seq: number
  result_review_id?: string; review_event_id?: string; link_report?: 'unchecked' | 'reported_bad'
}
export interface PageRenameReceipt { version:1;room_id:number;result_id:string;base_version:number;page_version:number;title_sha256:string;event_id:string;seq:number;replayed:boolean }
export interface RevisionRef { room_id:number;event_id:string;seq:number;actor_type:'user'|'agent';actor_id:number;kind:'message';href:string }
export interface RevisionStatus { room_id:number;result_id:string;expected_current_version:number;author_agent_id:number;instruction:string;root_ref:RevisionRef;state:'pending'|'ready'|'completed'|'failed'|'cancelled'|'conflict'|'unknown';replayed?:boolean;source_event_id?:string;source_ref?:RevisionRef;result_ref?:{room_id:number;result_id:string;version:number;href:string;source_event_id:string;source_ref:RevisionRef}|null }
const base = (room: number) => `/conversations/${room}/results`
const at = (room: number, id: string, version: number) => `${base(room)}/${id}/versions/${version}`
const headers = (key: string) => ({ headers: { 'Idempotency-Key': key } })
export const roomResultsApi = {
  renamePage: async (room:number,id:string,version:number,title:string,key:string) => (await api.post<PageRenameReceipt>(`${base(room)}/${id}/rename`,{expected_current_version:version,title},headers(key))).data,
  renameReceipt: async (room:number,id:string,key:string) => (await api.get<PageRenameReceipt>(`${base(room)}/${id}/rename/receipt`,headers(key))).data,
  pageCurrent: async (room:number,id:string) => (await api.get<ResultDetail>(`${base(room)}/${id}/current`)).data,
  pageConversation: async (room:number,id:string,cursor?:{afterSeq:number;upperSeq:number}) => (await api.get<{version:1;room_id:number;result_id:string;upper_seq:number;items:RoomEvent[];root_states:Array<{event_id:string;state:string}>;next_after_seq:number|null}>(`${base(room)}/${id}/conversation`,{params:{limit:50,...(cursor?{after_seq:cursor.afterSeq,upper_seq:cursor.upperSeq}:{})}})).data,
  revisionActive: async (room:number,id:string) => (await api.get<{room_id:number;result_id:string;base_version:number;revision:RevisionStatus|null}>(`${base(room)}/${id}/revisions/active`)).data,
  revisionStart: async (room:number,id:string,version:number,instruction:string,key:string) => (await api.post<RevisionStatus>(`${base(room)}/${id}/revisions`,{expected_current_version:version,instruction},headers(key))).data,
  revisionReceipt: async (room:number,id:string,key:string) => (await api.get<RevisionStatus>(`${base(room)}/${id}/revisions/receipt`,headers(key))).data,
  revisionStatus: async (room:number,id:string,root:string) => (await api.get<RevisionStatus>(`${base(room)}/${id}/revisions/${root}`)).data,
  revisionFinalize: async (room:number,id:string,root:string) => (await api.post<RevisionStatus>(`${base(room)}/${id}/revisions/${root}/finalize`)).data,
  list: async (room: number, after?: string) => (await api.get<{ room_id: number; items: ResultSummary[]; next_cursor: string | null }>(base(room), { params: { after, limit: 50 } })).data,
  versions: async (room: number, id: string, before?: number) => (await api.get<{ room_id: number; result_id: string; current_version: number; items: ResultSummary[]; next_before_version: number | null }>(`${base(room)}/${id}/versions`, { params: { before, limit: 50 } })).data,
  detail: async (room: number, id: string, version: number, signal?:AbortSignal) => (signal ? await api.get<ResultDetail>(at(room,id,version),{signal}) : await api.get<ResultDetail>(at(room,id,version))).data,
  generatedFile: async (room:number,id:string,version:number,signal:AbortSignal) => api.get<ArrayBuffer>(`${at(room,id,version)}/file`,{responseType:'arraybuffer',signal}),
  removeGeneratedFile: async (room:number,id:string,version:number,signal:AbortSignal) => (await api.delete<{removed:boolean}>(`${at(room,id,version)}/file`,{signal})).data,
  reviews: async (room: number, id: string, version: number, after?: string) => (await api.get<{ items: ResultOpinion[]; next_cursor: string | null }>(`${at(room, id, version)}/reviews`, { params: { after, limit: 50 } })).data,
  save: async (room: number, body: ResultSave, key: string) => (await api.post<ResultCommand>(base(room), body, headers(key))).data,
  version: async (room: number, id: string, body: ResultSave & { expected_current_version: number }, key: string) => (await api.post<ResultCommand>(`${base(room)}/${id}/versions`, body, headers(key))).data,
  state: async (room: number, id: string, version: number, body: { state: ResultState; expected_state_revision: number }, key: string) => (await api.post<ResultCommand>(`${at(room, id, version)}/state`, body, headers(key))).data,
  review: async (room: number, id: string, version: number, reviewEventId: string, key: string) => (await api.post<ResultCommand>(`${at(room, id, version)}/reviews`, { review_event_id: reviewEventId }, headers(key))).data,
  linkReport: async (room: number, id: string, version: number, body: { report: 'unchecked' | 'reported_bad'; expected_state_revision: number }, key: string) => (await api.post<ResultCommand>(`${at(room, id, version)}/link-report`, body, headers(key))).data,
}
