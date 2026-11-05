import type { Meta, StoryObj } from '@storybook/react-vite'
import { MemoryRouter } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useAuthStore } from '../src/stores/authStore'
import { directoryViewGeneration } from '../src/lib/directoryViewMemory'
import { voiceCallsApi } from '../src/api/voiceCalls'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { VoiceCallPanel } from '../src/components/VoiceCall'
import { VoiceCallController, type CallTransport, type CallPhase, type CallTarget, type CallView, type VoiceCallUIController } from '../src/lib/voiceCallController'
import type { VoiceCallRecord } from '../src/api/voiceCalls'

// Static fake state only: no API, audio, microphone, permissions or native calls.
const target:CallTarget={ownerId:1,epoch:1,roomId:501,agentId:7,title:'감자',generation:0,canBegin:()=>true}
const call:VoiceCallRecord={id:'11111111-1111-4111-8111-111111111111',owner_id:1,room_id:501,agent_id:7,status:'active',started_at:'2026-11-03T00:00:00Z',ended_at:null,duration_ms:20000,turns:[{client_turn_id:'22222222-2222-4222-8222-222222222222',text:'합성 예시 발화',user_event_ref:{event_id:'33333333-3333-4333-8333-333333333333',seq:1},phase:'running',reply_event_ref:null,reply_text:null,replies:[]}]}
function Surface({phase,notice}:{phase:CallPhase;notice:string}) {
 const view:CallView={phase,notice,target,proposed:null,call,muted:false,voices:[],voice:'',language:'ko-KR',rate:0.5}
 const controller:VoiceCallUIController={subscribe:()=>()=>{},snapshot:()=>view,confirm(){},dismiss(){},hangup(){},mute(){},resume(){},refresh(){},settings(){}}
 return <I18nProvider><MemoryRouter><VoiceCallPanel controller={controller}/></MemoryRouter></I18nProvider>
}
const meta={title:'Screens/음성 통화 연결 복구',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const Reconnecting:Story={args:{phase:'reconnecting',notice:'reconnecting'}}
export const Recovered:Story={args:{phase:'responding',notice:'reconnected'}}
export const Failed:Story={args:{phase:'unknown',notice:'reconnect_failed'}}

// This story drives the real controller with injected, in-memory API/audio
// adapters. Counts are synthetic; no Core, network, microphone or sound output.
function DrivenSurface({outcome}:{outcome:'recover'|'fail'|'post-unknown'|'mic-recover'|'mic-denied'}) {
 const [,redraw]=useState(0)
 const [fixture]=useState(()=>{
  useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'})
  const epoch=useAuthStore.getState().epoch, counts={creates:0,turns:0,reads:0,heartbeats:0,permissions:0,resumes:0}, update={fn:()=>{}}
  let record:VoiceCallRecord|null=null
  const clone=()=>{if(!record)throw Error('synthetic call unavailable');return structuredClone(record)}
  const fail=()=>Object.assign(new Error('synthetic disconnected'),{isAxiosError:true,code:'ERR_NETWORK'})
  const calls:typeof voiceCallsApi={
   async list(){return {calls:record?[clone()]:[]}},
   async create(room,id,agent){counts.creates++;update.fn();record={...call,id,room_id:room,agent_id:agent,owner_id:1,turns:[]};return clone()},
   async turn(_room,_id,turnId,text){counts.turns++;update.fn();if(!record)throw Error('synthetic call unavailable');record={...record,turns:[{client_turn_id:turnId,text,user_event_ref:{event_id:'33333333-3333-4333-8333-333333333333',seq:1},phase:'running',reply_event_ref:null,reply_text:null,replies:[]}]};if(outcome==='post-unknown')throw fail();return clone()},
   async get(_room,_id,options){counts.reads++;update.fn();if((!outcome.startsWith('mic-') && counts.reads===1)||outcome==='fail')throw fail();return await new Promise((resolve,reject)=>{const timer=setTimeout(()=>resolve(clone()),1500);options?.signal?.addEventListener('abort',()=>{clearTimeout(timer);reject(Object.assign(Error('cancelled'),{isAxiosError:true,code:'ERR_CANCELED'}))},{once:true})})},
   async heartbeat(){counts.heartbeats++;update.fn();if(outcome.startsWith('mic-')&&counts.heartbeats===1)throw fail();return clone()},
   async end(){if(!record)throw Error('synthetic call unavailable');record={...record,status:'ended'};return clone()},
  }
  const transport:CallTransport={async available(){return true},async voices(){return []},async checkResumePermission(){counts.permissions++;update.fn();return outcome==='mic-denied'?'permission_needed':'authorized'},async listen(){},async resumeListen(){counts.resumes++;update.fn()},async listenState(){return outcome.startsWith('mic-')?{phase:'listening',text:''}:{phase:'review',text:'합성 발화'}},async cancelListen(){},async speak(){throw Error('synthetic speech disabled')},async speakState(){throw Error('synthetic speech disabled')},async cancelSpeak(){}}
  return {controller:new VoiceCallController(transport,calls),counts,update,epoch}
 })
 fixture.update.fn=()=>redraw(n=>n+1)
 useEffect(()=>{const t:CallTarget={...target,epoch:fixture.epoch,generation:directoryViewGeneration()};fixture.controller.offer(t);return()=>{fixture.controller.resetOwner();fixture.update.fn=()=>{}}},[fixture])
 return <I18nProvider><MemoryRouter><VoiceCallPanel controller={fixture.controller}/><p className="metadata">합성 호출: 통화 생성 {fixture.counts.creates} · 발화 전송 {fixture.counts.turns} · 상태 조회 {fixture.counts.reads} · 유지 {fixture.counts.heartbeats} · 권한 상태 확인 {fixture.counts.permissions} · 무프롬프트 듣기 재개 {fixture.counts.resumes}</p></MemoryRouter></I18nProvider>
}
const startDriven=async({canvasElement}:{canvasElement:HTMLElement})=>{const canvas=within(canvasElement);await userEvent.click(await canvas.findByRole('button',{name:/통화 시작|Start call/}))}
export const ControllerRecovers:Story={args:{phase:'confirm',notice:''},render:()=> <DrivenSurface outcome="recover"/>,play:startDriven}
export const ControllerFails:Story={args:{phase:'confirm',notice:''},render:()=> <DrivenSurface outcome="fail"/>,play:startDriven}
export const MicrophoneResumeAllowed:Story={args:{phase:'confirm',notice:''},render:()=> <DrivenSurface outcome="mic-recover"/>,play:startDriven}
export const MicrophoneResumeDenied:Story={args:{phase:'confirm',notice:''},render:()=> <DrivenSurface outcome="mic-denied"/>,play:startDriven}
export const TurnPostUnknown:Story={args:{phase:'confirm',notice:''},render:()=> <DrivenSurface outcome="post-unknown"/>,play:startDriven}
