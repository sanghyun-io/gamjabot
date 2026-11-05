import api from '../lib/api'
import { parsePageDocument, type PageBlock } from '../lib/pageDocument'
const MAX_BYTES=5*1024*1024,MAX_RESPONSE=7*1024*1024
const mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
const hex=(v:unknown):v is string=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v)
export type PageXlsxSheet={dataset_id:string;name:string;rows:number;columns:number}
export type PageXlsxChart={title:string;kind:'bar'|'line'|'pie';aggregate:'count'|'sum'|'avg';source_sheet:string;snapshot_sheet:string}
export type PageXlsxPreview={version:1;owner_id:number;owner_epoch:string;room_id:number;result_id:string;page_version:number;document_sha256:string;sha256:string;size_bytes:number;mime:string;sheets:PageXlsxSheet[];stringified_numbers:number;charts:PageXlsxChart[]}
export type PageXlsxAdapter={preview:(room:number,result:string,version:number,signal:AbortSignal)=>Promise<PageXlsxPreview>;generate:(room:number,result:string,version:number,p:PageXlsxPreview,signal:AbortSignal)=>Promise<ArrayBuffer>}
export async function pageDigest(content:string):Promise<string>{const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(content));return Array.from(new Uint8Array(bytes),b=>b.toString(16).padStart(2,'0')).join('')}
function pageCharts(blocks:PageBlock[]):Extract<PageBlock,{type:'chart'}>[] {return blocks.flatMap(b=>b.type==='chart'?[b]:b.type==='columns'?pageCharts(b.items):b.type==='tabs'?b.tabs.flatMap(t=>pageCharts(t.blocks)):[])}
export function validPageXlsxPreview(p:PageXlsxPreview,owner:number,room:number,result:string,version:number,content:string,documentSha:string):boolean {
 const doc=parsePageDocument(content);if(!doc||!doc.datasets.length||!p||p.version!==1||p.owner_id!==owner||!hex(p.owner_epoch)||p.room_id!==room||p.result_id!==result||p.page_version!==version||p.document_sha256!==documentSha||!hex(p.sha256)||!Number.isSafeInteger(p.size_bytes)||p.size_bytes<22||p.size_bytes>MAX_BYTES||p.mime!==mime||!Array.isArray(p.sheets)||p.sheets.length!==doc.datasets.length||!Number.isSafeInteger(p.stringified_numbers)||p.stringified_numbers<0||p.stringified_numbers>80000)return false
 const names=new Set<string>()
 if(!p.sheets.every((s,i)=>{const d=doc.datasets[i];if(!s||s.dataset_id!==d.id||typeof s.name!=='string'||s.name.length<1||s.name.length>31||/[\[\]:*?/\\\x00-\x1f]/.test(s.name)||names.has(s.name.toLocaleLowerCase())||s.rows!==d.rows.length||s.columns!==d.columns.length)return false;names.add(s.name.toLocaleLowerCase());return true}))return false
 const charts=pageCharts(doc.blocks);if(!Array.isArray(p.charts)||p.charts.length!==charts.length||charts.length>32)return false
 return p.charts.every((c,i)=>{const chart=charts[i],view=doc.views.find(v=>v.id===chart.view),source=p.sheets.find(s=>s.dataset_id===view?.dataset);if(!c||c.title!==chart.title||c.kind!==chart.kind||c.aggregate!==chart.aggregate||c.source_sheet!==source?.name||typeof c.snapshot_sheet!=='string'||c.snapshot_sheet.length<1||c.snapshot_sheet.length>31||/[\[\]:*?/\\\x00-\x1f]/.test(c.snapshot_sheet)||names.has(c.snapshot_sheet.toLocaleLowerCase()))return false;names.add(c.snapshot_sheet.toLocaleLowerCase());return true})
}
const path=(room:number,result:string,version:number)=>`/conversations/${room}/results/${result}/versions/${version}/xlsx`
export const pageXlsxAdapter:PageXlsxAdapter={
 preview:async(room,result,version,signal)=>(await api.get<PageXlsxPreview>(`${path(room,result,version)}/preview`,{signal})).data,
 generate:async(room,result,version,p,signal)=>{
  const limited=new AbortController();let exceeded=false;const abort=()=>limited.abort();signal.addEventListener('abort',abort,{once:true});if(signal.aborted)limited.abort()
  try {const {data}=await api.post<PageXlsxPreview & {content_base64:string}>(path(room,result,version),{expected_owner_epoch:p.owner_epoch,expected_document_sha256:p.document_sha256,expected_sha256:p.sha256,consent:true},{signal:limited.signal,maxContentLength:MAX_RESPONSE,onDownloadProgress:e=>{if(e.loaded>MAX_RESPONSE||e.total!==undefined&&e.total>MAX_RESPONSE){exceeded=true;limited.abort()}}})
   const {content_base64,...metadata}=data||{};const keys=(['version','owner_id','owner_epoch','room_id','result_id','page_version','document_sha256','sha256','size_bytes','mime','stringified_numbers'] as const);if(!metadata||keys.some(k=>metadata[k]!==p[k])||!Array.isArray(metadata.sheets)||metadata.sheets.length!==p.sheets.length||metadata.sheets.some((sheet,i)=>!sheet||sheet.dataset_id!==p.sheets[i].dataset_id||sheet.name!==p.sheets[i].name||sheet.rows!==p.sheets[i].rows||sheet.columns!==p.sheets[i].columns)||!Array.isArray(metadata.charts)||metadata.charts.length!==p.charts.length||metadata.charts.some((c,i)=>!c||c.title!==p.charts[i].title||c.kind!==p.charts[i].kind||c.aggregate!==p.charts[i].aggregate||c.source_sheet!==p.charts[i].source_sheet||c.snapshot_sheet!==p.charts[i].snapshot_sheet)||typeof content_base64!=='string'||content_base64.length!==4*Math.ceil(p.size_bytes/3)||content_base64.length>4*Math.ceil(MAX_BYTES/3)||!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(content_base64))throw Error('changed')
   const raw=atob(content_base64);if(raw.length!==p.size_bytes||btoa(raw)!==content_base64)throw Error('changed');const out=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)out[i]=raw.charCodeAt(i)
   if(out[0]!==0x50||out[1]!==0x4b||out[2]!==0x03||out[3]!==0x04||await pageDigestBytes(out)!==p.sha256)throw Error('changed')
   return out.buffer as ArrayBuffer
  }catch(e){if(exceeded)throw Error('too_large');throw e}finally{signal.removeEventListener('abort',abort)}
 },
}
async function pageDigestBytes(bytes:Uint8Array):Promise<string>{const value=await crypto.subtle.digest('SHA-256',bytes as BufferSource);return Array.from(new Uint8Array(value),b=>b.toString(16).padStart(2,'0')).join('')}
