import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { canonicalEventId } from '../lib/peerReply'
import { validBotMessageRef, type BotMessageRef } from './botMessages'

export type RoutineSchedule = { kind: 'interval'; interval_minutes: number; timezone: string } | { kind: 'weekly'; weekdays: number[]; hour: number; minute: number; timezone: string } | { kind: 'webhook' }
export type Routine = { version: 1; routine_id: string; revision: number; agent_id: number; room_id: number; name: string; instruction: string; enabled: boolean; schedule: RoutineSchedule; next_run_at: string | null; availability: string; webhook?: {configured:boolean;generation:number}|null }
export type RoutineInput = Pick<Routine, 'name'|'instruction'|'enabled'|'schedule'>
export type RoutineRun = { run_id: string; state: 'running'|'completed'|'failed'|'cancelled'|'interrupted'|'unknown'; scheduled_at: string; started_at: string | null; completed_at: string | null; root_ref: BotMessageRef | null; result_ref: BotMessageRef | null; failure_code: string | null; origin: 'scheduled'|'test'|'webhook' }
export type RoutineCommand = { version: 1; operation: 'create'|'test'; agent_id:number; routine: Routine | null; run: RoutineRun | null }
const obj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const pos = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0
const date = (v: unknown) => typeof v === 'string' && Number.isFinite(Date.parse(v))
const maybeDate = (v: unknown) => v === null || date(v)
const owner = () => { const s=useAuthStore.getState(); if (!s.token || !pos(s.user?.id)) throw new Error('Routine owner unavailable'); return {id:s.user.id,epoch:s.epoch} }
const unchanged = (before: ReturnType<typeof owner>) => { const s=useAuthStore.getState(); if(!s.token || s.user?.id!==before.id || s.epoch!==before.epoch) throw new Error('Routine owner changed') }
export function validSchedule(v: unknown): v is RoutineSchedule {
  if (!obj(v)) return false
  if (v.kind==='webhook') return Object.keys(v).length===1
  if (typeof v.timezone !== 'string' || !v.timezone.trim()) return false
  try { new Intl.DateTimeFormat('en',{timeZone:v.timezone}).format(0) } catch { return false }
  if (v.kind === 'interval') return typeof v.interval_minutes === 'number' && Number.isSafeInteger(v.interval_minutes) && v.interval_minutes >= 5 && v.interval_minutes <= 10080
  return v.kind === 'weekly' && Array.isArray(v.weekdays) && v.weekdays.length > 0 && v.weekdays.length <= 7 && v.weekdays.every((x: unknown, i: number, rows: unknown[]) => typeof x === 'number' && Number.isSafeInteger(x) && x >= 0 && x <= 6 && (i === 0 || (rows[i-1] as number) < x))
    && typeof v.hour === 'number' && Number.isSafeInteger(v.hour) && v.hour >= 0 && v.hour <= 23 && typeof v.minute === 'number' && Number.isSafeInteger(v.minute) && v.minute >= 0 && v.minute <= 59
}
export function validRoutine(v: unknown, agent: number): v is Routine {
  return obj(v) && v.version===1 && canonicalEventId(v.routine_id) && pos(v.revision) && v.agent_id===agent && pos(v.room_id) && typeof v.name==='string' && !!v.name.trim() && typeof v.instruction==='string' && !!v.instruction.trim() && typeof v.enabled==='boolean' && validSchedule(v.schedule) && maybeDate(v.next_run_at) && (v.schedule.kind!=='webhook' || v.next_run_at===null && obj(v.webhook) && Object.keys(v.webhook).sort().join(',')==='configured,generation' && typeof v.webhook.configured==='boolean' && typeof v.webhook.generation==='number' && Number.isSafeInteger(v.webhook.generation) && v.webhook.generation>=0) && typeof v.availability==='string'
}
export function validRun(v: unknown, room: number, agent: number): v is RoutineRun {
  return obj(v) && canonicalEventId(v.run_id) && ['running','completed','failed','cancelled','interrupted','unknown'].includes(String(v.state)) && date(v.scheduled_at) && maybeDate(v.started_at) && maybeDate(v.completed_at) && ['scheduled','test','webhook'].includes(String(v.origin)) && (v.failure_code===null || typeof v.failure_code==='string')
    && (v.root_ref===null || validBotMessageRef(v.root_ref) && v.root_ref.room_id===room && v.root_ref.actor_type==='system' && v.root_ref.actor_id===null && v.root_ref.kind==='activity')
    && (v.result_ref===null || validBotMessageRef(v.result_ref) && v.result_ref.room_id===room && v.result_ref.actor_type==='agent' && v.result_ref.actor_id===agent && (v.state==='completed'&&v.result_ref.kind==='message'||v.state==='failed'&&v.result_ref.kind==='failure'))
}
export function sameSchedule(a: RoutineSchedule, b: RoutineSchedule): boolean {
  if(a.kind==='webhook' || b.kind==='webhook')return a.kind===b.kind
  return a.kind === b.kind && a.timezone === b.timezone && (a.kind === 'interval' && b.kind === 'interval' ? a.interval_minutes === b.interval_minutes : a.kind === 'weekly' && b.kind === 'weekly' && a.hour === b.hour && a.minute === b.minute && a.weekdays.length === b.weekdays.length && a.weekdays.every((v,i) => v === b.weekdays[i]))
}
export function matchesRoutine(v: Routine, body: RoutineInput): boolean { return v.name===body.name && v.instruction===body.instruction && v.enabled===body.enabled && sameSchedule(v.schedule,body.schedule) }
export function validCommand(data: unknown, agent: number, operation: 'create'|'test', target: RoutineInput | Routine): data is RoutineCommand {
  if (!obj(data) || data.version!==1 || data.operation!==operation || data.agent_id!==agent) return false
  if (operation==='create') return data.run===null && validRoutine(data.routine,agent) && data.routine.revision===1 && matchesRoutine(data.routine,target as RoutineInput)
  const routine=target as Routine
  return data.routine===null && validRun(data.run,routine.room_id,agent) && data.run.origin==='test' && data.run.root_ref!==null
}
const base = (agent:number) => `/bots/${agent}/routines`
export const routinesApi = {
  issueWebhook: async (routine:Routine,signal?:AbortSignal):Promise<{version:1;routine_id:string;revision:number;generation:number;key:string;url_path:string}> => {
    const auth=owner();if(routine.schedule.kind!=='webhook'||!routine.webhook)throw Error('Webhook unavailable')
    const {data}=await api.post<unknown>(`${base(routine.agent_id)}/${routine.routine_id}/webhook-key`,{expected_revision:routine.revision,expected_generation:routine.webhook.generation},{signal});unchanged(auth)
    if(!obj(data)||Object.keys(data).sort().join(',')!=='generation,key,revision,routine_id,url_path,version'||data.version!==1||data.routine_id!==routine.routine_id||data.revision!==routine.revision||data.generation!==routine.webhook.generation+1||typeof data.key!=='string'||!/^gw_[A-Za-z0-9_-]{43}$/.test(data.key)||data.url_path!==`/api/v1/routine-webhooks/${routine.routine_id}`)throw Error('Webhook receipt unavailable')
    return {version:1,routine_id:routine.routine_id,revision:routine.revision,generation:routine.webhook.generation+1,key:data.key,url_path:data.url_path as string}
  },
  revokeWebhook: async (routine:Routine,signal?:AbortSignal):Promise<void> => {
    const auth=owner();if(routine.schedule.kind!=='webhook'||!routine.webhook)throw Error('Webhook unavailable')
    const {data}=await api.delete<unknown>(`${base(routine.agent_id)}/${routine.routine_id}/webhook-key`,{data:{expected_revision:routine.revision,expected_generation:routine.webhook.generation},signal});unchanged(auth)
    if(!obj(data)||Object.keys(data).sort().join(',')!=='configured,generation,revision,routine_id,version'||data.version!==1||data.routine_id!==routine.routine_id||data.revision!==routine.revision||data.generation!==routine.webhook.generation+1||data.configured!==false)throw Error('Webhook receipt unavailable')
  },
  webhookEvent: async (routine:Routine,key:string,signal?:AbortSignal):Promise<{run_id:string;state:RoutineRun['state']}> => {
    const auth=owner();if(!/^[A-Za-z0-9_-]{1,100}$/.test(key)||/^gw_[A-Za-z0-9_-]{43}$/.test(key))throw Error('Webhook event unavailable');const {data}=await api.get<unknown>(`${base(routine.agent_id)}/${routine.routine_id}/webhook-events/${encodeURIComponent(key)}`,{signal});unchanged(auth)
    if(!obj(data)||Object.keys(data).sort().join(',')!=='accepted,run_id,routine_id,state,version'||data.version!==1||data.routine_id!==routine.routine_id||data.accepted!==true||!canonicalEventId(data.run_id)||!['running','completed','failed','cancelled','interrupted','unknown'].includes(String(data.state)))throw Error('Webhook event unavailable')
    return {run_id:data.run_id,state:data.state as RoutineRun['state']}
  },
  persistence: async (agent:number,id:string):Promise<unknown> => { const auth=owner(),{data}=await api.get<unknown>(`/bots/${agent}/routine-persistence/${id}`);unchanged(auth);return data },
  list: async (agent:number):Promise<Routine[]> => { const auth=owner(),{data}=await api.get<unknown>(base(agent)); unchanged(auth); if(!obj(data) || data.version!==1 || data.agent_id!==agent || !Array.isArray(data.items) || data.items.length>50 || !data.items.every(x=>validRoutine(x,agent)) || new Set(data.items.map(x=>x.routine_id)).size!==data.items.length) throw new Error('Routines unavailable'); return data.items },
  read: async (agent:number,id:string,signal?:AbortSignal):Promise<Routine> => { const auth=owner(),{data}=await api.get<unknown>(`${base(agent)}/${id}`,{signal}); unchanged(auth); if(!validRoutine(data,agent) || data.routine_id!==id) throw new Error('Routine unavailable'); return data },
  create: async (agent:number,body:RoutineInput,key:string):Promise<unknown> => { const auth=owner(),{data}=await api.post<unknown>(base(agent),body,{headers:{'Idempotency-Key':key}}); unchanged(auth); return data },
  update: async (agent:number,id:string,revision:number,body:RoutineInput):Promise<Routine> => { const auth=owner(),{data}=await api.put<unknown>(`${base(agent)}/${id}`,{expected_revision:revision,...body}); unchanged(auth); if(!validRoutine(data,agent) || data.routine_id!==id || data.revision<=revision || !matchesRoutine(data,body)) throw new Error('Routine save unavailable'); return data },
  runs: async (routine:Routine):Promise<RoutineRun[]> => { const auth=owner(),{data}=await api.get<unknown>(`${base(routine.agent_id)}/${routine.routine_id}/runs`); unchanged(auth); if(!obj(data) || data.version!==1 || data.routine_id!==routine.routine_id || !Array.isArray(data.runs) || data.runs.length>20 || !data.runs.every(x=>validRun(x,routine.room_id,routine.agent_id))) throw new Error('Routine history unavailable'); return data.runs },
  run: async (routine:Routine,id:string):Promise<RoutineRun> => { const auth=owner();if(!canonicalEventId(id))throw Error('Routine run unavailable');const {data}=await api.get<unknown>(`${base(routine.agent_id)}/${routine.routine_id}/runs/${id}`);unchanged(auth);if(!obj(data)||data.version!==1||data.routine_id!==routine.routine_id||!validRun(data.run,routine.room_id,routine.agent_id)||data.run.run_id!==id)throw Error('Routine run unavailable');return data.run },
  test: async (routine:Routine,key:string):Promise<unknown> => { const auth=owner(),{data}=await api.post<unknown>(`${base(routine.agent_id)}/${routine.routine_id}/test`,{expected_revision:routine.revision},{headers:{'Idempotency-Key':key}}); unchanged(auth); return data },
  command: async (agent:number,key:string):Promise<unknown> => { const auth=owner(),{data}=await api.get<unknown>(`${base(agent)}/commands/${key}`); unchanged(auth); return data },
}
