import type { Meta, StoryObj } from '@storybook/react-vite'
import { useCallback, useLayoutEffect, useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { toolsApi, type ActionPreview, type ToolActivity, type SlackBinding } from '../src/api/tools'
import { eventsApi, type RoomEvent } from '../src/api/events'
import GrantPrompt from '../src/components/GrantPrompt'
const id=(n:number)=>`b2000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const root=id(1), actionId=id(2), reviewId=id(3), digest='a'.repeat(64)
const binding:SlackBinding={version:1,owner_id:1,agent_id:8,room_id:501,run_id:id(4),config_revision:1,credential_revision:1,generation:1,root_event_id:root,workspace_id:'TSYNTHETIC',channel_id:'CSYNTHETIC',sender_id:'USYNTHETIC',event_id:'EvSynthetic',thread_ts:'123.456'}
const rootRef={room_id:501,event_id:root,seq:1,actor_type:'system' as const,actor_id:null,kind:'activity' as const}
const fields={slack_binding:binding,root_ref:rootRef,scope_status:'active' as const,revocation_state:null,routine_binding:null,execution_binding:null}
const action:ToolActivity={action_id:actionId,room_id:501,agent_id:8,trigger_event_id:root,family:'read_file',target:'synthetic.txt',expected_effect:'Read a file',state:'waiting',scope:null,grant_id:null,requested_at:'2026-11-03T00:00:00Z',started_at:null,completed_at:null,review_id:reviewId,...fields}
const preview:ActionPreview={action_id:actionId,family:action.family,target:action.target,scope_target:action.target,scope_description:'Synthetic review',expected_effect:action.expected_effect,workflow_id:null,arguments:{path:'synthetic.txt'},payload_digest:digest,review_id:reviewId,...fields,approval_policy:{scopes:['once'],unverified_context_required:false,start_within_seconds:60},review:{version:1,action:{action_id:actionId,room_id:501,agent_id:8,trigger_event_id:root,family:action.family,target:action.target,scope_target:action.target,expected_effect:action.expected_effect,payload_digest:digest,execution_binding:null},authority:{owner_id:1,room_id:501,agent_id:8,proposer:{owner_id:1,room_id:501,agent_id:8}},targets:[]}}
function Surface({mismatch=false}:{mismatch?:boolean}){
 const [ready,setReady]=useState(false),refresh=useCallback(()=>{},[]),stop=useCallback(()=>{},[])
 useLayoutEffect(()=>{useAuthStore.getState().setAuth('synthetic-token',{id:1,email:'preview@example.invalid',username:'Preview'});const priorPreview=toolsApi.preview,priorDecide=toolsApi.decide,priorEvents=eventsApi.list
  toolsApi.preview=async()=>structuredClone(preview)
  toolsApi.decide=async()=>{throw Error('Synthetic only')}
  eventsApi.list=async()=>[{event_id:root,room_id:501,seq:1,actor_type:'system',actor_id:null,actor_name_snapshot:'',kind:'activity',device_id:null,idempotency_key:null,created_at:'2026-11-03T00:00:00Z',payload:{category:'slack_external',content:'합성 Slack 요청을 확인해 주세요.',slack:{version:1,run_id:binding.run_id,event_id:binding.event_id,workspace_id:binding.workspace_id,channel_id:binding.channel_id,user_id:mismatch?'UOTHER':binding.sender_id,thread_ts:binding.thread_ts,config_revision:1}}} satisfies RoomEvent]
  setReady(true);return()=>{toolsApi.preview=priorPreview;toolsApi.decide=priorDecide;eventsApi.list=priorEvents}
 },[mismatch])
 return <MemoryRouter><I18nProvider><main className="page-frame page-content"><p>합성 Slack 승인 화면 · 실제 승인이나 파일 접근 없음</p>{ready&&<GrantPrompt roomId={501} roomName="합성 Slack 대화" agentName="합성 Bot" action={action} onRefresh={refresh} onStop={stop}/>}</main></I18nProvider></MemoryRouter>
}
const meta={title:'Product/Slack approval origin',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const MatchingOrigin:Story={args:{mismatch:false}}
export const MismatchedOrigin:Story={args:{mismatch:true}}
