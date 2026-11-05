import './networkGuard'
import '../src/index.css'
import { useRef, useState } from 'react'
import type { Meta, StoryObj } from '@storybook/react'
import { SharedPrivateMemory, type SharedPrivateMemoryReader } from '../src/components/SharedPrivateMemory'
import type { SharedPrivateMemoryResult, SharedPrivateMemoryScope } from '../src/api/teamService'

const a:SharedPrivateMemoryScope={room_id:88,publication_id:'11111111-1111-4111-8111-111111111111',team_id:'22222222-2222-4222-8222-222222222222',account_id:'33333333-3333-4333-8333-333333333333',issuer:'https://example.invalid',session_generation:'44444444-4444-4444-8444-444444444444'}
const b:SharedPrivateMemoryScope={...a,account_id:'55555555-5555-4555-8555-555555555555',session_generation:'66666666-6666-4666-8666-666666666666'}
const result=(scope:SharedPrivateMemoryScope,empty=false):SharedPrivateMemoryResult=>({...scope,version:1,scope_revision:1,truncated:false,entries:empty?[]:[{memory_id:'77777777-7777-4777-8777-777777777777',category:'preference',content:scope.account_id===a.account_id?'합성 예시: 간결한 답변 선호':'합성 예시: 자세한 답변 선호',revision:1}]})
type Scenario='ready'|'empty'|'error'|'scope-change'
function Example({scenario}:{scenario:Scenario}) {
  const [scope,setScope]=useState(a)
  const resolveOld=useRef<((value:SharedPrivateMemoryResult)=>void)|null>(null)
  const reader=useRef<SharedPrivateMemoryReader>(async current=>{
    if(scenario==='error')throw new Error('synthetic unavailable')
    if(scenario==='scope-change'&&current.account_id===a.account_id)return new Promise(resolve=>{resolveOld.current=resolve})
    return result(current,scenario==='empty')
  }).current
  return <div className="page-frame page-content stack">{scenario==='scope-change'&&<div className="page-actions"><button type="button" className="button button--secondary" onClick={()=>setScope(b)}>합성 계정 변경</button><button type="button" className="button button--secondary" onClick={()=>resolveOld.current?.(result(a))}>이전 합성 응답 완료</button></div>}<SharedPrivateMemory scope={scope} read={reader} ko startOpen/></div>
}
const meta:Meta<typeof Example>={title:'Shared Bot/Private memory',component:Example}
export default meta
type Story=StoryObj<typeof Example>
export const Ready:Story={args:{scenario:'ready'}}
export const Empty:Story={args:{scenario:'empty'}}
export const Unavailable:Story={args:{scenario:'error'}}
export const AccountChangeLateResponse:Story={args:{scenario:'scope-change'}}
