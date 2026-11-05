import type { Meta, StoryObj } from '@storybook/react-vite'
import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { agentsApi } from '../src/api/agents'
import { botLibraryApi, type BotLibraryItem } from '../src/api/botLibrary'
import { libraryUploadsApi, type LibraryUpload } from '../src/api/libraryUploads'
import { libraryPinsApi, pinKey, type PinOperation, type PinSource, type PinStatus } from '../src/api/libraryPins'
import { roomResultsApi } from '../src/api/roomResults'
import BotLibrary from '../src/pages/BotLibrary'

const bot=8, rid='af041000-0000-4000-8000-000000000801',event='af041000-0000-4000-8000-000000000810', upload='11111111-1111-4111-8111-111111111111', sha='a'.repeat(64),created='2026-10-30T00:00:00Z'
const result:BotLibraryItem={result_id:rid,room_id:501,version:2,kind:'page',title:'합성 주간 보고서',author_agent_id:bot,author_name_snapshot:'Bot',state:'draft',state_revision:1,created_at:created,updated_at:created,linked_opinion_count:0,source_href:`/chat/501?seq=1&event=${event}`,detail_href:`/conversations/501/results/${rid}/versions/2`}
const file:LibraryUpload={upload_id:upload,bot_id:bot,filename:'합성 자료.pdf',size_bytes:24,sha256:sha,created_at:created,media_type:'application/octet-stream',origin:'owner_upload',removed:false}
const pageSource:PinSource={kind:'result',room_id:501,result_id:rid,version:2},uploadSource:PinSource={kind:'upload',upload_id:upload,sha256:sha}
type Mode='ready'|'unknown'|'removed'
function PinStory({mode}:{mode:Mode}){
 const [client]=useState(()=>{
  useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'})
  sessionStorage.removeItem('gamja.library-pin.1.8')
  let rows:PinStatus[]=[{source:pageSource,revision:1,pinned:true,availability:'available'},{source:uploadSource,revision:1,pinned:true,availability:mode==='removed'?'removed':'available'}]
  agentsApi.get=async()=>({id:bot,user_id:1,name:'보고 Bot'} as never)
  botLibraryApi.list=async()=>({agent_id:bot,items:[result],next_cursor:null})
  libraryUploadsApi.list=async()=>({bot_id:bot,items:[file],next_cursor:null})
  libraryUploadsApi.read=async()=>mode==='removed'?{...file,removed:true}:file
  roomResultsApi.detail=async()=>({...result} as never)
  libraryPinsApi.context=async()=>({owner_id:1,owner_epoch:'b'.repeat(64)})
  libraryPinsApi.list=async()=>({items:rows.filter(x=>x.pinned),next_cursor:null})
  libraryPinsApi.status=async(_bot,source)=>rows.find(x=>pinKey(x.source)===pinKey(source))||{source,revision:0,pinned:false,availability:'available'}
  const reply=(op:PinOperation,state:'applied'|'fenced')=>({state,revision:state==='applied'?op.expected_revision+1:null,pinned:state==='applied'?op.pinned:null,code:null,operation_id:op.operation_id,source:op.source,expected_revision:op.expected_revision,requested_pinned:op.pinned,expected_owner_epoch:op.expected_owner_epoch})
  libraryPinsApi.operate=async(_bot,op)=>{if(mode==='unknown')throw Error('synthetic lost response');rows=[...rows.filter(x=>pinKey(x.source)!==pinKey(op.source)),{source:op.source,revision:op.expected_revision+1,pinned:op.pinned,availability:'available'}];return reply(op,'applied')}
  libraryPinsApi.resolve=async(_bot,op)=>reply(op,'fenced')
  return new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})
 })
 return <I18nProvider systemLanguages={()=>['ko']}><QueryClientProvider client={client}><MemoryRouter initialEntries={['/agents/8/library']}><Routes><Route path="/agents/:agentId/library" element={<BotLibrary/>}/></Routes></MemoryRouter></QueryClientProvider></I18nProvider>
}
const meta={title:'Screens/보관함 고정',component:PinStory,args:{mode:'ready'}} satisfies Meta<typeof PinStory>
export default meta
type Story=StoryObj<typeof meta>
export const Ready:Story={name:'고정된 Page와 업로드',args:{mode:'ready'}}
export const Removed:Story={name:'삭제된 원본 · 고정 해제 가능',args:{mode:'removed'}}
export const Unknown:Story={name:'고정 변경 응답 유실 · 요청 확인',args:{mode:'unknown'}}
