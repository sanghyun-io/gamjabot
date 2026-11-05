import type { Meta, StoryObj } from '@storybook/react-vite'
import { useEffect, useState } from 'react'
import { within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createPreviewScenario } from './fixtures'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { libraryUploadsApi } from '../src/api/libraryUploads'
import { libraryFileQuestionsApi } from '../src/api/libraryFileQuestions'
import { imageInputVerificationApi, type ImageCheckReceipt } from '../src/api/imageInputVerification'
import { eventsApi, type RoomEvent } from '../src/api/events'
import FileQuestionComposer from '../src/components/FileQuestionComposer'
const id='11111111-1111-4111-8111-111111111111',sha='ab'.repeat(32),textHash='2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
const item={upload_id:id,bot_id:8,filename:'report.txt',size_bytes:5,sha256:sha,created_at:'2026-10-31T00:00:00Z',media_type:'application/octet-stream' as const,origin:'owner_upload' as const,removed:false}
function setup(names:Record<number,string>,unknown=false){let receipt:RoomEvent|null=null;let receiptKey:string|null=null;libraryUploadsApi.list=async(bot)=>({bot_id:bot,items:bot===8?[item]:[],next_cursor:null});libraryFileQuestionsApi.preview=async(_room,input)=>({basis:'cd'.repeat(32),file:item,extraction:{text_sha256:textHash,format:'utf8',page_count:null,truncated:false,extractor:'utf8',chars:5},excerpt:'hello',target:{bot_id:8,bot_name:names[input.bot_id],connection_id:2,model_id:'synthetic-model',connection_name:'Synthetic',provider_id:'synthetic'},context_preview:{room_id:501,projected_seq:1,selected_agent_ids:[8],fingerprint:'fixture',agents:[{agent_id:8,agent_name:names[input.bot_id],model_id:'synthetic-model',projection_status:'ready',error_code:null,system_text:'',messages:[],source_coverage:{committed_high_water:0,included_event_seqs:[],summarized_ranges:[],draft_included:true},truncations:[],non_text:[],budget:{unit:'characters',limit_chars:10000,used_chars:100,remaining_chars:9900}}]}});libraryFileQuestionsApi.send=async(room,input,basis,key)=>{const hash=async(value:string)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)))).map(x=>x.toString(16).padStart(2,'0')).join('');const q=await hash(input.content),command=await hash(key);const event:RoomEvent={event_id:'22222222-2222-4222-8222-222222222222',device_id:null,room_id:room,seq:25,actor_type:'user',actor_id:1,actor_name_snapshot:'Owner',kind:'message',created_at:'2026-10-31T00:00:00Z',idempotency_key:`file-question:${command}`,payload:{content:input.content,file_context:{version:1,bot_id:8,upload_id:id,sha256:sha,basis,proof:{owner_id:1,room_id:room,bot_id:8,upload_id:id,sha256:sha,question_sha256:q,file:{filename:item.filename,size_bytes:item.size_bytes},target:{bot_id:8,bot_name:names[input.bot_id],connection_id:2,model_id:'synthetic-model',connection_name:'Synthetic',provider_id:'synthetic'},extraction:{text_sha256:textHash,format:'utf8',page_count:null,truncated:false,extractor:'utf8',chars:5}}}}};receipt=event;receiptKey=key;if(unknown)throw Error('Synthetic lost response');return event};libraryFileQuestionsApi.receipt=async(_room,key)=>{if(receipt&&key===receiptKey)return receipt;throw Error('Synthetic unavailable')}}
function Surface(){const [scope]=useState(()=>{useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'});setup({8:'보고 Bot'});return useAuthStore.getState().epoch});return <I18nProvider><main className="page-frame page-frame--medium page-content"><FileQuestionComposer roomId={501} ownerId={1} authEpoch={scope} bots={[{id:8,name:'보고 Bot'}]} onEvent={()=>{}}/></main></I18nProvider>}
function ChatSurface({unknown=false}:{unknown?:boolean}){const [scenario]=useState(()=>{const result=createPreviewScenario({screen:'chat',state:'ready'});setup({8:'기획',9:'검토',10:'테스트',11:'대기',12:'조사'},unknown);return result});useEffect(()=>()=>scenario.dispose(),[scenario]);return scenario.element}
const meta={title:'Screens/보관 파일 질문',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const Ready:Story={name:'파일 선택과 미리보기'}

export const Chat:Story={name:'대화 · 보관 파일 질문',render:()=> <ChatSurface/>}

export const ChatUnknown:Story={name:'대화 · 파일 질문 전송 결과 확인',render:()=> <ChatSurface unknown/>}

// A tiny synthetic PNG fixture; no personal file or external service is used.
type ImageCase = 'before-consent' | 'ready' | 'unsupported' | 'changed' | 'unknown' | 'recovered' | 'verification' | 'verification-unknown' | 'verification-done' | 'verification-terminal-unknown' | 'verification-pending' | 'verification-stale'
const syntheticPng='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aL8AAAAASUVORK5CYII='
const pngSha='0bdff1d9ca1b18f2d30cf9ded35ea9f7158112971a811cbf2ba0dbd92b558cdb'
const pngItem={...item,filename:'synthetic.png',size_bytes:68,sha256:pngSha}
const image={mime:'image/png' as const,width:1,height:1,size_bytes:68}
function ImageSurface({state}:{state:ImageCase}) {
 const [accepted,setAccepted]=useState(false)
 const [scope]=useState(()=>{
  useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'})
  const epoch=useAuthStore.getState().epoch;sessionStorage.removeItem(`file-question:1:${epoch}:501`)
  let receipt:RoomEvent|null=null,receiptKey=''
  libraryUploadsApi.list=async(bot)=>({bot_id:bot,items:bot===8?[pngItem]:[],next_cursor:null})
  libraryUploadsApi.fileBytes=async()=>Uint8Array.from(atob(syntheticPng),c=>c.charCodeAt(0)).buffer
  libraryFileQuestionsApi.preview=async(_room,input)=>{
   if(['unsupported','verification','verification-unknown','verification-done','verification-terminal-unknown','verification-pending','verification-stale'].includes(state))throw {response:{status:409,data:{detail:{code:'file_question_image_unverified',bot_id:8,connection_id:2,model_id:'synthetic-vision',config_revision:1,auth_revision:0,file:pngItem,image,target:{bot_id:8,bot_name:'Vision Bot',connection_id:2,model_id:'synthetic-vision',connection_name:'Synthetic',provider_id:'synthetic'}}}}}
   if(input.mode!=='image')throw Error('Synthetic image mode required')
   return {mode:'image',basis:'cd'.repeat(32),file:pngItem,image,requires_image_consent:true,target:{bot_id:8,bot_name:'Vision Bot',connection_id:2,model_id:'synthetic-vision',connection_name:'Synthetic',provider_id:'synthetic'},context_preview:{room_id:501,projected_seq:1,selected_agent_ids:[8],fingerprint:'fixture',agents:[{agent_id:8,agent_name:'Vision Bot',model_id:'synthetic-vision',projection_status:'ready',error_code:null,system_text:'',messages:[],source_coverage:{committed_high_water:0,included_event_seqs:[],summarized_ranges:[],draft_included:true},truncations:[],non_text:[],budget:{unit:'characters',limit_chars:10000,used_chars:100,remaining_chars:9900}}]}}
  }
  libraryFileQuestionsApi.send=async(room,input,basis,key,_signal,consent)=>{
   if(input.mode!=='image'||consent!==true)throw Error('Synthetic consent required')
   const hash=async(value:string)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)))).map(x=>x.toString(16).padStart(2,'0')).join('')
   const q=await hash(input.content),command=await hash(key)
   const target={bot_id:8,bot_name:'Vision Bot',connection_id:2,model_id:'synthetic-vision',connection_name:'Synthetic',provider_id:'synthetic'}
   receipt={event_id:'22222222-2222-4222-8222-222222222222',device_id:null,room_id:room,seq:25,actor_type:'user',actor_id:1,actor_name_snapshot:'Owner',kind:'message',created_at:'2026-10-31T00:00:00Z',idempotency_key:`file-question:${command}`,payload:{content:input.content,file_context:{version:2,bot_id:8,upload_id:id,sha256:pngSha,basis,image_consent:true,proof:{version:2,mode:'image',owner_id:1,room_id:room,bot_id:8,upload_id:id,sha256:pngSha,question_sha256:q,vision_binding:'ee'.repeat(32),image,image_consent_required:true,file:{filename:pngItem.filename,size_bytes:pngItem.size_bytes},target}}}}
   receiptKey=key;if(state==='unknown'||state==='recovered')throw Error('Synthetic lost response');return receipt
  }
  libraryFileQuestionsApi.receipt=async(_room,key)=>{if(receipt&&key===receiptKey)return receipt;throw Error('Synthetic unavailable')}
  const checkPreview={version:1 as const,model_id:'synthetic-vision',max_generation_requests:2,max_catalog_requests:0,max_metered_model_http_requests:2,config_revision:1,auth_grant_revision:0};let checkReceipt:ImageCheckReceipt|null=null;let checkKey=''
  sessionStorage.removeItem(`image-input-check:1:${epoch}:2:synthetic-vision`)
  if(state==='verification-stale')sessionStorage.setItem(`image-input-check:1:${epoch}:2:synthetic-vision`,JSON.stringify({key:'synthetic-old-check',preview:{...checkPreview,config_revision:2}}))
  imageInputVerificationApi.preview=async()=>checkPreview
  imageInputVerificationApi.check=async(_connection,_preview,key)=>{checkKey=key;checkReceipt=state==='verification-terminal-unknown'?{...checkPreview,status:'unknown',phase:'first_image',error_code:'provider_timeout',actual_generation_requests:null,actual_catalog_requests:null,actual_metered_model_http_requests:null}:state==='verification-pending'?{...checkPreview,status:'pending',actual_generation_requests:null,actual_catalog_requests:null,actual_metered_model_http_requests:null}:{...checkPreview,status:'verified',actual_generation_requests:2,actual_catalog_requests:0,actual_metered_model_http_requests:2};if(state==='verification-unknown'||state==='verification-terminal-unknown')throw Error('Synthetic response lost');return checkReceipt}
  imageInputVerificationApi.command=async(_connection,key)=>{if(!checkReceipt||key!==checkKey)throw Error('Synthetic missing receipt');return checkReceipt}
  return epoch
 })
 return <I18nProvider systemLanguages={()=>['en']}><main className="page-frame page-frame--medium page-content"><FileQuestionComposer roomId={501} ownerId={1} authEpoch={scope} bots={[{id:8,name:'Vision Bot'}]} onEvent={()=>setAccepted(true)}/>{accepted&&<p role="status">Synthetic receipt confirmed</p>}</main></I18nProvider>
}

