import { invoke, isTauri } from '@tauri-apps/api/core'
import { validReply, type RoomEvent } from '../api/events'
import api from './api'
import { voiceCallsApi, type VoiceCallRecord, type VoiceCallTurn } from '../api/voiceCalls'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from './directoryViewMemory'
import { readRoomStop, roomSendAllowed } from './roomStopWorkspace'
import { blocksNewMessage, pendingRoomMessages } from './pendingRoomMessages'

export type CallTarget = { ownerId: number; epoch: number; roomId: number; agentId: number; title: string; generation: number; canBegin: () => boolean }
export type CallPhase = 'idle' | 'confirm' | 'creating' | 'preparing' | 'listening' | 'stopping' | 'responding' | 'reconnecting' | 'speaking' | 'muted' | 'approval' | 'unknown' | 'error' | 'ended'
export type CallVoice = { id: string; name: string; language: string }
export type CallView = { phase: CallPhase; target: CallTarget | null; proposed: CallTarget | null; call: VoiceCallRecord | null; muted: boolean; notice: string; voices: CallVoice[]; voice: string; language: string; rate: number }
const initial: CallView = { phase: 'idle', target: null, proposed: null, call: null, muted: false, notice: '', voices: [], voice: '', language: '', rate: 0.5 }
const uuid = (s: unknown): s is string => typeof s === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(s)
// Only network failures and explicit gateway outages qualify; cancellation and application errors do not.
const transient = (error:unknown) => { const e=error as {isAxiosError?:boolean;code?:string;response?:{status?:number}} | null; return e?.isAxiosError===true && (e.response ? [502,503,504].includes(e.response.status || 0) : ['ERR_NETWORK','ECONNABORTED','ETIMEDOUT'].includes(e.code || '')) }
const sameRef = (a:{event_id:string;seq:number},b:{event_id:string;seq:number}) => a.event_id===b.event_id && a.seq===b.seq
function sameRecordedTurns(before:VoiceCallRecord,after:VoiceCallRecord) {
  return before.turns.length===after.turns.length && before.turns.every((old,index)=>{ const next=after.turns[index]; if(!next || old.client_turn_id!==next.client_turn_id || old.text!==next.text || !sameRef(old.user_event_ref,next.user_event_ref)) return false
    if(old.reply_event_ref && (!next.reply_event_ref || !sameRef(old.reply_event_ref,next.reply_event_ref) || old.reply_text!==next.reply_text)) return false
    if(['completed','failed','cancelled','interrupted'].includes(old.phase) && old.phase!==next.phase) return false
    return Array.isArray(old.replies) && Array.isArray(next.replies) && old.replies.length<=next.replies.length && old.replies.every((reply,i)=>!!next.replies[i] && sameRef(reply.event_ref,next.replies[i].event_ref) && reply.text===next.replies[i].text)
  })
}
const sameOwner = (t: CallTarget) => { const a = useAuthStore.getState(); return !!a.token && a.user?.id === t.ownerId && a.epoch === t.epoch && directoryViewGeneration() === t.generation }
const validCall = (c: VoiceCallRecord, t: CallTarget, id?: string) => !!c && uuid(c.id) && (!id || c.id === id) && c.owner_id === t.ownerId && c.room_id === t.roomId && c.agent_id === t.agentId && ['active', 'ended', 'interrupted'].includes(c.status) && typeof c.started_at === 'string' && Array.isArray(c.turns) && c.turns.every(x => uuid(x.client_turn_id) && typeof x.text === 'string' && validReply(x.user_event_ref) && (x.reply_event_ref === null || validReply(x.reply_event_ref)))
export type CallTransport = {
  available(): Promise<boolean>; voices(): Promise<CallVoice[]>; checkResumePermission(): Promise<'authorized'|'permission_needed'|'unknown'>; listen(token: string, language: string): Promise<void>; resumeListen(token: string, language: string): Promise<void>; listenState(token: string): Promise<{ phase: string; text: string }>; cancelListen(token: string): Promise<void>
  speak(token: string, text: string, voice: string, rate: number): Promise<string>; speakState(token: string): Promise<string>; cancelSpeak(token: string): Promise<void>
}
export const nativeCallTransport: CallTransport = {
  async available() { return isTauri() && await invoke<string>('voice_transport') === 'native' },
  async voices() { return invoke<CallVoice[]>('call_speech', { input: { action: 'voices' } }) },
  async checkResumePermission() { const token=crypto.randomUUID();let state:'authorized'|'permission_needed'|'unknown'='unknown';try { await invoke('voice_action',{action:'check_call',token}); const value=await invoke<{phase:string}>('voice_snapshot',{token}); if(value.phase==='authorized'||value.phase==='permission_needed')state=value.phase } catch { state='unknown' } finally { try { await invoke('voice_action',{action:'cancel',token}) } catch { state='unknown' } } return state },
  async listen(token, language) { await invoke('voice_action', { action: 'start_call', token, language }) },
  async resumeListen(token, language) { await invoke('voice_action', { action: 'resume_call', token, language }) },
  async listenState(token) { return invoke('voice_snapshot', { token }) },
  async cancelListen(token) { await invoke('voice_action', { action: 'cancel', token }) },
  async speak(token, text, voice, rate) { return (await invoke<{ phase: string }>('call_speech', { input: { action: 'start', token, text, voice, rate } })).phase },
  async speakState(token) { return (await invoke<{ phase: string }>('call_speech', { input: { action: 'snapshot', token } })).phase },
  async cancelSpeak(token) { await invoke('call_speech', { input: { action: 'cancel', token } }) },
}
export interface VoiceCallUIController { subscribe: (f: () => void) => () => void; snapshot: () => CallView; confirm: () => void; dismiss: () => void; hangup: () => void; mute: () => void; resume: () => void; refresh: () => void; settings: (v: Partial<Pick<CallView, 'voice' | 'language' | 'rate'>>) => void }
export class VoiceCallController implements VoiceCallUIController {
  private view = initial; private listeners = new Set<() => void>(); private generation = 0; private mic: string | null = null; private speech: string | null = null; private timer: ReturnType<typeof setTimeout> | null = null; private heartbeat: ReturnType<typeof setTimeout> | null = null; private starts = new Set<Promise<unknown>>(); private silencing: Promise<void> = Promise.resolve(); private sending = false; private opening = false; private checking = false; private pendingCreate: { id: string; target: CallTarget } | null = null; private audioSafe = true; private stopRevision = 0; private leaseSafeUntil = 0; private leaseWallUntil = 0; private reconnectUntil = 0; private reconnectWallUntil = 0; private autoResumeGuard = false; private reconnectAbort: AbortController | null = null; private waitingTurn: string | null = null; private spokenTurn: string | null = null; private speakingTurn: string | null = null; private unknownTurn: { id: string; text: string } | null = null
  private transport: CallTransport
  private calls: typeof voiceCallsApi
  constructor(transport: CallTransport = nativeCallTransport, calls: typeof voiceCallsApi = voiceCallsApi) { this.transport = transport; this.calls = calls }
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn) } }
  snapshot = () => this.view
  private set(update: Partial<CallView>) { this.view = { ...this.view, ...update }; this.listeners.forEach(fn => fn()) }
  private current(gen: number, target: CallTarget) { return gen === this.generation && sameOwner(target) && this.view.target === target }
  private later(fn: () => void, ms: number) { if (this.timer) clearTimeout(this.timer); this.timer = setTimeout(fn, ms) }
  private track<T>(promise: Promise<T>) { this.starts.add(promise); void promise.then(() => this.starts.delete(promise), () => this.starts.delete(promise)); return promise }
  private silence() { const mic = this.mic, speech = this.speech, pending = [...this.starts]; this.mic = null; this.speech = null; this.silencing = this.silencing.then(async () => { await Promise.allSettled(pending); if (mic) await this.transport.cancelListen(mic).catch(() => { this.audioSafe = false }); if (speech) await this.transport.cancelSpeak(speech).catch(() => { this.audioSafe = false }) }); return this.silencing }

  offer(target: CallTarget) { if (!sameOwner(target)) return; this.set({ proposed: target, phase: this.view.call?.status === 'active' ? this.view.phase : 'confirm', notice: '' }) }
  dismiss = () => { this.set({ proposed: null, phase: this.view.call?.status === 'active' ? this.view.phase : 'idle', notice: '' }) }
  settings = (v: Partial<Pick<CallView, 'voice' | 'language' | 'rate'>>) => { if (this.view.phase === 'preparing' || this.view.phase === 'listening' || this.view.phase === 'speaking') return; this.set(v) }
  confirm = () => { if (this.opening) return; this.opening = true; void this.confirmAsync().finally(() => { this.opening = false }) }
  private async confirmAsync() {
    const target = this.view.proposed; if (!target || !sameOwner(target) || !target.canBegin() || !document.hasFocus()) return
    if (this.view.call?.status === 'active') { await this.endAsync(); if (this.view.call?.status === 'active' || this.view.phase === 'unknown') return }
    if (!target.canBegin() || !sameOwner(target)) return
    const gen = ++this.generation; this.spokenTurn = null; this.speakingTurn = null; this.waitingTurn = null; this.leaseSafeUntil = 0; this.leaseWallUntil = 0; this.reconnectUntil = 0; this.reconnectWallUntil = 0; this.autoResumeGuard=false; this.unknownTurn = null; this.stopTimers(); await this.silence(); this.set({ ...initial, target, proposed: null, phase: 'creating', language: navigator.language || '', rate: 0.5 })
    try {
      if (!this.audioSafe || !await this.transport.available()) throw Error('unsupported')
      const voices = await this.transport.voices(); if (!this.current(gen, target)) return
      this.set({ voices: Array.isArray(voices) ? voices.filter(v => typeof v.id === 'string' && typeof v.name === 'string' && typeof v.language === 'string') : [] })
      if (!target.canBegin() || !document.hasFocus()) { this.set({ ...initial }); return }
      const id = crypto.randomUUID(), createStarted=performance.now(), createWall=Date.now(); this.pendingCreate = { id, target }; let call: VoiceCallRecord
      try { call = await this.calls.create(target.roomId, id, target.agentId) }
      catch (error) { // GET readback only; never replay create after unknown transport.
        const conflict = (error as { response?: { data?: { detail?: { code?: string; call?: VoiceCallRecord } } } }).response?.data?.detail
        if (conflict?.code === 'active_voice_call' && conflict.call) { this.pendingCreate = null; const other = conflict.call; const oldTarget = { ...target, roomId: other.room_id, agentId: other.agent_id, title: '' }; if (validCall(other, oldTarget) && other.status === 'active' && this.current(gen, target)) { this.set({ target: oldTarget, proposed: target, call: other, phase: 'unknown', notice: 'active_conflict' }); return } }
        const result = await this.calls.list(target.roomId); const found = result.calls?.find(row => row.id === id)
        if (!found) throw Error('unknown_create'); call = found
      }
      if (!this.current(gen, target)) return
      if (!validCall(call, target, id) || call.status !== 'active') throw Error('invalid_call')
      this.pendingCreate = null; this.leaseSafeUntil=createStarted+85000; this.leaseWallUntil=createWall+85000; this.stopRevision = readRoomStop(`${target.ownerId}:${target.epoch}`, target.roomId).revision
      this.set({ call, phase: 'muted', muted: false }); this.scheduleHeartbeat(gen, target); if (!target.canBegin() || !document.hasFocus()) { this.set({ phase: 'unknown', notice: 'resume_needed' }); return } await this.startListening(gen, target)
    } catch (e) { if (this.current(gen, target)) this.set({ phase: 'unknown', notice: e instanceof Error && e.message === 'unsupported' ? 'unsupported' : 'create_unknown' }) }
  }
  private stopTimers() { this.reconnectAbort?.abort(); this.reconnectAbort=null; if (this.timer) clearTimeout(this.timer); if (this.heartbeat) clearTimeout(this.heartbeat); this.timer = null; this.heartbeat = null }
  private scheduleHeartbeat(gen: number, t: CallTarget, delay=30000) { if (this.heartbeat) clearTimeout(this.heartbeat); this.heartbeat = setTimeout(() => void this.beat(gen, t), delay) }
  private async beat(gen: number, t: CallTarget) { const call = this.view.call; if (!call || !this.current(gen, t)) return; const started=performance.now(), startedWall=Date.now(); let next:VoiceCallRecord
    try { next = await this.calls.heartbeat(t.roomId, call.id, {timeout:4000}) }
    catch(error) { if(this.current(gen,t)) { if(transient(error)) void this.reconnect(gen,t); else { ++this.generation; this.stopTimers(); await this.silence(); this.set({phase:'unknown',notice:'connection_unknown'}) } } return }
    if (!this.current(gen,t)) return
    if (!validCall(next,t,call.id)) { ++this.generation; this.stopTimers(); await this.silence(); this.set({phase:'unknown',notice:'connection_unknown'}); return }
    if (next.status !== 'active') { this.stopTimers(); await this.silence(); this.set({ call: next, phase: 'ended', notice: '' }); return }
    this.leaseSafeUntil=started+85000; this.leaseWallUntil=startedWall+85000; if(sameRecordedTurns(this.view.call || call,next)) this.set({ call: next }); this.scheduleHeartbeat(gen,t)
  }
  private async resumePermission() {
    let timer:ReturnType<typeof setTimeout>|undefined
    const result=await Promise.race([this.transport.checkResumePermission().catch(()=> 'unknown' as const),new Promise<'unknown'>(resolve=>{timer=setTimeout(()=>resolve('unknown'),2000)})])
    if(timer)clearTimeout(timer)
    if(result==='unknown')this.audioSafe=false
    return result
  }
  private leaseRemaining() { return Math.min(this.leaseSafeUntil-performance.now(),this.leaseWallUntil-Date.now()) }
  private reconnectAllowed(gen:number,t:CallTarget,callId:string) {
    return this.current(gen,t) && this.view.call?.id===callId && this.view.call.status==='active' && this.view.phase==='reconnecting'
      && !this.view.muted && !this.sending && !this.unknownTurn && this.audioSafe && this.eligible(t)
      && document.visibilityState==='visible' && document.hasFocus() && this.leaseRemaining()>5000
  }
  private async reconnect(oldGen:number,t:CallTarget) {
    const before=this.view.call, mode=this.view.phase, turnId=this.waitingTurn
    // Do not restart speech, unknown submissions, permissions, approval, mute or an owner-suspended session.
    if (!before || !this.current(oldGen,t)) return
    const eligibleMode=mode==='listening' || mode==='responding' && !!turnId && turnId!==this.spokenTurn
    const foreground=document.visibilityState==='visible' && document.hasFocus()
    const canAttempt=eligibleMode && foreground && !this.view.muted && !this.sending && !this.unknownTurn && this.audioSafe && this.eligible(t) && this.leaseRemaining()>5000
    const gen=++this.generation; this.stopTimers(); this.set({phase:canAttempt?'reconnecting':'unknown',notice:canAttempt?'reconnecting':'connection_unknown'})
    let timer:ReturnType<typeof setTimeout>|undefined
    const stopped=await Promise.race([this.silence().then(()=>true),new Promise<boolean>(resolve=>{timer=setTimeout(()=>resolve(false),2000)})]);if(timer)clearTimeout(timer)
    if(!this.current(gen,t))return
    if(!stopped){this.audioSafe=false;this.set({phase:'unknown',notice:'audio_unknown'});return}
    if(!canAttempt||!this.audioSafe){this.set({phase:'unknown',notice:this.audioSafe?'connection_unknown':'audio_unknown'});return}
    if(!this.reconnectUntil){this.reconnectUntil=performance.now()+12000;this.reconnectWallUntil=Date.now()+12000}
    const deadline=Math.min(this.reconnectUntil,this.leaseSafeUntil-5000),wallDeadline=Math.min(this.reconnectWallUntil,this.leaseWallUntil-5000)
    const delays=[0,700,1500,3000,4000]
    const attempt=async(index:number):Promise<void>=>{
      if(!this.reconnectAllowed(gen,t,before.id)||performance.now()>=deadline||Date.now()>=wallDeadline){if(this.current(gen,t)&&this.view.phase==='reconnecting')this.set({phase:'unknown',notice:'reconnect_failed'});return}
      const controller=new AbortController();this.reconnectAbort=controller
      try {
        const timeout=Math.max(1,Math.min(2500,deadline-performance.now(),wallDeadline-Date.now()))
        const next=await this.calls.get(t.roomId,before.id,{signal:controller.signal,timeout})
        if(this.reconnectAbort===controller)this.reconnectAbort=null
        if(!this.reconnectAllowed(gen,t,before.id)){if(this.current(gen,t)&&this.view.phase==='reconnecting')this.set({phase:'unknown',notice:'reconnect_failed'});return}
        if(!validCall(next,t,before.id)||next.started_at!==before.started_at||next.status==='active'&&next.ended_at!==null||!sameRecordedTurns(before,next)||next.turns.some(row=>!['queued','running','waiting_approval','completed','failed','cancelled','interrupted','unknown'].includes(row.phase))){this.set({phase:'unknown',notice:'reconnect_failed'});return}
        if(next.status!=='active'){this.set({call:next,phase:'ended',notice:''});return}
        if(mode==='responding') {
          const turn=next.turns.find(item=>item.client_turn_id===turnId)
          if(!turn || turn.client_turn_id===this.spokenTurn){this.set({phase:'unknown',notice:'reconnect_failed'});return}
          if(next.turns.some(row=>row.client_turn_id!==turnId && ['queued','running','waiting_approval','unknown'].includes(row.phase))){this.set({phase:'unknown',notice:'reconnect_failed'});return}
          if(turn.phase==='waiting_approval'){this.set({call:next,phase:'approval',notice:'approval'});this.scheduleHeartbeat(gen,t,Math.min(30000,Math.max(1,this.leaseRemaining()-5000)));return}
          if(!['queued','running','completed'].includes(turn.phase)){this.set({phase:'unknown',notice:'reconnect_failed'});return}
          const permission=await this.resumePermission()
          if(!this.reconnectAllowed(gen,t,before.id) || performance.now()>=deadline || Date.now()>=wallDeadline){if(this.current(gen,t)&&this.view.phase==='reconnecting')this.set({phase:'unknown',notice:this.audioSafe?'reconnect_failed':'audio_unknown'});return}
          if(permission!=='authorized'){this.set({call:next,phase:'unknown',notice:permission==='permission_needed'?'permission_needed':'audio_unknown'});return}
          this.autoResumeGuard=true;this.set({call:next,phase:'responding',notice:'reconnected'});this.scheduleHeartbeat(gen,t,Math.min(30000,Math.max(1,this.leaseRemaining()-5000)));this.later(()=>void this.pollTurn(gen,t,turn.client_turn_id),100);return
        }
        const last=next.turns.at(-1)
        if(next.turns.some(row=>['queued','running','waiting_approval','unknown'].includes(row.phase)) || last && (last.phase!=='completed'||last.client_turn_id!==this.spokenTurn)){this.set({phase:'unknown',notice:'reconnect_failed'});return}
        const permission=await this.resumePermission()
        if(!this.reconnectAllowed(gen,t,before.id) || performance.now()>=deadline || Date.now()>=wallDeadline){if(this.current(gen,t)&&this.view.phase==='reconnecting')this.set({phase:'unknown',notice:this.audioSafe?'reconnect_failed':'audio_unknown'});return}
        if(permission!=='authorized'){this.set({call:next,phase:'unknown',notice:permission==='permission_needed'?'permission_needed':'audio_unknown'});return}
        this.autoResumeGuard=true;this.set({call:next,phase:'preparing',notice:'reconnected'});this.scheduleHeartbeat(gen,t,Math.min(30000,Math.max(1,this.leaseRemaining()-5000)));await this.startListening(gen,t,true)
      } catch(error) {
        if(this.reconnectAbort===controller)this.reconnectAbort=null
        if(!this.reconnectAllowed(gen,t,before.id)){if(this.current(gen,t)&&this.view.phase==='reconnecting')this.set({phase:'unknown',notice:'reconnect_failed'});return}
        if(!transient(error)||index+1>=delays.length){this.set({phase:'unknown',notice:'reconnect_failed'});return}
        const delay=delays[index+1]; if(performance.now()+delay>=deadline||Date.now()+delay>=wallDeadline){this.set({phase:'unknown',notice:'reconnect_failed'});return}
        this.later(()=>void attempt(index+1),delay)
      }
    }
    await attempt(0)
  }
  private eligible(t: CallTarget) { const identity = `${t.ownerId}:${t.epoch}`; return sameOwner(t) && readRoomStop(identity, t.roomId).phase !== 'unknown' && roomSendAllowed(identity, t.roomId, this.stopRevision) && !blocksNewMessage(pendingRoomMessages(identity, t.roomId)) }
  private async startListening(gen: number, t: CallTarget, reconnected=false) {
    await this.silencing
    if (!this.current(gen, t) || this.view.muted || !this.view.call || this.view.call.status !== 'active') return
    if (!this.audioSafe || reconnected && this.leaseRemaining()<=5000 || !this.eligible(t) || !document.hasFocus() || document.visibilityState !== 'visible') { this.set({ phase: 'unknown', notice: 'resume_needed' }); return }
    const token = crypto.randomUUID(); this.mic = token; this.set({ phase: 'preparing', notice: reconnected ? 'reconnected' : '' })
    try { await this.track(reconnected ? this.transport.resumeListen(token, this.view.language) : this.transport.listen(token, this.view.language)); if (!this.current(gen, t) || this.mic !== token || reconnected && this.leaseRemaining()<=5000) { const owns=this.current(gen,t) && this.mic===token; if(owns)this.set({phase:'unknown',notice:'reconnect_failed'}); let timeout:ReturnType<typeof setTimeout>|undefined; const stopped=await Promise.race([this.transport.cancelListen(token).then(()=>true).catch(()=>false),new Promise<boolean>(resolve=>{timeout=setTimeout(()=>resolve(false),2000)})]); if(timeout)clearTimeout(timeout); if(!stopped)this.audioSafe=false; if(owns && this.current(gen,t) && this.mic===token){this.mic=null;if(!stopped)this.set({phase:'unknown',notice:'audio_unknown'})} return } this.later(() => void this.pollMic(gen, t, token), 400) }
    catch { if (this.current(gen, t)) { await this.silence(); this.set({ phase: 'unknown', notice: 'microphone_unknown' }) } }
  }
  private async pollMic(gen: number, t: CallTarget, token: string) {
    if (!this.current(gen, t) || this.mic !== token) return
    if (!this.eligible(t)) { await this.silence(); this.set({ phase: 'unknown', notice: 'stop_changed' }); return }
    try { const r = await this.transport.listenState(token); if (!this.current(gen, t) || this.mic !== token) return
      if (r.phase === 'waiting' || r.phase === 'listening' || r.phase === 'stopping') { if (r.phase === 'listening' && this.view.phase === 'preparing') { this.reconnectUntil=0; this.reconnectWallUntil=0; this.autoResumeGuard=false; this.set({ phase: 'listening' }) }; this.later(() => void this.pollMic(gen, t, token), 400); return }
      this.mic = null; await this.transport.cancelListen(token).catch(() => {})
      if (r.phase !== 'review') { this.set({ phase: 'unknown', notice: r.phase }); return }
      const text = r.text.trim(); if (!text) { await this.startListening(gen, t); return }
      await this.sendTurn(gen, t, text)
    } catch { if (this.current(gen, t)) { await this.silence(); this.set({ phase: 'unknown', notice: 'microphone_unknown' }) } }
  }
  private async sendTurn(gen: number, t: CallTarget, text: string) {
    const c = this.view.call; if (!c || !this.eligible(t) || this.sending || text.length > 65536) { this.set({ phase: 'unknown', notice: 'send_blocked' }); return }
    this.sending = true; const turn = crypto.randomUUID(); this.unknownTurn = { id: turn, text }; this.set({ phase: 'responding', notice: '' })
    try { const next = await this.calls.turn(t.roomId, c.id, turn, text)
      if (!this.current(gen, t)) return
      if (!validCall(next, t, c.id) || next.status !== 'active') throw Error()
      const row = next.turns.find(x => x.client_turn_id === turn && x.text === text); if (!row) throw Error()
      this.unknownTurn = null; this.waitingTurn=turn; this.set({ call: next }); this.later(() => void this.pollTurn(gen, t, turn), 500)
    } catch { if (this.current(gen, t)) this.set({ phase: 'unknown', notice: 'turn_unknown' }) } finally { this.sending = false }
  }
  private async pollTurn(gen: number, t: CallTarget, id: string) {
    const call = this.view.call; if (!call || !this.current(gen, t)) return
    if (!this.eligible(t)) { this.set({ phase: 'unknown', notice: 'stop_changed' }); return }
    try { const next = await this.calls.get(t.roomId, call.id, {timeout:4000}); if (!this.current(gen, t)) return
      if (!validCall(next, t, call.id) || next.status !== 'active') throw Error()
      const turn = next.turns.find(x => x.client_turn_id === id); if (!turn) throw Error(); this.set({ call: next })
      if (turn.phase === 'waiting_approval') { this.set({ phase: 'approval', notice: 'approval' }); return }
      if (turn.phase === 'queued' || turn.phase === 'running') { this.reconnectUntil=0;this.reconnectWallUntil=0;this.later(() => void this.pollTurn(gen, t, id), 1500); return }
      if (turn.phase !== 'completed' || !turn.reply_event_ref || !turn.reply_text) { this.set({ phase: 'unknown', notice: 'reply_unknown' }); return }
      if (!await this.verifyReply(t, turn) || !this.current(gen, t)) throw Error()
      if(!this.eligible(t) || !document.hasFocus() || document.visibilityState!=='visible' || this.autoResumeGuard && this.leaseRemaining()<=5000){this.set({phase:'unknown',notice:'reconnect_failed'});return}
      this.reconnectUntil=0;this.reconnectWallUntil=0;this.waitingTurn = null; this.speakingTurn = turn.client_turn_id; await this.speakReply(gen, t, turn.reply_text)
    } catch(error) { if (this.current(gen, t)) { if(transient(error)) void this.reconnect(gen,t); else this.set({ phase: 'unknown', notice: 'reply_unknown' }) } }
  }
  private async verifyReply(t: CallTarget, turn: VoiceCallTurn) {
    const reply = turn.reply_event_ref
    if (!reply || reply.seq <= turn.user_event_ref.seq) return false
    let cursor = turn.user_event_ref.seq - 1
    while (cursor < reply.seq) {
      const rows = (await api.get<RoomEvent[]>(`/conversations/${t.roomId}/events`,{params:{after_seq:cursor,limit:100},timeout:4000})).data; if (!sameOwner(t) || !rows.length) return false
      const root = rows.find(r => r.event_id === turn.user_event_ref.event_id && r.seq === turn.user_event_ref.seq)
      if (cursor === turn.user_event_ref.seq - 1 && (!root || root.room_id !== t.roomId || root.actor_type !== 'user' || root.actor_id !== t.ownerId || root.kind !== 'message' || root.idempotency_key !== `voice:${this.view.call?.id}:${turn.client_turn_id}` || root.payload.content !== turn.text)) return false
      const answer = rows.find(r => r.event_id === reply.event_id && r.seq === reply.seq)
      if (answer) return answer.room_id === t.roomId && answer.actor_type === 'agent' && answer.actor_id === t.agentId && answer.kind === 'message' && answer.payload.trigger_event_id === turn.user_event_ref.event_id && answer.payload.content === turn.reply_text
      const last = rows[rows.length - 1]?.seq; if (!last || last <= cursor) return false; cursor = last
    }
    return false
  }
  private async speakReply(gen: number, t: CallTarget, text: string) {
    await this.silencing
    if (!this.current(gen, t)) return
    if(!this.eligible(t) || !document.hasFocus() || document.visibilityState!=='visible' || this.autoResumeGuard && this.leaseRemaining()<=5000){this.set({phase:'unknown',notice:'reconnect_failed'});return}
    if (this.speech || this.mic || !this.audioSafe) { this.set({ phase: 'unknown', notice: 'audio_unknown' }); return }
    const token = crypto.randomUUID(); this.speech = token; this.set({ phase: 'speaking' })
    try { const phase = await this.track(this.transport.speak(token, text, this.view.voice, this.view.rate)); if (!this.current(gen, t) || this.speech !== token) { await this.transport.cancelSpeak(token).catch(() => {}); return } if (phase !== 'speaking') throw Error(); this.later(() => void this.pollSpeech(gen, t, token), 400) }
    catch { if (this.current(gen, t)) { await this.silence(); this.set({ phase: 'unknown', notice: 'speech_unknown' }) } }
  }
  private async pollSpeech(gen: number, t: CallTarget, token: string) { if (!this.current(gen, t) || this.speech !== token) return; try { const phase = await this.transport.speakState(token); if (!this.current(gen, t) || this.speech !== token) return; if (phase === 'speaking') { this.later(() => void this.pollSpeech(gen, t, token), 400); return } this.speech = null; await this.transport.cancelSpeak(token).catch(() => {}); if (phase !== 'finished') { this.set({ phase: 'unknown', notice: 'speech_unknown' }); return } this.spokenTurn = this.speakingTurn; this.speakingTurn = null; if (this.view.muted) this.set({ phase: 'muted' }); else await this.startListening(gen, t, this.autoResumeGuard) } catch { if (this.current(gen, t)) { await this.silence(); this.set({ phase: 'unknown', notice: 'speech_unknown' }) } } }
  mute = () => { if (!this.view.call || this.view.call.status !== 'active') return; const muted = !this.view.muted; this.set({ muted }); if (muted && (this.view.phase === 'listening' || this.view.phase === 'preparing' || this.view.phase === 'reconnecting')) { const reconnecting=this.view.phase==='reconnecting', gen = ++this.generation, t = this.view.target; this.stopTimers(); void this.silence(); this.set({ phase: 'muted' }); if (t && !reconnecting) this.scheduleHeartbeat(gen, t) } else if (!muted && this.view.phase === 'muted') this.set({ phase: 'unknown', notice: 'resume_needed' }) }
  resume = () => { if (this.checking) return; this.checking = true; void this.resumeAsync().finally(() => { this.checking = false }) }
  private async resumeAsync() {
    this.reconnectUntil=0;this.reconnectWallUntil=0;this.autoResumeGuard=false
    const t = this.view.target; if (!t || !sameOwner(t)) return; const gen = this.generation
    if (!this.view.call && this.pendingCreate?.target === t) {
      const pending = this.pendingCreate
      try { const result = await this.calls.list(t.roomId); const found = result.calls.find(row => row.id === pending.id); if (!this.current(gen, t)) return
        if (!found || !validCall(found, t, pending.id) || found.status !== 'active') { this.set({ phase: 'unknown', notice: 'create_unknown' }); return }
        this.pendingCreate = null; this.stopRevision = readRoomStop(`${t.ownerId}:${t.epoch}`, t.roomId).revision; this.set({ call: found, phase: 'unknown', notice: 'resume_needed' }); this.scheduleHeartbeat(gen, t)
      } catch { if (this.current(gen, t)) this.set({ phase: 'unknown', notice: 'create_unknown' }) }
      return
    }
    const call = this.view.call
    // Once hangup is unconfirmed, checking is strictly read-only: no lease renewal or audio restart.
    if (call && this.view.notice === 'end_unknown') {
      try { const next = await this.calls.get(t.roomId, call.id); if (!this.current(gen, t) || !validCall(next, t, call.id)) return
        if (next.status === 'active') this.set({ call: next, phase: 'unknown', notice: 'end_unknown' })
        else this.set({ call: next, phase: 'ended', notice: this.audioSafe ? '' : 'audio_unknown' })
      } catch { if (this.current(gen, t)) this.set({ phase: 'unknown', notice: 'end_unknown' }) }
      return
    }
    if (!call || call.status !== 'active' || this.view.muted) return
    try { const next = await this.calls.get(t.roomId, call.id); if (!this.current(gen, t) || !validCall(next, t, call.id)) throw Error()
      if (next.status !== 'active') { this.stopTimers(); await this.silence(); this.set({ call: next, phase: 'ended', notice: this.audioSafe ? '' : 'audio_unknown' }); return }
      this.set({ call: next }); this.scheduleHeartbeat(gen, t)
      const stop = readRoomStop(`${t.ownerId}:${t.epoch}`, t.roomId); if (stop.phase === 'sending' || stop.phase === 'unknown') { this.set({ phase: 'unknown', notice: 'stop_changed' }); return } this.stopRevision = stop.revision
      if (this.unknownTurn) { const uncertain = this.unknownTurn; const found = next.turns.find(row => row.client_turn_id === uncertain.id && row.text === uncertain.text); if (!found) { this.set({ phase: 'unknown', notice: 'turn_unknown' }); return } this.unknownTurn = null; this.waitingTurn=found.client_turn_id; this.set({ phase: 'responding', notice: '' }); this.later(() => void this.pollTurn(gen, t, found.client_turn_id), 100); return }
      const pending = next.turns.at(-1); if (pending && (['queued','running','waiting_approval'].includes(pending.phase) || pending.phase === 'completed' && pending.client_turn_id !== this.spokenTurn)) { this.waitingTurn=pending.client_turn_id; this.set({ phase: 'responding' }); this.later(() => void this.pollTurn(gen, t, pending.client_turn_id), 100); return }
      if (this.view.notice === 'turn_unknown') return; await this.startListening(gen, t)
    } catch { if (this.current(gen, t)) this.set({ phase: 'unknown', notice: 'connection_unknown' }) }
  }
  refresh = () => { const t = this.view.target, call = this.view.call, gen = this.generation; if (!t || !call || !sameOwner(t)) return; void this.calls.get(t.roomId, call.id).then(next => { if (this.current(gen, t) && validCall(next, t, call.id)) this.set({ call: next, ...(next.status !== 'active' ? { phase: 'ended' as const } : {}) }) }).catch(() => { if (this.current(gen, t)) this.set({ notice: 'connection_unknown' }) }) }
  hangup = () => { void this.endAsync() }
  private async endAsync() { const t = this.view.target, c = this.view.call; const gen = ++this.generation; this.stopTimers(); this.set({ phase: 'stopping' }); await this.silence(); if (!t || !c || !sameOwner(t)) { this.set({ ...initial }); return } try { let next: VoiceCallRecord; try { next = await this.calls.end(t.roomId, c.id) } catch { next = await this.calls.get(t.roomId, c.id) } if (gen !== this.generation || !sameOwner(t)) return; if (!validCall(next, t, c.id) || next.status === 'active') throw Error(); this.unknownTurn = null; this.set({ call: next, phase: 'ended', muted: false, notice: this.audioSafe ? '' : 'audio_unknown' }) } catch { if (gen === this.generation && sameOwner(t)) this.set({ phase: 'unknown', notice: 'end_unknown' }) } }
  resetOwner() { ++this.generation; this.pendingCreate = null; this.unknownTurn = null; this.waitingTurn=null; this.leaseSafeUntil=0; this.leaseWallUntil=0; this.reconnectUntil=0; this.reconnectWallUntil=0; this.autoResumeGuard=false; this.stopTimers(); void this.silence(); this.set({ ...initial }) }
  suspend() { if (!this.view.call || this.view.call.status !== 'active') return; const gen = ++this.generation, t = this.view.target; this.stopTimers(); void this.silence(); this.set({ phase: 'unknown', notice: 'resume_needed' }); if (t) this.scheduleHeartbeat(gen, t) }
}
export const voiceCallController = new VoiceCallController()
