import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useState } from 'react'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './networkGuard'
import { installNativeFixture } from './native'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { agentsApi } from '../src/api/agents'
import { libraryUploadsApi } from '../src/api/libraryUploads'
import { botLibraryApi } from '../src/api/botLibrary'
import { roomResultsApi, type ResultDetail } from '../src/api/roomResults'
import { readResultRoute } from '../src/lib/resultLink'
import RoomResults from '../src/components/RoomResults'
import BotLibrary from '../src/pages/BotLibrary'
import type { RoomEvent } from '../src/api/events'

type Mode='ready'|'read-failure'|'delete-unknown'
const room=501,agent=8,action='11111111-1111-4111-8111-111111111111',eventId='33333333-3333-4333-8333-333333333333',resultId='22222222-2222-4222-8222-222222222222'
const content='name,count\napple,2\npear,3\n',sha='181145eaba28d06f3102945041c6056be756ccd3cabd2e5b259a08a025b5e001',created='2026-10-31T00:00:00Z'
const ref={version:1 as const,ref:`generated-files/${room}/${action}.utf8`,filename:'fruit.csv',size_bytes:new TextEncoder().encode(content).length,sha256:sha,media_type:'text/plain; charset=utf-8' as const,capture_kind:'approved_write_payload' as const}
const event:RoomEvent={event_id:eventId,room_id:room,seq:1,device_id:null,actor_type:'tool',actor_id:null,actor_name_snapshot:'',kind:'tool_result',created_at:created,idempotency_key:`tool-result:${action}`,payload:{state:'completed',action_id:action,agent_id:agent,generated_file:ref}}
const summary={room_id:room,result_id:resultId,version:1,kind:'file' as const,title:'fruit.csv',author_agent_id:agent,author_name_snapshot:'',state:'draft' as const,state_revision:1,created_at:created,updated_at:created,linked_opinion_count:0}
function ResultScreen(){const location=useLocation(),navigate=useNavigate(),auth=useAuthStore.getState();return <main className="page-frame page-frame--medium page-content"><RoomResults roomId={room} ownerId={auth.user!.id} authEpoch={auth.epoch} active visible events={[event]} source={null} routeTarget={readResultRoute(location.search)} routeEntry={location.key} onClearSource={()=>{}} onNavigate={(seq,id)=>navigate(`/chat/${room}?seq=${seq}&event=${id}`)}/></main>}
function StorySurface({mode}:{mode:Mode}){
 const [client]=useState(()=>{useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'});let removed=false
  agentsApi.get=async()=>({id:agent,user_id:1,name:'보고 Bot',description:'',system_prompt:'',model:'synthetic',connection_id:null,model_id:null,temperature:'auto',max_tokens:0,is_active:true,created_at:created,updated_at:null})
  libraryUploadsApi.list=async()=>({bot_id:agent,items:[],next_cursor:null})
  botLibraryApi.list=async()=>({agent_id:agent,items:[{...summary,source_href:`/chat/${room}?seq=1&event=${eventId}`,detail_href:`/conversations/${room}/results/${resultId}/versions/1`}],next_cursor:null})
  roomResultsApi.list=async()=>({room_id:room,items:[summary],next_cursor:null})
  roomResultsApi.versions=async()=>({room_id:room,result_id:resultId,current_version:1,items:[summary],next_before_version:null})
  roomResultsApi.reviews=async()=>({items:[],next_cursor:null})
  roomResultsApi.detail=async()=>({...summary,current_version:1,source:{event_id:eventId,seq:1,href:`/chat/${room}?seq=1&event=${eventId}`},execution:{trigger_event_id:null,round:null,generation:null,model_use:null},content:'',file_ref:{adapter:'generated_file',action_id:action,event_seq:1,...ref},file_status:removed?'removed':'unchecked',evidence:[],evidence_status:'none',review_support:'unsupported',link_report:null,state_owner_id:null,state_changed_at:null,link_report_owner_id:null,link_reported_at:null} satisfies ResultDetail)
  roomResultsApi.generatedFile=async()=>{if(mode==='read-failure')throw Error('synthetic read failure');if(removed)throw {response:{status:410}};const bytes=new TextEncoder().encode(content);return {data:bytes.buffer,headers:{'content-type':'text/plain; charset=utf-8'}} as never}
  roomResultsApi.removeGeneratedFile=async()=>{removed=true;if(mode==='delete-unknown')throw Error('synthetic lost response');return {removed:true}}
  return new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})})
 return <I18nProvider><QueryClientProvider client={client}><MemoryRouter initialEntries={[`/agents/${agent}/library`]}><Routes><Route path="/agents/:agentId/library" element={<BotLibrary/>}/><Route path="/chat/:room" element={<ResultScreen/>}/></Routes></MemoryRouter></QueryClientProvider></I18nProvider>
}
const meta={title:'Screens/생성 파일 결과',component:StorySurface,args:{mode:'ready'}} satisfies Meta<typeof StorySurface>
export default meta
type Story=StoryObj<typeof meta>
export const CsvReady:Story={name:'CSV · 보관함에서 열기와 삭제',args:{mode:'ready'}}
export const CsvReadFailure:Story={name:'CSV · 저장 사본 읽기 실패',args:{mode:'read-failure'}}
export const CsvDeleteUnknown:Story={name:'CSV · 삭제 결과 불명과 상태 확인',args:{mode:'delete-unknown'}}


// Synthetic native transport only: this Story never opens an OS panel or writes a file.
function NativeSaveSurface({outcome}:{outcome:'saved'|'cancelled'|'unknown'}) {
 const [ready,setReady]=useState(false)
 useLayoutEffect(()=>{
  const restore=installNativeFixture(async(command:string,args?:Record<string,unknown>)=>{
   if(command==='workspace_export_transport')return 'native'
   if(command==='choose_generated_file_export')return outcome==='cancelled'?null:{selection_id:'a'.repeat(32),filename:'fruit.csv',replacing:false}
   if(command==='save_generated_file_export'){
    if(outcome==='unknown')throw Error('Synthetic lost save response')
    return {path:'/synthetic/fruit.csv',bytes:new TextEncoder().encode(String(args?.content??'')).length,replaced:false,durability_unconfirmed:false}
   }
   if(command==='discard_workspace_export')return undefined
   throw Error('Unexpected synthetic native command')
  })
  setReady(true)
  return restore
 },[outcome])
 return ready?<StorySurface mode="ready"/>:null
}
export const NativeSave:Story={name:'CSV · 네이티브 저장 성공 (합성 IPC)',render:()=> <NativeSaveSurface outcome="saved"/>}
export const NativeCancel:Story={name:'CSV · 네이티브 저장 취소 (합성 IPC)',render:()=> <NativeSaveSurface outcome="cancelled"/>}
export const NativeUnknown:Story={name:'CSV · 네이티브 저장 결과 불명 (합성 IPC)',render:()=> <NativeSaveSurface outcome="unknown"/>}
