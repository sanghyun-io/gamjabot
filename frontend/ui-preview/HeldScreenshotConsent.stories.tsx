import type { Meta, StoryObj } from '@storybook/react-vite'
import { useCallback, useLayoutEffect, useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { toolsApi, type ActionPreview, type ExecutionBinding, type ToolActivity } from '../src/api/tools'
import GrantPrompt from '../src/components/GrantPrompt'
import ToolActivityRecord from '../src/components/ToolActivityRecord'

// Synthetic retained review and history only; no screenshot, provider or network.
const id=(n:number)=>`b2000000-0000-4000-8000-${String(n).padStart(12,'0')}`
const profile='0123456789abcdef0123456789abcdef', target='https://example.invalid/held-page', effect='Capture browser page', digest='a'.repeat(64), root=id(1)
const binding:ExecutionBinding={version:1,root_scope_id:id(2),scope_id:id(2),owner_id:1,root_room_id:501,root_request_event_id:root,execution_room_id:501,agent_id:8,run_generation:1,job_id:null,phase:null,context_mode:'user_room',deadline_at:'2099-11-03T00:00:00Z',lineage:[{scope_id:id(2),epoch:0}]}
const rootRef={room_id:501,event_id:root,seq:1,actor_type:'user' as const,actor_id:1,kind:'message' as const}
const fields={execution_binding:binding,root_ref:rootRef,scope_status:'active' as const,revocation_state:null}
const action=(n:number):ToolActivity=>({action_id:id(n),room_id:501,agent_id:8,trigger_event_id:root,family:'browser_screenshot',target,expected_effect:effect,state:'waiting',scope:null,grant_id:null,requested_at:'2026-11-03T00:00:00Z',started_at:null,completed_at:null,review_id:id(n+100),...fields})
const preview=(a:ToolActivity):ActionPreview=>({action_id:a.action_id,family:a.family,target,scope_target:target,scope_description:'One reviewed capture',expected_effect:effect,workflow_id:null,arguments:{url:target},payload_digest:digest,review_id:a.review_id!,...fields,approval_policy:{scopes:['once'],unverified_context_required:false,start_within_seconds:60},review:{version:2,captured_at:'2026-11-03T00:00:00Z',action:{action_id:a.action_id,room_id:501,agent_id:8,trigger_event_id:root,family:a.family,target,scope_target:target,expected_effect:effect,payload_digest:digest,execution_binding:binding},authority:{owner_id:1,room_id:501,agent_id:8,proposer:{owner_id:1,room_id:501,agent_id:8,connection_id:1,provider_id:'synthetic',model_id:'synthetic-vision',config_revision:1,auth_grant_revision:1}},targets:[{role:'browser_context',status:'verified',room_id:501,agent_id:8,session_id:id(8),workflow_id:null,context_id:'c'.repeat(32),profile:'persistent_room',profile_id:profile,profile_revision:22,profile_label:'Synthetic held profile'}],external_context:{account:'unsupported',target_version:'unsupported'}}})
type Scenario='approval'|'decisionError'|'history'
function Surface({scenario}:{scenario:Scenario}) {
 const [ready,setReady]=useState(false),[number,setNumber]=useState(10),[decision,setDecision]=useState<boolean|null>(null),[submitted,setSubmitted]=useState(false)
 const refresh=useCallback(()=>{},[]), stop=useCallback(()=>{},[])
 useLayoutEffect(()=>{
  useAuthStore.getState().setAuth('synthetic-token',{id:1,email:'preview@example.invalid',username:'Preview'})
  const savedPreview=toolsApi.preview,savedDecide=toolsApi.decide
  toolsApi.preview=async(room,actionId)=>{if(room!==501||actionId!==id(number))throw Error('Synthetic target mismatch');return structuredClone(preview(action(number)))}
  toolsApi.decide=async(room,actionId,choice,scope,reviewId,_ack,send=false)=>{if(room!==501||actionId!==id(number)||choice!=='approve'||scope!=='once'||reviewId!==id(number+100))throw Error('Synthetic decision mismatch');if(scenario==='decisionError')throw Error('Synthetic response unknown');setDecision(send);setSubmitted(true);return {}}
  setReady(true);return()=>{toolsApi.preview=savedPreview;toolsApi.decide=savedDecide}
 },[number,scenario])
 const history:ToolActivity[]=[{...action(20),state:'completed',send_image_to_model:null},{...action(21),state:'completed',send_image_to_model:false},{...action(22),state:'completed',send_image_to_model:true},{...action(23),state:'uncertain',send_image_to_model:true},{...action(24),state:'failed',send_image_to_model:true}]
 return <MemoryRouter><I18nProvider><main className="page-frame page-content stack"><p>합성 승인과 이력 · 캡처나 모델 전송을 실행하지 않습니다.</p>{ready&&(scenario==='history'?history.map(row=><section className="surface" key={row.action_id}><ToolActivityRecord action={row} agentName="합성 Bot" /></section>):<>{!submitted?<GrantPrompt key={number} roomId={501} roomName="Synthetic room" agentName="합성 Bot" action={action(number)} onRefresh={refresh} onStop={stop}/>:<><p>합성 승인 선택 기록: {decision?'이번 캡처 이미지 전달 허용':'이미지 전달 선택 안 함'} · 캡처 또는 전송 완료 영수증이 아닙니다.</p><button type="button" onClick={()=>{setNumber(n=>n+1);setSubmitted(false);setDecision(null)}}>다음 합성 캡처 검토</button></>}</>)}</main></I18nProvider></MemoryRouter>
}
const meta={title:'Screens/Held screenshot consent',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const Approval:Story={args:{scenario:'approval'}}
export const DecisionUnknown:Story={args:{scenario:'decisionError'}}
export const History:Story={args:{scenario:'history'}}