const imagePreview = async ({canvasElement}:{canvasElement:HTMLElement}) => {
 const canvas=within(canvasElement)
 await userEvent.selectOptions(await canvas.findByLabelText('How to use the file'),'image')
 await userEvent.selectOptions(canvas.getByLabelText('Bot'),'8')
 await canvas.findByRole('option',{name:/synthetic.png/});await userEvent.selectOptions(canvas.getByLabelText('Saved file'),id)
 await userEvent.type(canvas.getByLabelText('Question'),'Describe this image.')
 await userEvent.click(canvas.getByRole('button',{name:'Review file and target'}))
}
export const ImageBeforeConsent:Story={globals:{locale:'en'},name:'PNG · preview and unchecked consent',render:()=> <ImageSurface state="before-consent"/>,play:imagePreview}
export const ImageReady:Story={globals:{locale:'en'},name:'PNG · consent selected',render:()=> <ImageSurface state="ready"/>,play:async c=>{await imagePreview(c);const canvas=within(c.canvasElement);await userEvent.click(await canvas.findByRole('checkbox'))}}
export const ImageUnsupported:Story={globals:{locale:'en'},name:'PNG · vision not verified',render:()=> <ImageSurface state="unsupported"/>,play:imagePreview}
export const ImageChanged:Story={globals:{locale:'en'},name:'PNG · question changed after review',render:()=> <ImageSurface state="changed"/>,play:async c=>{await imagePreview(c);const canvas=within(c.canvasElement);await userEvent.click(await canvas.findByRole('checkbox'));await userEvent.type(canvas.getByLabelText('Question'),' Changed')}}
export const ImageUnknown:Story={globals:{locale:'en'},name:'PNG · response unknown',render:()=> <ImageSurface state="unknown"/>,play:async c=>{await imagePreview(c);const canvas=within(c.canvasElement);await userEvent.click(await canvas.findByRole('checkbox'));await userEvent.click(canvas.getByRole('button',{name:'Send question and image'}))}}
export const ImageRecovered:Story={globals:{locale:'en'},name:'PNG · receipt lookup recovered',render:()=> <ImageSurface state="recovered"/>,play:async c=>{await imagePreview(c);const canvas=within(c.canvasElement);await userEvent.click(await canvas.findByRole('checkbox'));await userEvent.click(canvas.getByRole('button',{name:'Send question and image'}));await userEvent.click(await canvas.findByRole('button',{name:'Check existing request'}));await waitFor(()=>{if(!canvas.queryByText('Synthetic receipt confirmed'))throw Error('Synthetic receipt pending')})}}

