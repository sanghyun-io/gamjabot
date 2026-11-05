import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { projectsApi } from './projects'
import { connectionsApi } from './connections'

export type TeamProfile = { profile_id: string; display_name: string; base_url: string; issuer: string; key_fingerprint: string; ready: boolean }
export type TeamSession = { state: 'disconnected' | 'authenticated' | 'team_ready' | 'team_session_expired'; team_id?: string | null; account_id?: string; session_generation?: string; profile_id?: string; issuer?: string; shared_execution_available: boolean; shared_text_execution_available?: boolean; shared_private_memory_available?: boolean }
export type TeamCredentials = { profile_id: string; username: string; password: string }
export type TeamActivation = { invite_code: string; username: string; password: string }

/** Local authenticated gateway only. Remote team credentials are never exposed to the UI. */
export const teamService = {
  profiles: async (): Promise<TeamProfile[]> => (await api.get('/team-connection/profiles')).data.profiles,
  status: async (): Promise<TeamSession> => (await api.get('/team-connection')).data,
  login: async (input: TeamCredentials): Promise<TeamSession> => { const {data}=await api.post('/team-connection', input); window.dispatchEvent(new Event('gamja-team-session-changed')); return data },
  activate: async (input: TeamActivation): Promise<void> => { await api.post('/team-connection/activate', input) },
  logout: async (session_generation: string): Promise<{state: 'disconnected'; remote_session_revoked?: boolean}> => { const {data}=await api.delete('/team-connection', {params:{session_generation}}); window.dispatchEvent(new Event('gamja-team-session-changed')); return data },
}

export type TeamCommand = 'list_teams' | 'list_members' | 'list_invitations' | 'list_sent_invitations' | 'list_publications' | 'get_invite' | 'get_publication' | 'create_team' | 'invite_existing' | 'invite_new' | 'accept_invite' | 'revoke_invite' | 'revoke_member' | 'put_publication' | 'revoke_publication'
export type TeamReceipt = { operation_id: string; team_id: string; state: 'committed'; kind: string; result: Record<string, unknown> }
export const teamCommands = {
  command: async (session_generation: string, operation: TeamCommand, payload: Record<string, unknown>, operation_id?: string): Promise<Record<string, unknown>> => {
    const { data } = await api.post('/team-connection/command', { session_generation, operation, payload, ...(operation_id ? { operation_id } : {}) })
    if (data.session_generation !== session_generation || !data.result || typeof data.result !== 'object') throw new Error('Team session changed')
    return data.result
  },
  select: async (session_generation: string, team_id: string): Promise<TeamSession> => { const {data}=await api.post('/team-connection/select', {session_generation,team_id}); window.dispatchEvent(new Event('gamja-team-session-changed')); return data },
  receipt: async (session_generation: string, operation_id: string): Promise<TeamReceipt> => {
    const { data } = await api.get(`/team-connection/operations/${encodeURIComponent(operation_id)}`, { params: { session_generation } })
    if (data.session_generation !== session_generation || data.receipt?.operation_id !== operation_id) throw new Error('Team operation unavailable')
    return data.receipt
  },
}

