import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useState } from 'react'
import { waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { sha256Bytes, type VoiceMemo, type VoiceMemoLoader } from '../src/api/voiceMemos'
import VoiceMemoArtifact from '../src/components/VoiceMemoArtifact'
import { installNativeFixture } from './native'

type Mode='available'|'unavailable'|'cancelled'|'changed'|'unknown'|'saved'
// Synthetic silence only. The fixture bridge never opens an OS panel or writes a file.
function fixture():ArrayBuffer {const frames=1000,data=new ArrayBuffer(44+frames*2),v=new DataView(data);const put=(at:number,s:string)=>[...s].forEach((c,i)=>v.setUint8(at+i,c.charCodeAt(0)));put(0,'RIFF');v.setUint32(4,data.byteLength-8,true);put(8,'WAVE');put(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,22050,true);v.setUint32(28,44100,true);v.setUint16(32,2,true);v.setUint16(34,16,true);put(36,'data');v.setUint32(40,frames*2,true);return data}
function Surface({mode}:{mode:Mode}) {
 const [memo,setMemo]=useState<VoiceMemo|null>(null),[epoch,setEpoch]=useState(0),[ready,setReady]=useState(false)
 useLayoutEffect(()=>{let active=true;const bytes=fixture(),text='합성 예시 음성 메모입니다.';useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'});const nextEpoch=useAuthStore.getState().epoch;setEpoch(nextEpoch)
  const restore=mode==='unavailable'?()=>{}:installNativeFixture(async(command,args)=>{
   if(command==='workspace_export_transport')return 'native'
   if(command==='choose_binary_file_export')return mode==='cancelled'?null:{selection_id:'a'.repeat(32),filename:'synthetic.wav',replacing:false}
   if(command==='save_binary_file_export'){if(mode==='unknown')throw Error('synthetic lost receipt');return {path:'/synthetic/synthetic.wav',bytes:bytes.byteLength,replaced:false,durability_unconfirmed:false}}
   if(command==='discard_workspace_export')return undefined
   throw Error(`Unexpected synthetic command: ${command}`)
  })
  void Promise.all([sha256Bytes(bytes),sha256Bytes(new TextEncoder().encode(text))]).then(([sha,textSha])=>{if(!active)return;setMemo({version:1,owner_id:1,room_id:501,agent_id:7,trigger_event_id:'11111111-1111-4111-8111-111111111111',action_id:'22222222-2222-4222-8222-222222222222',review_id:'33333333-3333-4333-8333-333333333333',grant_id:1,generation:1,media_type:'audio/wav',size_bytes:bytes.byteLength,sha256:sha,duration_ms:45,transcript:text,text_sha256:textSha,voice_id:'synthetic',locale:'ko-KR',rate_milli:500,deleted:false});setReady(true)})
  return()=>{active=false;restore()}
 },[mode])
 const load:VoiceMemoLoader=async()=>{const bytes=fixture();if(mode==='changed')new DataView(bytes).setUint8(44,1);return {data:bytes,mediaType:'audio/wav'}}
 return <I18nProvider><main className="page-frame page-content">{ready&&memo&&<VoiceMemoArtifact memo={memo} source={{kind:'result',resultId:'44444444-4444-4444-8444-444444444444',version:1}} roomId={501} ownerId={1} epoch={epoch} canUse={()=>true} load={load}/>}</main></I18nProvider>
}
const meta={title:'Screens/음성 메모 기기 저장',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
const save=async({canvasElement}:{canvasElement:HTMLElement})=>{const canvas=within(canvasElement);await userEvent.click(await canvas.findByRole('button',{name:/기기에 저장|Save to device/}));await waitFor(()=>{if(!canvas.queryByRole('alert') && !canvas.queryByText(/저장을 취소했습니다|Save cancelled|saved|저장했습니다/i))throw Error('Synthetic save status pending')})}
export const Available:Story={args:{mode:'available'}}
export const Unavailable:Story={args:{mode:'unavailable'}}
export const Cancelled:Story={args:{mode:'cancelled'},play:save}
export const Changed:Story={args:{mode:'changed'},play:save}
export const Unknown:Story={args:{mode:'unknown'},play:save}
export const Saved:Story={args:{mode:'saved'},play:save}