export const ImageVerification:Story={globals:{locale:'en'},name:'PNG · review synthetic vision check',render:()=> <ImageSurface state="verification"/>,play:async c=>{await imagePreview(c);const canvas=within(c.canvasElement);await userEvent.click(await canvas.findByRole('button',{name:'Review image capability check'}))}}
export const ImageVerificationUnknown:Story={globals:{locale:'en'},name:'PNG · vision check unknown',render:()=> <ImageSurface state="verification-unknown"/>,play:async c=>{await imagePreview(c);const canvas=within(c.canvasElement);await userEvent.click(await canvas.findByRole('button',{name:'Review image capability check'}));await userEvent.click(await canvas.findByRole('button',{name:'Run image capability check'}))}}
export const ImageVerificationDone:Story={globals:{locale:'en'},name:'PNG · synthetic vision check verified',render:()=> <ImageSurface state="verification-done"/>,play:async c=>{await imagePreview(c);const canvas=within(c.canvasElement);await userEvent.click(await canvas.findByRole('button',{name:'Review image capability check'}));await userEvent.click(await canvas.findByRole('button',{name:'Run image capability check'}))}}

export const ImageCheckTerminalUnknown:Story={globals:{locale:'en'},name:'PNG · terminal unknown, review a new check',render:()=> <ImageSurface state="verification-terminal-unknown"/>,play:async c=>{await imagePreview(c);const canvas=within(c.canvasElement);await userEvent.click(await canvas.findByRole('button',{name:'Review image capability check'}));await userEvent.click(await canvas.findByRole('button',{name:'Run image capability check'}));await userEvent.click(await canvas.findByRole('button',{name:'Check result'}))}}
export const ImageCheckPending:Story={globals:{locale:'en'},name:'PNG · pending check cannot be repeated',render:()=> <ImageSurface state="verification-pending"/>,play:async c=>{await imagePreview(c);const canvas=within(c.canvasElement);await userEvent.click(await canvas.findByRole('button',{name:'Review image capability check'}));await userEvent.click(await canvas.findByRole('button',{name:'Run image capability check'}));await userEvent.click(await canvas.findByRole('button',{name:'Check result'}))}}
export const ImageCheckStaleStored:Story={globals:{locale:'en'},name:'PNG · saved check revision changed',render:()=> <ImageSurface state="verification-stale"/>,play:imagePreview}

