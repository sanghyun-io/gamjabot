import type { Meta, StoryObj } from '@storybook/react-vite'
import userEvent from '@testing-library/user-event'
import { waitFor } from '@testing-library/react'
import { useLayoutEffect, useState } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './networkGuard'
import { installNativeFixture } from './native'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { agentsApi } from '../src/api/agents'
import { botLibraryApi } from '../src/api/botLibrary'
import { libraryUploadsApi, type LibraryUpload } from '../src/api/libraryUploads'
import BotLibrary from '../src/pages/BotLibrary'

type Mode='ready'|'upload-unknown'|'upload-missing'|'delete-unknown'|'read-failure'
const bot=8,id='11111111-1111-4111-8111-111111111111',created='2026-10-31T00:00:00Z',content='%PDF-1.4\n% synthetic\n',sha='3cf7d2f974726b8cf563019d202e41ac808b2db80e654dadc7d62722b04e6665'
const initial:LibraryUpload={upload_id:id,bot_id:bot,filename:'sample.pdf',size_bytes:new TextEncoder().encode(content).length,sha256:sha,created_at:created,media_type:'application/octet-stream',origin:'owner_upload',removed:false}
function UploadStory({mode}:{mode:Mode}){
 const [client]=useState(()=>{useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'});let rows:LibraryUpload[]=[initial],contents=new Map([[id,new TextEncoder().encode(content)]]),missingAttempt=false
  agentsApi.get=async()=>({id:bot,user_id:1,name:'보고 Bot',description:'',system_prompt:'',model:'synthetic',connection_id:null,model_id:null,temperature:'auto',max_tokens:0,is_active:true,created_at:created,updated_at:null})
  botLibraryApi.list=async()=>({agent_id:bot,items:[],next_cursor:null})
  libraryUploadsApi.list=async()=>({bot_id:bot,items:rows,next_cursor:null})
  libraryUploadsApi.read=async(_bot,upload)=>{const row=rows.find(item=>item.upload_id===upload);if(!row)throw {isAxiosError:true,response:{status:404}};return row}
  libraryUploadsApi.put=async(_bot,upload,name,digest,bytes)=>{if(mode==='upload-missing'&&!missingAttempt){missingAttempt=true;throw Error('Synthetic lost response before save')}const row:LibraryUpload={...initial,upload_id:upload,filename:name,size_bytes:bytes.byteLength,sha256:digest};rows=[...rows,row].sort((a,b)=>a.upload_id.localeCompare(b.upload_id));contents.set(upload,new Uint8Array(bytes));if(mode==='upload-unknown')throw Error('Synthetic lost response');return row}
  libraryUploadsApi.file=async(_bot,upload)=>{if(mode==='read-failure')throw Error('Synthetic unavailable');const data=contents.get(upload);if(!data)throw Error('Synthetic missing');return {data:data.slice().buffer,headers:{'content-type':'application/octet-stream'}} as never}
  libraryUploadsApi.remove=async(_bot,upload)=>{rows=rows.map(row=>row.upload_id===upload?{...row,removed:true}:row);const row=rows.find(item=>item.upload_id===upload)!;if(mode==='delete-unknown')throw Error('Synthetic lost response');return row}
  return new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})})
 return <I18nProvider><QueryClientProvider client={client}><MemoryRouter initialEntries={[`/agents/${bot}/library`]}><Routes><Route path="/agents/:agentId/library" element={<BotLibrary/>}/></Routes></MemoryRouter></QueryClientProvider></I18nProvider>
}
const meta={title:'Screens/보관함 업로드',component:UploadStory,args:{mode:'ready'}} satisfies Meta<typeof UploadStory>
export default meta
type Story=StoryObj<typeof meta>
const selectFixture=async({canvasElement}:{canvasElement:HTMLElement})=>{const input=await waitFor(()=>{const element=canvasElement.querySelector<HTMLInputElement>('input[type=file]');if(!element)throw Error('Fixture input unavailable');return element});await userEvent.upload(input,new File([content],'synthetic.pdf',{type:'application/pdf'}))}
export const UploadSelected:Story={name:'Owner 파일 선택 · 명시 저장',args:{mode:'ready'},play:selectFixture}
export const UploadUnknown:Story={name:'Owner 업로드 · 결과 불명 조회',args:{mode:'upload-unknown'},play:selectFixture}
export const UploadMissing:Story={name:'Owner 업로드 · 미저장 확인 후 같은 파일 재시도',args:{mode:'upload-missing'},play:selectFixture}
export const DownloadReady:Story={name:'보관 파일 · 검증 후 다운로드',args:{mode:'ready'}}
export const DownloadFailure:Story={name:'보관 파일 · 읽기 실패',args:{mode:'read-failure'}}
export const DeleteUnknown:Story={name:'보관 파일 · 삭제 결과 불명',args:{mode:'delete-unknown'}}


// Workbench-only mock: no native bridge, OS panel, model request or disk write.
function NativeBinarySurface({outcome}:{outcome:'saved'|'cancelled'|'unknown'}) {
 const [ready,setReady]=useState(false)
 useLayoutEffect(()=>{
  const restore=installNativeFixture(async(command,args)=>{
   if(command==='workspace_export_transport')return 'native'
   if(command==='choose_binary_file_export')return outcome==='cancelled'?null:{selection_id:'b'.repeat(32),filename:'sample.pdf',replacing:false}
   if(command==='save_binary_file_export'){
    if(outcome==='unknown')throw Error('Synthetic lost write response')
    return {path:'/synthetic/sample.pdf',bytes:atob(String(args?.contentBase64??'')).length,replaced:false,durability_unconfirmed:false}
   }
   if(command==='discard_workspace_export')return undefined
   throw Error('Unexpected synthetic command')
  });setReady(true);return restore
 },[outcome])
 return ready?<UploadStory mode="ready"/>:null
}
export const NativeBinarySave:Story={name:'업로드 PDF · 네이티브 저장 성공 (합성 IPC)',render:()=> <NativeBinarySurface outcome="saved"/>}
export const NativeBinaryCancel:Story={name:'업로드 PDF · 네이티브 저장 취소 (합성 IPC)',render:()=> <NativeBinarySurface outcome="cancelled"/>}
export const NativeBinaryUnknown:Story={name:'업로드 PDF · 네이티브 저장 결과 불명 (합성 IPC)',render:()=> <NativeBinarySurface outcome="unknown"/>}
