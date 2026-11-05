import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useState } from 'react'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { toolsApi, type ActionPreview, type ToolActivity } from '../src/api/tools'
import GrantPrompt from '../src/components/GrantPrompt'

// Synthetic review only: no native browser, credentials, or network.
const actionId='b2000000-0000-4000-8000-000000000012',reviewId='b2000000-0000-4000-8000-000000000013',rootId='b2000000-0000-4000-8000-000000000011'
const target='vault-login:0123456789abcdef0123456789abcdef',effect='Open a secure login handoff',digest='a'.repeat(64)
const action:ToolActivity={action_id:actionId,room_id:501,agent_id:8,trigger_event_id:rootId,family:'request_vault_login',target,expected_effect:effect,state:'waiting',scope:null,grant_id:null,requested_at:'2026-11-02T00:00:00Z',started_at:null,completed_at:null,review_id:reviewId}
const preview:ActionPreview={action_id:actionId,family:action.family,target,scope_target:target,scope_description:'One reviewed synthetic request',expected_effect:effect,workflow_id:null,arguments:{item_id:'0123456789abcdef0123456789abcdef'},payload_digest:digest,review_id:reviewId,approval_policy:{scopes:['once'],unverified_context_required:true,start_within_seconds:60},review:{version:1,captured_at:'2026-11-02T00:00:00Z',action:{action_id:actionId,room_id:501,agent_id:8,trigger_event_id:rootId,family:action.family,target,scope_target:target,expected_effect:effect,payload_digest:digest},authority:{owner_id:1,room_id:501,agent_id:8,proposer:{owner_id:1,room_id:501,agent_id:8,connection_id:2,provider_id:'synthetic',model_id:'synthetic'}},targets:[{role:'vault_profile',status:'verified',profile_id:'00112233445566778899aabbccddeeff',profile_revision:3,profile_label:'QA Go profile',profile_origins:[['https','example.invalid',443]],account_label:'QA account',item_id:'0123456789abcdef0123456789abcdef',item_revision:2,secret_revision:1,origin:'https://example.invalid',username:'demo@example.invalid'}],external_context:{account:'unsupported',target_version:'unsupported'}}}
const noop=()=>{}
function Surface(){
 const [ready,setReady]=useState(false)
 useLayoutEffect(()=>{useAuthStore.getState().setAuth('synthetic-token',{id:1,email:'preview@example.invalid',username:'Preview'});const saved=toolsApi.preview,savedDecision=toolsApi.decide;toolsApi.preview=async()=>structuredClone(preview);toolsApi.decide=async()=>({});setReady(true);return()=>{toolsApi.preview=saved;toolsApi.decide=savedDecision}},[])
 return <I18nProvider><main className="page-frame page-content"><p>합성 승인 카드 · 실제 입력이나 네트워크 없음</p>{ready&&<GrantPrompt roomId={501} roomName="Synthetic room" agentName="QA Bot" action={action} onRefresh={noop} onStop={noop}/>}</main></I18nProvider>
}
const meta={title:'Screens/Vault approval',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const ReviewedLogin:Story={}