// Synthetic previews only: no Office file, model, or external service is used.
type OfficeFormat='docx'|'xlsx'|'pptx'
type OfficeCase=OfficeFormat|'encrypted'|'invalid'|'resource_limit'|'no_text'
function OfficeSurface({state}:{state:OfficeCase}) {
 const [scope]=useState(()=>{
  useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'})
  const epoch=useAuthStore.getState().epoch;sessionStorage.removeItem(`file-question:1:${epoch}:501`)
  setup({8:'Document Bot'})
  const format:OfficeFormat=state==='docx'||state==='xlsx'||state==='pptx'?state:'docx'
  const file={...item,filename:`synthetic.${format}`,size_bytes:1024}
  const excerpts:Record<OfficeFormat,string>={docx:'Project overview\nQuarterly plan\nItem\tTotal\nSample\t42\n',xlsx:'Forecast\nA1: Item\tC1: Total\nA2: Sample\tC2: 42\n',pptx:'Slide 1\nOverview\nSlide 2\nForecast\nSlide 3\nNext steps\n'}
  libraryUploadsApi.list=async bot=>({bot_id:bot,items:bot===8?[file]:[],next_cursor:null})
  const basePreview=libraryFileQuestionsApi.preview
  libraryFileQuestionsApi.preview=async(room,input,signal)=>{
   if(!['docx','xlsx','pptx'].includes(state))throw {response:{status:422,data:{detail:{code:`file_question_office_${state}`}}}}
   const p=await basePreview(room,input,signal)
   if(p.mode==='image')throw Error('Synthetic text preview required')
   const excerpt=excerpts[format],text_sha256=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(excerpt)))).map(x=>x.toString(16).padStart(2,'0')).join('')
   return {...p,file,excerpt,extraction:{...p.extraction,text_sha256,chars:Array.from(excerpt).length,format,extractor:'ooxml-text-v1/stdlib',page_count:format==='pptx'?3:null}}
  }
  libraryFileQuestionsApi.send=async()=>{throw Error('Office synthetic preview only')}
  return epoch
 })
 return <I18nProvider systemLanguages={()=>['en']}><main className="page-frame page-frame--medium page-content"><FileQuestionComposer roomId={501} ownerId={1} authEpoch={scope} bots={[{id:8,name:'Document Bot'}]} onEvent={()=>{}}/></main></I18nProvider>
}
const officePreview=async ({canvasElement}:{canvasElement:HTMLElement})=>{
 const canvas=within(canvasElement)
 await userEvent.selectOptions(canvas.getByLabelText('Bot'),'8')
 await canvas.findByRole('option',{name:/synthetic\.(docx|xlsx|pptx)/});await userEvent.selectOptions(canvas.getByLabelText('Saved file'),id)
 await userEvent.type(canvas.getByLabelText('Question'),'Summarize this document.')
 await userEvent.click(canvas.getByRole('button',{name:'Review file and target'}))
}
export const OfficeDocx:Story={globals:{locale:'en'},name:'DOCX · text preview',render:()=> <OfficeSurface state="docx"/>,play:officePreview}
export const OfficeXlsx:Story={globals:{locale:'en'},name:'XLSX · text preview',render:()=> <OfficeSurface state="xlsx"/>,play:officePreview}
export const OfficePptx:Story={globals:{locale:'en'},name:'PPTX · slide text preview',render:()=> <OfficeSurface state="pptx"/>,play:officePreview}
export const OfficeEncrypted:Story={globals:{locale:'en'},name:'Office · encrypted',render:()=> <OfficeSurface state="encrypted"/>,play:officePreview}
export const OfficeInvalid:Story={globals:{locale:'en'},name:'Office · invalid file',render:()=> <OfficeSurface state="invalid"/>,play:officePreview}
export const OfficeResourceLimit:Story={globals:{locale:'en'},name:'Office · too large or complex',render:()=> <OfficeSurface state="resource_limit"/>,play:officePreview}
export const OfficeNoText:Story={globals:{locale:'en'},name:'Office · no readable text',render:()=> <OfficeSurface state="no_text"/>,play:officePreview}

