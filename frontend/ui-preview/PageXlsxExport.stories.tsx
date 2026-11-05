import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useMemo, useState } from 'react'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { pageDigest, type PageXlsxAdapter, type PageXlsxPreview } from '../src/api/pageXlsxExport'
import PageXlsxExport, { type PageXlsxBridge } from '../src/components/PageXlsxExport'
const resultId='11111111-1111-4111-8111-111111111111'
const content=JSON.stringify({format:'gamja.page.v1',title:'합성 보고서',datasets:[{id:'sales',columns:[{key:'item',label:'항목',type:'text'},{key:'total',label:'합계',type:'number'},{key:'done',label:'완료',type:'boolean'}],rows:[{item:'합성 A',total:42,done:true},{item:'합성 B',total:8,done:false}]}],views:[{id:'sales_view',dataset:'sales',filters:[]}],blocks:[{type:'heading',level:1,text:'합성 데이터'},{type:'table',view:'sales_view',columns:['item','total'],title:'표'},{type:'chart',view:'sales_view',kind:'bar',category:'item',aggregate:'sum',value:'total',title:'합성 차트'}]})
const fakeBytes=new Uint8Array([0x50,0x4b,3,4,...Array(28).fill(0)])
const hash=async(b:BufferSource)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',b))).map(x=>x.toString(16).padStart(2,'0')).join('')
type Scenario='ready'|'cancelled'|'unknown'|'empty'|'changed'|'marker-replaced'|'unsupported'
function Surface({scenario}:{scenario:Scenario}) {
 const [epoch,setEpoch]=useState(0),[ready,setReady]=useState(false),[version,setVersion]=useState(1),[notice,setNotice]=useState('')
 useLayoutEffect(()=>{useAuthStore.getState().setAuth('synthetic-token',{id:1,email:'preview@example.invalid',username:'Preview'});setEpoch(useAuthStore.getState().epoch);setReady(true)},[])
 const store=useMemo(()=>{const data=new Map<string,string>();return {getItem:(k:string)=>data.get(k)??null,setItem:(k:string,v:string)=>{data.set(k,v)},removeItem:(k:string)=>{data.delete(k)}}},[])
 const bridge=useMemo<PageXlsxBridge>(()=>{let last:PageXlsxPreview|null=null;const adapter:PageXlsxAdapter={preview:async(room,result,pageVersion)=>{if(scenario==='unsupported')throw {response:{status:422,data:{detail:{code:'page_xlsx_chart_unsupported'}}}};if(scenario==='empty')throw {response:{status:422,data:{detail:{code:'page_xlsx_no_datasets'}}}};const p:PageXlsxPreview={version:1,owner_id:1,owner_epoch:'a'.repeat(64),room_id:room,result_id:result,page_version:pageVersion,document_sha256:scenario==='changed'?'b'.repeat(64):await pageDigest(content),sha256:await hash(fakeBytes),size_bytes:fakeBytes.byteLength,mime:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',sheets:[{dataset_id:'sales',name:'sales',rows:2,columns:3}],stringified_numbers:0,charts:[{title:'합성 차트',kind:'bar',aggregate:'sum',source_sheet:'sales',snapshot_sheet:'chart_1'}]};last=p;return p},generate:async(_room,_result,_version,p)=>{if(!last||last.sha256!==p.sha256)throw Error('changed');return fakeBytes.slice().buffer}};return {available:true,adapter,store,save:async()=>scenario==='cancelled'?{phase:'cancelled'}:(scenario==='unknown'||scenario==='marker-replaced')?{phase:'unknown'}:{phase:'saved',filename:'synthetic.xlsx',receipt:{path:'/synthetic/synthetic.xlsx',bytes:fakeBytes.byteLength,replaced:false,durability_unconfirmed:false}}}},[scenario,store])
 return <I18nProvider><main className="page-frame page-content stack"><p>합성 Page와 가상 bytes·저장 adapter입니다. 실제 XLSX 생성·OS 대화상자·파일 저장은 없습니다.</p><button className="button button--secondary" type="button" onClick={()=>setVersion(v=>v+1)}>합성 버전 전환</button><button className="button button--secondary" type="button" onClick={()=>{useAuthStore.getState().setAuth('synthetic-relogin',{id:1,email:'preview@example.invalid',username:'Preview'});setEpoch(useAuthStore.getState().epoch)}}>합성 재로그인</button>{scenario==='marker-replaced'&&<><button className="button button--secondary" type="button" onClick={()=>{const key=`gamja.page-xlsx-save.v1.1.501.${resultId}.${version}`;const raw=store.getItem(key);if(!raw){setNotice('먼저 합성 저장을 시작하세요.');return}const value=JSON.parse(raw);value.marker_id='22222222-2222-4222-8222-222222222222';store.setItem(key,JSON.stringify(value));setNotice('다른 합성 저장 표시로 변경됨')}}>다른 합성 저장 표시로 변경</button><p role="status">{notice}</p></>}{ready&&<PageXlsxExport key={`${scenario}:${version}:${epoch}`} ownerId={1} epoch={epoch} roomId={501} resultId={resultId} version={version} title="합성 보고서" content={content} canUse={()=>true} bridge={bridge}/>}</main></I18nProvider>
}
const meta={title:'Product/Page dataset XLSX export',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const Ready:Story={args:{scenario:'ready'}}
export const Cancelled:Story={args:{scenario:'cancelled'}}
export const Unknown:Story={args:{scenario:'unknown'}}
export const Empty:Story={args:{scenario:'empty'}}
export const Changed:Story={args:{scenario:'changed'}}
export const MarkerReplaced:Story={args:{scenario:'marker-replaced'}}

export const UnsupportedChart:Story={args:{scenario:'unsupported'}}
