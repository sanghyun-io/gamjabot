import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useMemo, useState } from 'react'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import SkillRunComposer, { type SkillRunBridge } from '../src/components/SkillRunComposer'
import type { SkillRunInput } from '../src/api/skillRun'
import type { RoomEvent } from '../src/api/events'

type Scenario = 'ready'|'stale'|'unknown'|'empty'|'mismatch'
const digest=async(value:string)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value))),b=>b.toString(16).padStart(2,'0')).join('')
const canonical=(value:Record<string,unknown>)=>JSON.stringify(Object.fromEntries(Object.keys(value).sort().map(k=>[k,value[k]])))
const skillId='11111111-1111-4111-8111-111111111111', eventId='22222222-2222-4222-8222-222222222222'
export function Surface({scenario}:{scenario:Scenario}) {
  const [epoch,setEpoch]=useState<number|null>(null),[event,setEvent]=useState<RoomEvent|null>(null),[version,setVersion]=useState(0),[sendCalls,setSendCalls]=useState(0)
  useLayoutEffect(()=>{useAuthStore.getState().setAuth('synthetic-token',{id:1,email:'preview@example.invalid',username:'Preview'});setEpoch(useAuthStore.getState().epoch)},[])
  const bridge=useMemo<SkillRunBridge>(()=>{
    const map=new Map<string,string>();let saved:RoomEvent|null=null
    const store={getItem:(key:string)=>map.get(key)??null,setItem:(key:string,value:string)=>{map.set(key,value)},removeItem:(key:string)=>{map.delete(key)}}
    const build=async(b:SkillRunInput)=>{const target={bot_id:b.bot_id,bot_name:'합성 Bot',connection_id:1,connection_name:'합성 연결',model_id:'synthetic-model',provider_id:'synthetic'},expanded_reference=`주제: ${b.values.topic}\n사실과 다음 단계를 간결하게 정리합니다.`
      const proof={version:1,owner_id:1,room_id:501,bot_id:b.bot_id,owner_epoch_sha256:'b'.repeat(64),profile:'c'.repeat(64),memory:'d'.repeat(64),connection_id:1,model_id:'synthetic-model',connection_revision:1,auth_revision:0,skill_id:b.skill_id,skill_revision:b.skill_revision,binding_revision:b.binding_revision,question_sha256:await digest(b.question),values_sha256:await digest(canonical(b.values)),expanded_sha256:await digest(expanded_reference),target_sha256:await digest(canonical(target))}
      const basis=await digest(canonical(proof));return {proof,preview:{basis,skill_id:b.skill_id,skill_revision:b.skill_revision,binding_revision:b.binding_revision,bot_id:b.bot_id,question:b.question,proof,variables:['topic'],expanded_reference,target}}}

    return {store,skills:async()=>scenario==='empty'?[]:[{version:1,skill_id:skillId,revision:2,name:'요약하기',description:'합성 Skill',content:'주제: {{topic}}\n사실과 다음 단계를 간결하게 정리합니다.',active:true,source:null,created_at:'2026-11-01T00:00:00Z',updated_at:'2026-11-01T00:00:00Z'}],bindings:async bot=>({version:1,agent_id:bot,items:[{skill_id:skillId,revision:1,enabled:true}]}),api:{
      preview:async(_room,body)=>{const value=(await build(body)).preview;return scenario==='mismatch'?{...value,expanded_reference:'서로 다른 합성 내용'}:value},
      send:async(room,body,basis,key)=>{
        setSendCalls(value=>value+1)
        if(scenario==='stale')throw {response:{status:409,data:{detail:{code:'skill_input_skill_changed'}}}}
        const {proof,preview}=await build(body);if(preview.basis!==basis)throw Error('synthetic basis changed')
        saved={event_id:eventId,device_id:null,room_id:room,seq:1,actor_type:'user',actor_id:1,actor_name_snapshot:'Preview',kind:'message',payload:{content:body.question,skill_context:{version:1,bot_id:body.bot_id,skill_id:body.skill_id,skill_revision:body.skill_revision,binding_revision:body.binding_revision,values:body.values,basis,proof}},created_at:'2026-11-04T00:00:00Z',idempotency_key:`skill-input:${await digest(key)}`}
        if(scenario==='unknown')throw Error('synthetic response lost')
        return saved
      },receipt:async()=>{if(!saved)throw Error('synthetic not found');return saved},
    }}
  },[scenario])
  return <I18nProvider><main className="page-frame page-content stack"><p>합성 Skill과 가짜 응답만 사용합니다. 실제 모델·요청 전송은 없습니다.</p><p>합성 전송 {sendCalls}회</p><button type="button" className="button button--secondary" onClick={()=>setVersion(v=>v+1)}>합성 화면 다시 열기</button>{epoch!==null&&<SkillRunComposer key={version} roomId={501} ownerId={1} epoch={epoch} bots={[{id:8,name:'합성 Bot'}]} canUse={()=>true} onEvent={setEvent} bridge={bridge}/>} {event&&<p role="status">합성 대화에 질문이 추가됨: {String(event.payload.content)}</p>}</main></I18nProvider>
}
const meta={title:'Product/Skill one-time request',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const Ready:Story={args:{scenario:'ready'}}
export const Stale:Story={args:{scenario:'stale'}}
export const Unknown:Story={args:{scenario:'unknown'}}
export const Empty:Story={args:{scenario:'empty'}}
export const Mismatch:Story={args:{scenario:'mismatch'}}
