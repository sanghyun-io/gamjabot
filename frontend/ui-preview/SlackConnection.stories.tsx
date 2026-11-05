import type { Meta, StoryObj } from '@storybook/react'
import { useMemo, useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import '../src/index.css'
import { SlackConnectionPanel, SlackTaskList, SlackChannelSetup } from '../src/components/SlackConnection'
import type { SlackAdapter, SlackStatus, SlackFlowAdapter, SlackConfig } from '../src/api/slackConnection'

const account='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',operation='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const base:SlackStatus={version:1,owner_id:1,owner_epoch:'a'.repeat(64),core_epoch:'b'.repeat(64),state:'missing',account_id:null,revision:0,operation_id:null,pending:null}
function Fixture({initial='missing',unknown=false}:{initial?:'missing'|'stored'|'uncertain';unknown?:boolean}){
 const [identity]=useState(()=>crypto.randomUUID());const adapter=useMemo<SlackAdapter>(()=>{let value:SlackStatus=initial==='stored'?{...base,state:'stored',account_id:account,revision:1,operation_id:operation}:initial==='uncertain'?{...base,state:'uncertain',pending:{version:1,owner_id:1,owner_epoch:base.owner_epoch,core_epoch:base.core_epoch,account_id:account,expected_revision:0,operation_id:operation,target_state:'stored'}}:{...base};return {status:async()=>value,store:async p=>{value={...base,state:'stored',account_id:p.accountId,revision:p.expectedRevision+1,operation_id:p.operationId};if(unknown)throw Error('synthetic lost response');return value},remove:async p=>{value={...base,state:'deleted',account_id:p.accountId,revision:p.expectedRevision+1,operation_id:p.operationId};if(unknown)throw Error('synthetic lost response');return value}}},[])
 return <main className="page-frame page-content"><SlackConnectionPanel ownerId={1} identity={identity} canUse={()=>true} adapter={adapter}/></main>
}
const meta:Meta<typeof Fixture>={title:'Product/Slack credential preparation',component:Fixture}
export default meta
type Story=StoryObj<typeof Fixture>
export const Missing:Story={args:{initial:'missing'}}
export const Stored:Story={args:{initial:'stored'}}
export const Unknown:Story={args:{initial:'missing',unknown:true}}
export const RestartUncertain:Story={args:{initial:'uncertain'}}

const taskBase={event_id:'EvSynthetic',channel_id:'CSYNTHETIC',sender_id:'USYNTHETIC',thread_ts:'123.456789',room_id:123,root_event_id:'00000000-0000-4000-8000-000000000001'}
export const ActivityEmpty:Story={render:()=> <MemoryRouter><SlackTaskList items={[]}/></MemoryRouter>}
export const ActivityApproval:Story={render:()=> <MemoryRouter><SlackTaskList items={[{...taskBase,state:'waiting_approval',reply_state:'pending'}]}/></MemoryRouter>}
export const ActivitySendUnknown:Story={render:()=> <MemoryRouter><SlackTaskList items={[{...taskBase,state:'completed',reply_state:'unknown'}]}/></MemoryRouter>}

function FlowFixture({scenario}:{scenario:'empty'|'normal'|'connectfail'|'permission'|'approval'|'sendunknown'|'workunknown'}){
 const adapter=useMemo<SlackFlowAdapter>(()=>{let config:SlackConfig=scenario==='empty'?{version:1,revision:0,state:'unconfigured'}:{version:1,revision:1,state:'configured',operation_id:operation,account_id:account,credential_revision:1,enabled:true,workspace_id:'TSYNTHETIC',app_id:'ASYNTHETIC',channel_id:'CSYNTHETIC',sender_ids:['USYNTHETIC'],agent_id:1,project_id:2,connection_id:3,model_id:'synthetic-model'};let recorded:Awaited<ReturnType<SlackFlowAdapter['resolve']>>|null=null;return {targets:async()=>[{agent_id:1,agent_name:'합성 Bot',project_id:2,project_name:'합성 프로젝트',connection_id:3,connection_name:'합성 공급자',model_id:'synthetic-model'}],config:async()=>config,transport:async()=>scenario==='connectfail'?'disconnected':scenario==='permission'?'permission_required':scenario==='normal'?'connected':'unconfirmed',events:async()=>scenario==='approval'?[{...taskBase,workspace_id:'TSYNTHETIC',state:'waiting_approval',reply_state:'pending',result_event_id:null}]:scenario==='sendunknown'?[{...taskBase,workspace_id:'TSYNTHETIC',state:'completed',reply_state:'unknown',result_event_id:'00000000-0000-4000-8000-000000000002'}]:[],unresolved:async()=>({next_cursor:null,items:(scenario==='sendunknown'||scenario==='workunknown')&&recorded?.decision!=='owner_confirmed_sent'?[{run_id:'00000000-0000-4000-8000-000000000003',event_id:taskBase.event_id,workspace_id:'TSYNTHETIC',channel_id:taskBase.channel_id,thread_ts:taskBase.thread_ts,sender_id:taskBase.sender_id,state:'unknown',attempt_id:scenario==='workunknown'?null:'00000000-0000-4000-8000-000000000004',config_revision:1,result_text:scenario==='workunknown'?null:'확인할 합성 답장'}]:[]}),resolve:async(id,input)=>{recorded={version:1,run_id:id,operation_id:input.operation_id,decision:input.decision};return recorded},resolution:async()=>{if(!recorded)throw Error('not found');return recorded},save:async p=>{config={version:1,revision:p.expected_revision+1,state:'configured',operation_id:p.operation_id,account_id:p.account_id,credential_revision:p.credential_revision,enabled:p.enabled,workspace_id:p.workspace_id,app_id:p.app_id,channel_id:p.channel_id,sender_ids:p.sender_ids,agent_id:p.agent_id,project_id:p.project_id,connection_id:p.connection_id,model_id:p.model_id};return config},reconcile:async()=>config,command:async id=>({operation_id:id,revision:config.revision,state:config.state==='configured'?'configured':'pending'})}},[scenario])
 const status:SlackStatus={...base,state:'stored',account_id:account,revision:1,operation_id:operation};return <MemoryRouter><main className="page-frame page-content"><SlackChannelSetup status={status} identity={`synthetic-${scenario}`} canUse={()=>true} adapter={adapter}/></main></MemoryRouter>
}
export const FlowEmpty:Story={render:()=> <FlowFixture scenario="empty"/>}
export const FlowConnected:Story={render:()=> <FlowFixture scenario="normal"/>}
export const FlowConnectFailed:Story={render:()=> <FlowFixture scenario="connectfail"/>}
export const FlowApproval:Story={render:()=> <FlowFixture scenario="approval"/>}
export const FlowSendUnknown:Story={render:()=> <FlowFixture scenario="sendunknown"/>}
export const FlowPermissionRequired:Story={render:()=> <FlowFixture scenario="permission"/>}

export const FlowWorkUnknown:Story={render:()=> <FlowFixture scenario="workunknown"/>}