// Synthetic long-text preview; no document, provider or network is used.
function FullAccessSurface({unknown=false}:{unknown?:boolean}){
 const [scope]=useState(()=>{
  useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'})
  const epoch=useAuthStore.getState().epoch;sessionStorage.removeItem(`file-question:1:${epoch}:501`);setup({8:'Synthetic Bot'})
  const original=libraryFileQuestionsApi.preview;let receipt:RoomEvent|null=null,receiptKey=''
  libraryFileQuestionsApi.preview=async(room,input,signal)=>{
   const p=await original(room,input,signal);if(p.mode==='image'||!input.full_access)return p
   return {...p,full_access:true,excerpt_sha256:textHash,excerpt_chars:5,range:{max_chars:200000,pdf_max_pages:50,ocr_max_pages:8},extraction:{...p.extraction,text_sha256:'ef'.repeat(32),chars:40001,truncated:false}}
  }
  libraryFileQuestionsApi.send=async(room,input,basis,key,_signal,_imageConsent,consent)=>{
   if(!input.full_access||!consent)throw Error('Synthetic consent required')
   const p=await libraryFileQuestionsApi.preview(room,input);if(p.mode==='image'||!p.range)throw Error('Synthetic text preview required')
   const hash=async(value:string)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)))).map(x=>x.toString(16).padStart(2,'0')).join('')
   const q=await hash(input.content),keyHash=await hash(key)
   receipt={event_id:'22222222-2222-4222-8222-222222222222',device_id:null,room_id:room,seq:25,actor_type:'user',actor_id:1,actor_name_snapshot:'Owner',kind:'message',created_at:'2026-11-03T00:00:00Z',idempotency_key:`file-question:${keyHash}`,payload:{content:input.content,file_context:{version:4,bot_id:8,upload_id:id,sha256:sha,basis,full_access_consent:true,proof:{version:4,mode:'text',full_access:true,owner_id:1,room_id:room,bot_id:8,upload_id:id,sha256:sha,question_sha256:q,file:{filename:item.filename,size_bytes:item.size_bytes},target:p.target,extraction:p.extraction,range:p.range}}}}
   receiptKey=key;if(unknown)throw Error('Synthetic lost response');return receipt
  }
  libraryFileQuestionsApi.receipt=async(_room,key)=>{if(receipt&&key===receiptKey)return receipt;throw Error('Synthetic unavailable')}
  return epoch
 })
 return <I18nProvider systemLanguages={()=>['en']}><main className="page-frame page-content"><p>Synthetic file, model and responses; no external data is used.</p><FileQuestionComposer roomId={501} ownerId={1} authEpoch={scope} bots={[{id:8,name:'Synthetic Bot'}]} onEvent={()=>{}}/></main></I18nProvider>
}
const fullAccessPreview=async({canvasElement}:{canvasElement:HTMLElement})=>{const c=within(canvasElement);await userEvent.selectOptions(c.getByLabelText('Bot'),'8');await c.findByRole('option',{name:/report.txt/});await userEvent.selectOptions(c.getByLabelText('Saved file'),id);await userEvent.type(c.getByLabelText('Question'),'Find the conclusion.');await userEvent.click(c.getByLabelText('Allow reading text beyond the excerpt'));await userEvent.click(c.getByRole('button',{name:'Review file and target'}));await c.findByLabelText(/I consent to sending the selected file/)}
export const FullAccessBeforeConsent:Story={globals:{locale:'en'},name:'Long text · review before consent',render:()=> <FullAccessSurface/>,play:fullAccessPreview}
export const FullAccessConsent:Story={globals:{locale:'en'},name:'Long text · explicit consent',render:()=> <FullAccessSurface/>,play:async c=>{await fullAccessPreview(c);await userEvent.click(within(c.canvasElement).getByLabelText(/I consent to sending the selected file/))}}
export const FullAccessUnknown:Story={globals:{locale:'en'},name:'Long text · response unknown',render:()=> <FullAccessSurface unknown/>,play:async c=>{await fullAccessPreview(c);const v=within(c.canvasElement);await userEvent.click(v.getByLabelText(/I consent to sending the selected file/));await userEvent.click(v.getByRole('button',{name:'Send question and file text'}))}}

