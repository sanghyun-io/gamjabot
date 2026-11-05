import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import PageMarkdownExport, { type PageMarkdownBridge } from '../src/components/PageMarkdownExport'

const sample=JSON.stringify({format:'gamja.page.v1',title:'합성 주간 보고',datasets:[{id:'work',columns:[{key:'person',label:'담당자',type:'text'},{key:'amount',label:'건수',type:'number'},{key:'done',label:'완료',type:'boolean'}],rows:[{person:'가람',amount:4,done:true},{person:'나래',amount:2,done:false}]}],views:[{id:'all_work',dataset:'work',filters:['search_name']}],blocks:[{type:'heading',level:1,text:'요약'},{type:'markdown',text:'합성 자료입니다.'},{type:'control',id:'search_name',view:'all_work',field:'person',mode:'search',label:'담당자 검색'},{type:'metric',view:'all_work',aggregate:'sum',label:'합계',field:'amount'},{type:'table',view:'all_work',columns:['person','amount'],title:'담당자별'},{type:'chart',view:'all_work',kind:'bar',category:'person',aggregate:'sum',value:'amount',title:'건수 차트'},{type:'tabs',tabs:[{label:'첫 탭',blocks:[{type:'markdown',text:'첫 탭 내용'}]},{label:'둘째 탭',blocks:[{type:'markdown',text:'둘째 탭 내용'}]}]}]})
type Mode='ready'|'cancelled'|'unknown'|'invalid'
function Surface({mode}:{mode:Mode}) {
 const [ready,setReady]=useState(false),[epoch,setEpoch]=useState(0),[captured,setCaptured]=useState<string|null>(null),[version,setVersion]=useState(1)
 const [client]=useState(()=>new QueryClient({defaultOptions:{queries:{retry:false}}}))
 useLayoutEffect(()=>{useAuthStore.getState().setAuth('synthetic-token',{id:1,email:'preview@example.invalid',username:'Preview'});setEpoch(useAuthStore.getState().epoch);setReady(true)},[])
 const bridge:PageMarkdownBridge={available:true,transport:async()=> 'native',save:async text=>{setCaptured(text);return mode==='cancelled'?'cancelled':mode==='unknown'?'unknown':'saved'}}
 return <QueryClientProvider client={client}><I18nProvider><main className="page-frame page-content stack"><p>합성 Page와 가상 저장 adapter · 실제 파일·OS 대화상자 없음</p><button className="button button--secondary" type="button" onClick={()=>setVersion(v=>v+1)}>합성 버전 전환</button>{ready&&<PageMarkdownExport key={version} ownerId={1} epoch={epoch} roomId={501} resultId="11111111-1111-4111-8111-111111111111" version={version} title="합성 주간 보고" content={mode==='invalid'?'{invalid':sample} canUse={()=>true} bridge={bridge}/>} {captured&&<p role="status">합성 adapter가 {new TextEncoder().encode(captured).byteLength} bytes를 받았습니다. 실제 저장은 없습니다.</p>}</main></I18nProvider></QueryClientProvider>
}
const meta={title:'Product/Page readable Markdown export',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const Ready:Story={args:{mode:'ready'}}
export const Cancelled:Story={args:{mode:'cancelled'}}
export const InvalidStructure:Story={args:{mode:'invalid'}}
export const Unknown:Story={args:{mode:'unknown'}}