export type PendingTeamOperation = {
  owner: number; profile_id: string; issuer: string; fingerprint: string
  team: string | null; account: string; id: string; kind: string; target?: string; result_team?: string; created_at: number
}
export type PendingTeamStore = {
  read(): PendingTeamOperation[]
  add(value: PendingTeamOperation): void
  remove(value: PendingTeamOperation): void
}
const PENDING_KEY = 'gamja.team.pending-operations.v1'
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const validKinds = new Set(['team.create','invite.existing','invite.enrollment','invite.accept','invite.revoke','member.revoke','publication.put','publication.revoke'])
function validPointer(x: unknown): x is PendingTeamOperation {
  if (!x || typeof x !== 'object' || Array.isArray(x)) return false
  const r = x as Record<string,unknown>
  const allowed = ['owner','profile_id','issuer','fingerprint','team','account','id','kind','target','result_team','created_at']
  return Object.keys(r).every(k => allowed.includes(k)) && Number.isSafeInteger(r.owner) && (r.owner as number)>0
    && typeof r.profile_id === 'string' && r.profile_id.length > 0 && r.profile_id.length <= 64
    && typeof r.issuer === 'string' && r.issuer.length > 0 && r.issuer.length <= 1024
    && typeof r.fingerprint === 'string' && r.fingerprint.length > 0 && r.fingerprint.length <= 256
    && (r.team === null || typeof r.team === 'string' && uuid.test(r.team)) && typeof r.account === 'string' && uuid.test(r.account)
    && typeof r.id === 'string' && uuid.test(r.id) && typeof r.kind === 'string' && validKinds.has(r.kind)
    && (r.target === undefined || typeof r.target === 'string' && uuid.test(r.target))
    && (r.result_team === undefined || typeof r.result_team === 'string' && uuid.test(r.result_team))
    && typeof r.created_at === 'number' && Number.isSafeInteger(r.created_at) && r.created_at > 0
}
/** Stores non-secret recovery pointers only. Corrupt or unavailable storage blocks mutations. */
export const pendingTeamStore: PendingTeamStore = {
  read() {
    const raw = window.localStorage.getItem(PENDING_KEY)
    if (raw === null) return []
    if (raw.length > 256000) throw new Error('Team recovery storage unavailable')
    const value: unknown = JSON.parse(raw)
    if (!Array.isArray(value) || value.length > 200 || !value.every(validPointer) || new Set(value.map(x=>x.id)).size !== value.length) throw new Error('Team recovery storage unavailable')
    return value
  },
  add(value) {
    if (!validPointer(value)) throw new Error('Team recovery storage unavailable')
    const all = this.read()
    if (all.length >= 200 || all.some(x=>x.id===value.id)) throw new Error('Team recovery storage unavailable')
    window.localStorage.setItem(PENDING_KEY, JSON.stringify([...all,value]))
    if (!this.read().some(x=>x.id===value.id)) throw new Error('Team recovery storage unavailable')
  },
  remove(value) {
    const all = this.read()
    if (!all.some(x=>x.id===value.id)) throw new Error('Team recovery storage unavailable')
    window.localStorage.setItem(PENDING_KEY, JSON.stringify(all.filter(x=>x.id!==value.id)))
    if (this.read().some(x=>x.id===value.id)) throw new Error('Team recovery storage unavailable')
  },
}

export type SharedRoomSelection = { session_generation:string; publication_id:string; project_id:number; connection_id:number; model_id:string }
export type SharedRoom = SharedRoomSelection & { state:'ready'; room_id:number; agent_id:number; team_id:string; owner_id:number; issuer:string; account_id:string; text_only:true }
export type SharedRoomOptions = { projects:Array<{id:number;name:string}>; connections:Array<{id:number;label:string;model_id:string}> }

function sameOwner(owner:number | undefined, epoch:number) {
  const auth=useAuthStore.getState()
  if (!owner || auth.user?.id!==owner || auth.epoch!==epoch) throw new Error('Owner changed')
}
function checkedSharedRoom(value:unknown, scope:{team:string;publication:string;generation:string}):SharedRoom {
  const r=value as Partial<SharedRoom> | null
  if (!r || r.owner_id!==useAuthStore.getState().user?.id || typeof r.issuer!=='string' || typeof r.account_id!=='string' || r.state!=='ready' || r.publication_id!==scope.publication || r.team_id!==scope.team || r.session_generation!==scope.generation || r.text_only!==true || !Number.isSafeInteger(r.room_id) || (r.room_id || 0)<=0 || !Number.isSafeInteger(r.agent_id) || (r.agent_id || 0)<=0 || !Number.isSafeInteger(r.project_id) || (r.project_id || 0)<=0 || !Number.isSafeInteger(r.connection_id) || (r.connection_id || 0)<=0 || typeof r.model_id!=='string' || !r.model_id.trim()) throw new Error('Shared room unavailable')
  return r as SharedRoom
}
export const sharedRoomApi = {
  options:async():Promise<SharedRoomOptions>=>{
    const auth=useAuthStore.getState(),owner=auth.user?.id,epoch=auth.epoch
    const [projects,connections]=await Promise.all([projectsApi.list(),connectionsApi.list()]);sameOwner(owner,epoch)
    return {projects:projects.filter(x=>x.user_id===owner && x.is_active).map(x=>({id:x.id,name:x.name})),connections:connections.filter(x=>x.owner_id===owner && x.enabled && !!x.default_model_id?.trim()).map(x=>({id:x.id,label:x.label,model_id:x.default_model_id}))}
  },
  open:async(team:string,input:SharedRoomSelection):Promise<SharedRoom>=>{
    const auth=useAuthStore.getState(),owner=auth.user?.id,epoch=auth.epoch
    const {data}=await api.post('/team-connection/shared-rooms',input);sameOwner(owner,epoch)
    return checkedSharedRoom(data,{team,publication:input.publication_id,generation:input.session_generation})
  },
  read:async(team:string,publication:string,generation:string):Promise<SharedRoom>=>{
    const auth=useAuthStore.getState(),owner=auth.user?.id,epoch=auth.epoch
    const {data}=await api.get(`/team-connection/shared-rooms/${encodeURIComponent(publication)}`,{params:{session_generation:generation}});sameOwner(owner,epoch)
    return checkedSharedRoom(data,{team,publication,generation})
  },
}

