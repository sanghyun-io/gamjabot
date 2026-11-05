import './networkGuard'
import '../src/index.css'
import { useRef, useState } from 'react'
import type { Meta, StoryObj } from '@storybook/react'
import { I18nProvider } from '../src/i18n'
import { TeamService, SharedRoomStatus, type TeamServiceAdapter } from '../src/components/TeamService'
import type { TeamCommand, TeamSession, PendingTeamOperation, SharedRoomPointer, SharedRoom } from '../src/api/teamService'

const team = '11111111-1111-4111-8111-111111111111', account = '22222222-2222-4222-8222-222222222222', generation = '33333333-3333-4333-8333-333333333333', publication = '44444444-4444-4444-8444-444444444444'
const connected: TeamSession = {state:'team_ready',team_id:team,account_id:account,session_generation:generation,shared_execution_available:false,profile_id:'example',issuer:'https://example.invalid'}
type Scenario = 'unconfigured'|'login'|'manager'|'member'|'unknown'|'secret'|'revoked'|'shared'|'shared-open'|'shared-unknown'|'shared-revoked'|'room-ready'|'room-unavailable'|'room-unknown'
function fixture(scenario:Scenario): TeamServiceAdapter {
  const profile = {profile_id:'example',display_name:'Example team service',base_url:'https://example.invalid',issuer:'https://example.invalid',key_fingerprint:'synthetic',ready:true}
  let pending:PendingTeamOperation[] = []
  let sharedPending:SharedRoomPointer[]=[]
  let sharedRoom:SharedRoom|null=null
  const isConnected = !['unconfigured','login'].includes(scenario)
  let currentSession:TeamSession = isConnected ? {...connected,shared_text_execution_available:scenario.startsWith('shared-')}  : {state:'disconnected',shared_execution_available:false}
  return {
    sharedPendingStore:{read:()=>[...sharedPending],add:value=>{sharedPending=[...sharedPending,value]},remove:value=>{sharedPending=sharedPending.filter(x=>x.publication!==value.publication)}},
    sharedOptions:async()=>({projects:[{id:1,name:'내 프로젝트'}],connections:[{id:2,label:'내 연결',model_id:'example-model'}]}),
    sharedRead:async()=>{if(scenario==='shared-revoked')throw new Error('unavailable');if(!sharedRoom)throw {response:{status:404,data:{detail:{code:'shared_room_not_found'}}}};return sharedRoom},
    sharedOpen:async(_team,selection)=>{sharedRoom={...selection,team_id:team,owner_id:42,account_id:account,issuer:'https://example.invalid',state:'ready',room_id:88,agent_id:9,text_only:true};if(scenario==='shared-unknown')throw new Error('synthetic response lost');return sharedRoom},
    openChat:()=>{},
    profiles:async()=>scenario==='unconfigured'?[]:[profile],
    pendingStore:{read:()=>[...pending],add:value=>{pending=[...pending,value]},remove:value=>{pending=pending.filter(x=>x.id!==value.id)}},
    status:async()=>currentSession,
    login:async()=>{currentSession={...connected,state:'authenticated',team_id:null};return currentSession},activate:async()=>{},logout:async()=>{currentSession={state:'disconnected',shared_execution_available:false};return {state:'disconnected',remote_session_revoked:true}},select:async(_generation,teamId)=>{currentSession={...connected,team_id:teamId,session_generation:crypto.randomUUID()};return currentSession},copy:async()=>{},confirm:()=>true,
    command:async (_generation:string,operation:TeamCommand,payload:Record<string,unknown>)=>{
      if (operation==='list_teams') return {items:[{team_id:team,name:'Example team',role:scenario==='member'?'member':'owner',revision:3}],truncated:false}
      if (operation==='list_members') return {team_id:team,revision:3,items:[{account_id:account,username:'Sample',role:scenario==='member'?'member':'owner',state:'active'}],truncated:false}
      if (operation==='list_invitations') return {items:[],truncated:false}
      if (operation==='list_sent_invitations') return {team_id:team,team_revision:3,items:scenario==='revoked'?[{invite_id:publication,username:'Sample',state:'revoked',expires_at:1893456000}]:[],truncated:false}
      if (operation==='list_publications') return {items:[{publication_id:publication,owner_id:account,revision:1,your_role:'owner'}],truncated:false}
      if (operation==='get_publication') return {publication_id:publication,revision:1,role_text:'합성 역할 예시',knowledge_text:'합성 공유 지식',audience:'team',people:[],your_role:'owner'}
      if (operation==='get_invite') return {invite_id:payload.invite_id,state:scenario==='revoked'?'revoked':'pending',expires_at:1893456000,team_revision:3}
      if (scenario==='unknown') throw new Error('synthetic connection loss')
      if (operation==='invite_new') return {invite_id:String(payload.id),invite_code:'SYNTHETIC-INVITATION-CODE',state:'pending'}
      if (operation==='invite_existing') return {invite_id:payload.id,state:'pending'}
      if (operation==='revoke_invite'||operation==='revoke_member'||operation==='revoke_publication') return {publication_id:payload.publication_id,state:'revoked'}
      if (operation==='accept_invite') return {state:'accepted'}
      if (operation==='put_publication') return {publication_id:payload.publication_id,revision:1}
      if (operation==='create_team') return {team_id:payload.id}
      return {}
    },
    receipt:async(_generation,id)=>({operation_id:id,team_id:team,state:'committed',kind:'invite.enrollment',result:{token:null}}),
  }
}
function Example({scenario}:{scenario:Scenario}) { const adapter=useRef(fixture(scenario)).current; const [mount,setMount]=useState(0); if(scenario.startsWith('room-'))return <I18nProvider><SharedRoomStatus state={scenario==='room-ready'?'ready':scenario==='room-unavailable'?'unavailable':'unknown'} ko onRefresh={()=>{}}/></I18nProvider>; return <I18nProvider><button type="button" className="button button--secondary" onClick={()=>setMount(x=>x+1)}>합성 화면 다시 열기</button><TeamService key={mount} identity={`synthetic:${scenario}`} ownerId={42} adapter={adapter}/></I18nProvider> }
const meta:Meta<typeof Example>={title:'Teams/Team service',component:Example}
export default meta
type Story=StoryObj<typeof Example>
export const Unconfigured:Story={args:{scenario:'unconfigured'}}
export const Login:Story={args:{scenario:'login'}}
export const Manager:Story={args:{scenario:'manager'}}
export const Member:Story={args:{scenario:'member'}}
export const InvitationFirstResponse:Story={args:{scenario:'secret'}}
export const InvitationRevoked:Story={args:{scenario:'revoked'}}
export const OperationUnknown:Story={args:{scenario:'unknown'}}
export const SharedSettings:Story={args:{scenario:'shared'}}

export const SharedConversation:Story={args:{scenario:'shared-open'}}
export const SharedConversationUnknown:Story={args:{scenario:'shared-unknown'}}
export const SharedConversationUnavailable:Story={args:{scenario:'shared-revoked'}}
export const SharedRoomReady:Story={args:{scenario:'room-ready'}}
export const SharedRoomUnavailable:Story={args:{scenario:'room-unavailable'}}
export const SharedRoomUnknown:Story={args:{scenario:'room-unknown'}}
