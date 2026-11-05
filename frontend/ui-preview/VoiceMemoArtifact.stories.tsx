import type { Meta, StoryObj } from '@storybook/react-vite'
import { useEffect, useState } from 'react'
import { fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { sha256Bytes, type VoiceMemo, type VoiceMemoLoader } from '../src/api/voiceMemos'
import VoiceMemoArtifact from '../src/components/VoiceMemoArtifact'

type Mode = 'waiting' | 'ready' | 'player-error' | 'changed' | 'unknown'
// Synthetic PCM silence, never sent to native audio or played by the Story.
function fixture():ArrayBuffer { const frames=1000, data=new ArrayBuffer(44+frames*2),v=new DataView(data);const put=(at:number,s:string)=>[...s].forEach((c,i)=>v.setUint8(at+i,c.charCodeAt(0)));put(0,'RIFF');v.setUint32(4,data.byteLength-8,true);put(8,'WAVE');put(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);v.setUint32(24,22050,true);v.setUint32(28,44100,true);v.setUint16(32,2,true);v.setUint16(34,16,true);put(36,'data');v.setUint32(40,frames*2,true);return data }
function Surface({ mode }:{mode:Mode}) {
  const [state] = useState(()=>{ useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'});const epoch=useAuthStore.getState().epoch;return {epoch,bytes:fixture()} })
  const [memo,setMemo] = useState<VoiceMemo|null>(null)
  useEffect(()=>{ let active=true; const text='안녕하세요. 이것은 합성 예시입니다.'; void Promise.all([sha256Bytes(state.bytes),sha256Bytes(new TextEncoder().encode(text))]).then(([sha,textSha])=>{if(active)setMemo({version:1,owner_id:1,room_id:501,agent_id:7,trigger_event_id:'11111111-1111-4111-8111-111111111111',action_id:'22222222-2222-4222-8222-222222222222',review_id:'33333333-3333-4333-8333-333333333333',grant_id:1,generation:1,media_type:'audio/wav',size_bytes:state.bytes.byteLength,sha256:sha,duration_ms:45,transcript:text,text_sha256:textSha,voice_id:'synthetic',locale:'ko-KR',rate_milli:500,deleted:false})}); return()=>{active=false} },[state])
  const load:VoiceMemoLoader=async(_room,_memo,_source,signal)=>{
    if(mode==='waiting') return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(Error('aborted')),{once:true}))
    if(mode==='unknown') throw Error('synthetic unavailable')
    const bytes=state.bytes.slice(0);if(mode==='changed')new DataView(bytes).setUint8(44,1)
    return {data:bytes,mediaType:'audio/wav'}
  }
  return <I18nProvider><main className="page-frame page-content">{memo && <VoiceMemoArtifact memo={memo} source={{kind:'event'}} roomId={501} ownerId={1} epoch={state.epoch} canUse={()=>true} load={load}/>}</main></I18nProvider>
}
const meta={title:'Screens/음성 메모',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
const load = async ({canvasElement}:{canvasElement:HTMLElement})=>{const canvas=within(canvasElement);await userEvent.click(await canvas.findByRole('button',{name:/음성 불러오기|Load audio/}))}
const playerError = async (args:{canvasElement:HTMLElement})=>{await load(args);const audio=await waitFor(()=>{const item=args.canvasElement.querySelector('audio');if(!item)throw Error('Synthetic player not ready');return item});fireEvent.error(audio)}
export const Waiting:Story={args:{mode:'waiting'},play:load}
export const Ready:Story={args:{mode:'ready'},play:load}
export const PlayerError:Story={args:{mode:'player-error'},play:playerError}
export const Changed:Story={args:{mode:'changed'},play:load}
export const Unknown:Story={args:{mode:'unknown'},play:load}