export type SharedRoomPointer = { owner:number; profile_id:string; issuer:string; fingerprint:string; account:string; team:string; publication:string; project_id:number; connection_id:number; model_id:string }
export type SharedRoomPendingStore = {read():SharedRoomPointer[]; add(value:SharedRoomPointer):void; remove(value:SharedRoomPointer):void}
const SHARED_PENDING_KEY='gamja.team.shared-room-pending.v1'
function validSharedPointer(value:unknown):value is SharedRoomPointer {
  if(!value || typeof value!=='object' || Array.isArray(value)) return false
  const r=value as Record<string,unknown>, fields=['owner','profile_id','issuer','fingerprint','account','team','publication','project_id','connection_id','model_id']
  return Object.keys(r).length===fields.length && Object.keys(r).every(k=>fields.includes(k)) && typeof r.owner==='number' && Number.isSafeInteger(r.owner) && r.owner>0 && typeof r.profile_id==='string' && r.profile_id.length>0 && r.profile_id.length<=64 && typeof r.issuer==='string' && r.issuer.length>0 && r.issuer.length<=1024 && typeof r.fingerprint==='string' && r.fingerprint.length>0 && r.fingerprint.length<=256 && typeof r.account==='string' && uuid.test(r.account) && typeof r.team==='string' && uuid.test(r.team) && typeof r.publication==='string' && uuid.test(r.publication) && typeof r.project_id==='number' && Number.isSafeInteger(r.project_id) && r.project_id>0 && typeof r.connection_id==='number' && Number.isSafeInteger(r.connection_id) && r.connection_id>0 && typeof r.model_id==='string' && r.model_id.trim().length>0 && r.model_id.length<=256
}
const sharedKey=(p:SharedRoomPointer)=>JSON.stringify([p.owner,p.profile_id,p.issuer,p.fingerprint,p.account,p.team,p.publication])
export const sharedRoomPendingStore:SharedRoomPendingStore={
  read(){const raw=window.localStorage.getItem(SHARED_PENDING_KEY);if(raw===null)return [];if(raw.length>256000)throw new Error('Recovery unavailable');const value:unknown=JSON.parse(raw);if(!Array.isArray(value)||value.length>200||!value.every(validSharedPointer)||new Set(value.map(sharedKey)).size!==value.length)throw new Error('Recovery unavailable');return value},
  add(value){if(!validSharedPointer(value))throw new Error('Recovery unavailable');const all=this.read(), key=sharedKey(value);if(all.length>=200||all.some(p=>sharedKey(p)===key))throw new Error('Recovery unavailable');window.localStorage.setItem(SHARED_PENDING_KEY,JSON.stringify([...all,value]));if(!this.read().some(p=>sharedKey(p)===key))throw new Error('Recovery unavailable')},
  remove(value){const key=sharedKey(value),all=this.read();if(!all.some(p=>sharedKey(p)===key))throw new Error('Recovery unavailable');window.localStorage.setItem(SHARED_PENDING_KEY,JSON.stringify(all.filter(p=>sharedKey(p)!==key)));if(this.read().some(p=>sharedKey(p)===key))throw new Error('Recovery unavailable')},
}

