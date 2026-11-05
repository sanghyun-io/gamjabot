import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useState } from 'react'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { vaultApi } from '../src/api/vault'
import { vaultProfileHandoffApi, type HandoffStatus, type NativeState } from '../src/api/vaultProfileHandoff'
import VaultProfileHandoff from '../src/components/VaultProfileHandoff'

// Actual product component with synthetic in-memory data. No browser or IPC.
const profile='0123456789abcdef0123456789abcdef', handoff='fedcba9876543210fedcba9876543210', epoch='a'.repeat(64)
type Scenario='ready'|'custody'|'parkRefreshFailure'|'parkReplyLost'|'parkReplyMismatch'|'borrowed'|'pending'|'unknown'|'recovery'|'changed'|'terminal'
function Surface({scenario}:{scenario:Scenario}) {
 const [ready,setReady]=useState(false),[identity,setIdentity]=useState(1)
 useLayoutEffect(()=>{
  useAuthStore.getState().setAuth('synthetic-token',{id:identity,email:'preview@example.invalid',username:'Preview'})
  const saved={...vaultProfileHandoffApi}, originalSession=vaultApi.session
  let state:HandoffStatus={version:1,profile_id:profile,revision:scenario==='terminal'?5:4,allowed_origins:[['https','example.invalid',443],['https','sso.example.invalid',443]],handoff:{handoff_id:handoff,owner_id:identity,owner_epoch:epoch,profile_id:profile,profile_revision:4,room_id:501,origin:'https://example.invalid',username:'demo@example.invalid',profile_label:'Demo Bot profile',account_label:'Demo',state:scenario==='terminal'?'returned':'prepared',prior_outcome_unknown:false},native_status:{version:1,status:scenario==='recovery'||scenario==='terminal'?'unknown':'prepared',handoff_id:handoff,profile_id:profile,profile_revision:4,fill_status:scenario==='recovery'||scenario==='terminal'?'unknown':'not_started'},can_fill:scenario!=='recovery'&&scenario!=='terminal',can_return:false,can_recover:true}
  vaultApi.session=async()=>({version:1,owner_id:identity,owner_epoch:epoch,identity_attested:true})
  vaultProfileHandoffApi.available=()=>true
  let held=scenario==='borrowed'?'borrowed':null as 'borrowed'|'parked'|null
  if(scenario==='custody'||scenario==='parkRefreshFailure'||scenario==='parkReplyLost'||scenario==='parkReplyMismatch')state={...state,native_status:{...state.native_status!,status:'active',fill_status:'filled'}}
  let loseNextRead=false
  vaultProfileHandoffApi.read=async()=>{if(loseNextRead){loseNextRead=false;throw Error('synthetic refresh failure')}return structuredClone(state)}
  vaultProfileHandoffApi.seal=async()=> 'b'.repeat(32)
  vaultProfileHandoffApi.custody=async()=>held?{version:1,status:held,handoff_id:handoff,profile_id:profile,profile_revision:4,custody_id:(scenario==='parkReplyMismatch'?'c':'b').repeat(32)}:{version:1,status:'unavailable',handoff_id:handoff,profile_id:null,profile_revision:null,custody_id:null}
  vaultProfileHandoffApi.native=async(operation):Promise<NativeState>=>{
   if(scenario==='pending')return new Promise<NativeState>(()=>{})
   if(operation==='park'){held='parked';if(scenario==='parkRefreshFailure')loseNextRead=true;state={...state,native_status:{...state.native_status!,status:'parked'},can_fill:false,can_return:false};if(scenario==='parkReplyLost'||scenario==='parkReplyMismatch')throw Error('synthetic native reply lost');return state.native_status!}
   if(scenario==='unknown')throw Error('synthetic unknown')
   const result:NativeState={version:1,status:operation==='close'?'closed':'active',handoff_id:handoff,profile_id:profile,profile_revision:4,fill_status:'not_filled'}
   if(operation==='close')held=null
   state={...state,native_status:result,can_fill:operation!=='close',can_return:operation==='close'};return result
  }
  vaultProfileHandoffApi.finish=async()=>{state={...state,revision:5,handoff:{...state.handoff!,state:'returned'},can_fill:false,can_return:false,can_recover:false};return structuredClone(state)}
  vaultProfileHandoffApi.recover=async()=>{state={...state,revision:5,handoff:{...state.handoff!,state:scenario==='terminal'?'returned':'recovered',prior_outcome_unknown:true},native_status:{version:1,status:'recovered',handoff_id:handoff,profile_id:profile,profile_revision:4,fill_status:'unknown'},can_fill:false,can_return:false,can_recover:false};return structuredClone(state)}
  setReady(true)
  return()=>{Object.assign(vaultProfileHandoffApi,saved);vaultApi.session=originalSession;sessionStorage.removeItem(`gamja.vaultProfileHandoff.unknown:${identity}:501:${profile}:${handoff}`)}
 },[identity,scenario])
 return <I18nProvider><main className="page-frame page-content"><p>합성 상태 · 실제 사이트와 자격증명을 사용하지 않습니다.</p>{scenario==='changed'&&<button type="button" onClick={()=>setIdentity(2)}>Change synthetic Owner</button>}{ready&&<VaultProfileHandoff key={identity} roomId={501} profileId={profile}/>}</main></I18nProvider>
}
const meta={title:'Screens/Vault same-profile handoff',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const Prepared:Story={args:{scenario:'ready'}}
export const KeepLogin:Story={args:{scenario:'custody'}}
export const ParkReplyLost:Story={args:{scenario:'parkReplyLost'}}
export const ParkReplyMismatch:Story={args:{scenario:'parkReplyMismatch'}}
export const ParkRefreshFailure:Story={args:{scenario:'parkRefreshFailure'}}
export const BotUsingSession:Story={args:{scenario:'borrowed'}}
export const Pending:Story={args:{scenario:'pending'}}
export const Unknown:Story={args:{scenario:'unknown'}}
export const Recovery:Story={args:{scenario:'recovery'}}
export const IdentityChange:Story={args:{scenario:'changed'}}

export const TerminalJournalRecovery:Story={args:{scenario:'terminal'}}