function CoverageChat({complete=false}:{complete?:boolean}){
 const [scenario]=useState(()=>{
  const result=createPreviewScenario({screen:'chat',state:'teammate-changed-failure'}),previous=eventsApi.list
  const root:RoomEvent={event_id:'44444444-4444-4444-8444-444444444444',device_id:null,room_id:501,seq:1,actor_type:'user',actor_id:1,actor_name_snapshot:'Owner',kind:'message',created_at:'2026-11-03T00:00:00Z',idempotency_key:null,payload:{content:'Review the synthetic document.',file_context:{version:4,bot_id:8,upload_id:id,sha256:sha,basis:'cd'.repeat(32),full_access_consent:true,proof:{owner_id:1,room_id:501,bot_id:8,extraction:{chars:5000,truncated:false}}}}}
  const answer:RoomEvent={event_id:'55555555-5555-4555-8555-555555555555',device_id:null,room_id:501,seq:2,actor_type:'agent',actor_id:8,actor_name_snapshot:'기획',kind:'message',created_at:'2026-11-03T00:00:01Z',idempotency_key:null,payload:{content:'Synthetic answer.',trigger_event_id:root.event_id,file_read_coverage:{version:1,root_event_id:root.event_id,total_chars:5000,extraction_truncated:false,read_ranges:complete?[[0,5000]]:[[0,1600],[3000,4000]],search_used:true}}}
  eventsApi.list=async(room,after,limit)=>room===501?[root,answer].filter(e=>e.seq>after).slice(0,limit):previous(room,after,limit)
  return {element:result.element,dispose:()=>{eventsApi.list=previous;result.dispose()}}
 });useEffect(()=>()=>scenario.dispose(),[scenario]);return scenario.element
}
export const FullCoveragePartial:Story={name:'대화 · 긴 파일 일부만 전달',render:()=> <CoverageChat/>}
export const FullCoverageComplete:Story={name:'대화 · 추출 범위 전체 전달',render:()=> <CoverageChat complete/>}