export type SharedRoomBinding = {kind:'ordinary'} | {kind:'shared';state:'unavailable';text_only:true} | {kind:'shared';state:'ready';text_only:true;room_id:number;agent_id:number;team_id:string;publication_id:string;session_generation:string;account_id:string;issuer:string;shared_private_memory_available:boolean;profile_id?:string;key_fingerprint?:string}
export async function readSharedRoomBinding(roomId:number):Promise<SharedRoomBinding>{
  const auth=useAuthStore.getState(),owner=auth.user?.id,epoch=auth.epoch
  const {data:r}=await api.get<unknown>(`/conversations/${roomId}/shared-binding`);sameOwner(owner,epoch)
  if(!r||typeof r!=='object'||Array.isArray(r))throw new Error('Room availability unknown')
  const value=r as Record<string,unknown>
  if(value.kind==='ordinary')return {kind:'ordinary'}
  if(value.kind==='shared'&&value.state==='unavailable'&&value.text_only===true)return {kind:'shared',state:'unavailable',text_only:true}
  if(value.kind==='shared'&&value.state==='ready'&&value.text_only===true&&value.room_id===roomId&&typeof value.agent_id==='number'&&Number.isSafeInteger(value.agent_id)&&value.agent_id>0&&typeof value.team_id==='string'&&uuid.test(value.team_id)&&typeof value.publication_id==='string'&&uuid.test(value.publication_id)&&typeof value.session_generation==='string'&&uuid.test(value.session_generation)&&typeof value.account_id==='string'&&uuid.test(value.account_id)&&typeof value.issuer==='string'&&value.issuer.length>0&&typeof value.shared_private_memory_available==='boolean'&&(value.profile_id===undefined&&value.key_fingerprint===undefined||typeof value.profile_id==='string'&&value.profile_id.length>0&&typeof value.key_fingerprint==='string'&&value.key_fingerprint.length>0))return value as SharedRoomBinding
  throw new Error('Room availability unknown')
}

export type SharedPrivateMemoryScope = {room_id:number;publication_id:string;team_id:string;account_id:string;issuer:string;session_generation:string}
export type SharedPrivateMemoryResult = SharedPrivateMemoryScope & {version:1;scope_revision:number;entries:Array<{memory_id:string;category:'fact'|'preference';content:string;revision:number}>;truncated:false}
function memoryScopeMatches(binding:SharedRoomBinding,scope:SharedPrivateMemoryScope){
  return binding.kind==='shared'&&binding.state==='ready'&&binding.shared_private_memory_available===true&&binding.room_id===scope.room_id&&binding.publication_id===scope.publication_id&&binding.team_id===scope.team_id&&binding.account_id===scope.account_id&&binding.issuer===scope.issuer&&binding.session_generation===scope.session_generation
}
export async function readSharedPrivateMemory(scope:SharedPrivateMemoryScope):Promise<SharedPrivateMemoryResult>{
  const auth=useAuthStore.getState(),owner=auth.user?.id,epoch=auth.epoch
  if(!memoryScopeMatches(await readSharedRoomBinding(scope.room_id),scope))throw new Error('Shared memory scope changed')
  const {data}=await api.get<unknown>(`/conversations/${scope.room_id}/shared-private-memory`,{params:{session_generation:scope.session_generation}})
  sameOwner(owner,epoch)
  if(!data||typeof data!=='object'||Array.isArray(data))throw new Error('Shared memory unavailable')
  const r=data as Record<string,unknown>
  if(r.version!==1||r.room_id!==scope.room_id||r.publication_id!==scope.publication_id||r.team_id!==scope.team_id||r.account_id!==scope.account_id||r.issuer!==scope.issuer||r.session_generation!==scope.session_generation||typeof r.scope_revision!=='number'||!Number.isSafeInteger(r.scope_revision)||r.scope_revision<0||r.truncated!==false||!Array.isArray(r.entries)||r.entries.length>64)throw new Error('Shared memory unavailable')
  let chars=0,bytes=0
  for(const entry of r.entries){
    if(!entry||typeof entry!=='object'||Array.isArray(entry))throw new Error('Shared memory unavailable')
    const x=entry as Record<string,unknown>
    if(typeof x.memory_id!=='string'||!uuid.test(x.memory_id)||(x.category!=='fact'&&x.category!=='preference')||typeof x.content!=='string'||typeof x.revision!=='number'||!Number.isSafeInteger(x.revision)||x.revision<1)throw new Error('Shared memory unavailable')
    const length=Array.from(x.content).length;if(length>2000)throw new Error('Shared memory unavailable');chars+=length;bytes+=new TextEncoder().encode(x.content).length
  }
  if(chars>8000||bytes>32000||new Set(r.entries.map(x=>(x as {memory_id:string}).memory_id)).size!==r.entries.length)throw new Error('Shared memory unavailable')
  if(!memoryScopeMatches(await readSharedRoomBinding(scope.room_id),scope))throw new Error('Shared memory scope changed')
  sameOwner(owner,epoch)
  return r as SharedPrivateMemoryResult
}
