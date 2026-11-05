import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useState } from 'react'
import { within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { libraryUploadsApi } from '../src/api/libraryUploads'
import { libraryFileQuestionsApi } from '../src/api/libraryFileQuestions'
import type { RoomEvent } from '../src/api/events'
import FileQuestionComposer from '../src/components/FileQuestionComposer'
const id='11111111-1111-4111-8111-111111111111',sha='ab'.repeat(32),basis='cd'.repeat(32)
const file={upload_id:id,bot_id:8,filename:'synthetic-scan.pdf',size_bytes:1024,sha256:sha,created_at:'2026-11-03T00:00:00Z',media_type:'application/octet-stream' as const,origin:'owner_upload' as const,removed:false}
const hash=async(s:string)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s)))).map(x=>x.toString(16).padStart(2,'0')).join('')
type Scenario='ready'|'truncated'|'no-text'|'mismatch'|'unknown'|'send-limits-unavailable'|'send-busy'|'send-interrupted'
function Surface({scenario}:{scenario:Scenario}){
 const [ready,setReady]=useState(false),[epoch,setEpoch]=useState(0),[accepted,setAccepted]=useState(false)
 useLayoutEffect(()=>{useAuthStore.getState().setAuth('synthetic-token',{id:1,email:'preview@example.invalid',username:'Preview'});const e=useAuthStore.getState().epoch,key=`file-question:1:${e}:501`;setEpoch(e);sessionStorage.removeItem(key)
  const oldList=libraryUploadsApi.list,oldPreview=libraryFileQuestionsApi.preview,oldSend=libraryFileQuestionsApi.send,oldReceipt=libraryFileQuestionsApi.receipt
  let receipt:RoomEvent|null=null,receiptKey=''
  libraryUploadsApi.list=async bot=>({bot_id:bot,items:bot===8?[file]:[],next_cursor:null})
  libraryFileQuestionsApi.preview=async(_room,input)=>{if(input.mode!=='pdf_ocr')throw Error('Synthetic OCR mode required');if(scenario==='no-text')throw {response:{status:422,data:{detail:{code:'file_question_ocr_no_text'}}}};const excerpt='Synthetic scan: invoice total 42.\n',text_sha256=await hash(excerpt);return {mode:scenario==='mismatch'?'text':'pdf_ocr',basis,file,target:{bot_id:8,bot_name:'Synthetic Bot',connection_id:2,model_id:'synthetic-model',connection_name:'Synthetic',provider_id:'synthetic'},extraction:{text_sha256,format:'pdf',page_count:scenario==='truncated'?10:2,truncated:scenario==='truncated',extractor:'macos-vision-ocr-v1',chars:Array.from(excerpt).length},excerpt,context_preview:{room_id:501,projected_seq:1,selected_agent_ids:[8],fingerprint:'fixture',agents:[{agent_id:8,agent_name:'Synthetic Bot',model_id:'synthetic-model',projection_status:'ready',error_code:null,system_text:'',messages:[],source_coverage:{committed_high_water:0,included_event_seqs:[],summarized_ranges:[],draft_included:true},truncations:[],non_text:[],budget:{unit:'characters',limit_chars:10000,used_chars:100,remaining_chars:9900}}]}}}
  libraryFileQuestionsApi.send=async(room,input,sendBasis,requestKey)=>{if(input.mode!=='pdf_ocr')throw Error('Synthetic OCR mode required');if(scenario==='send-limits-unavailable'||scenario==='send-busy'||scenario==='send-interrupted')throw {response:{status:422,data:{detail:{code:scenario==='send-busy'?'file_question_ocr_busy':scenario==='send-interrupted'?'file_question_ocr_interrupted':'file_question_ocr_limits_unavailable'}}}};const preview=await libraryFileQuestionsApi.preview(room,input),question=await hash(input.content),keyHash=await hash(requestKey);if(preview.mode==='image')throw Error('bad fixture');receipt={event_id:'22222222-2222-4222-8222-222222222222',device_id:null,room_id:room,seq:25,actor_type:'user',actor_id:1,actor_name_snapshot:'Owner',kind:'message',created_at:'2026-11-03T00:00:00Z',idempotency_key:`file-question:${keyHash}`,payload:{content:input.content,file_context:{version:3,bot_id:8,upload_id:id,sha256:sha,basis:sendBasis,proof:{version:3,mode:'pdf_ocr',owner_id:1,room_id:room,bot_id:8,upload_id:id,sha256:sha,question_sha256:question,file:{filename:file.filename,size_bytes:file.size_bytes},target:{bot_id:8,connection_id:2,model_id:'synthetic-model'},extraction:preview.extraction}}}};receiptKey=requestKey;if(scenario==='unknown')throw Error('synthetic lost response');return receipt}
  libraryFileQuestionsApi.receipt=async(_room,k)=>{if(!receipt||k!==receiptKey)throw Error('not found');return receipt}
  setReady(true);return()=>{libraryUploadsApi.list=oldList;libraryFileQuestionsApi.preview=oldPreview;libraryFileQuestionsApi.send=oldSend;libraryFileQuestionsApi.receipt=oldReceipt;sessionStorage.removeItem(key)}
 },[scenario])
 return <I18nProvider systemLanguages={()=>['en']}><main className="page-frame page-content"><p>Synthetic text and fake adapter; no PDF, OCR, model, or network is used.</p>{ready&&<FileQuestionComposer roomId={501} ownerId={1} authEpoch={epoch} bots={[{id:8,name:'Synthetic Bot'}]} onEvent={()=>setAccepted(true)}/>}{accepted&&<p role="status">Synthetic receipt confirmed.</p>}</main></I18nProvider>
}
const meta={title:'Product/Scanned PDF question',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
const review=async({canvasElement}:{canvasElement:HTMLElement})=>{const canvas=within(canvasElement);await userEvent.selectOptions(canvas.getByLabelText('Bot'),'8');await canvas.findByRole('option',{name:/synthetic-scan.pdf/});await userEvent.selectOptions(canvas.getByLabelText('How to use the file'),'pdf_ocr');await userEvent.selectOptions(canvas.getByLabelText('Saved file'),id);await userEvent.type(canvas.getByLabelText('Question'),'Summarize this scan.');await userEvent.click(canvas.getByRole('button',{name:'Review file and target'}))}
export const Ready:Story={globals:{locale:'en'},args:{scenario:'ready'},play:review}
export const Truncated:Story={globals:{locale:'en'},args:{scenario:'truncated'},play:review}
export const NoText:Story={globals:{locale:'en'},args:{scenario:'no-text'},play:review}
export const MismatchedMode:Story={globals:{locale:'en'},args:{scenario:'mismatch'},play:review}
export const Unknown:Story={globals:{locale:'en'},args:{scenario:'unknown'},play:async context=>{await review(context);const canvas=within(context.canvasElement);await userEvent.click(await canvas.findByRole('button',{name:'Send question and file text'}));await waitFor(()=>{if(!canvas.queryByRole('button',{name:'Check existing request'}))throw Error('Synthetic receipt check pending')})}}
export const SendLimitsUnavailable:Story={globals:{locale:'en'},args:{scenario:'send-limits-unavailable'},play:async context=>{await review(context);const canvas=within(context.canvasElement);await userEvent.click(await canvas.findByRole('button',{name:'Send question and file text'}));await waitFor(()=>{if(!canvas.queryByText(/Safe recognition limits are unavailable/))throw Error('Synthetic precommit error pending');if(canvas.queryByRole('button',{name:'Check existing request'}))throw Error('Precommit error incorrectly left unknown');if((canvas.getByLabelText('Question') as HTMLTextAreaElement).value!=='Summarize this scan.')throw Error('Question not retained')})}}
const admissionFailure=async(context:{canvasElement:HTMLElement})=>{await review(context);const canvas=within(context.canvasElement);await userEvent.click(await canvas.findByRole('button',{name:'Send question and file text'}));await waitFor(()=>{if(!canvas.queryByText(/request was not sent/))throw Error('Synthetic precommit error pending');if(canvas.queryByRole('button',{name:'Check existing request'}))throw Error('Precommit error incorrectly left unknown');if((canvas.getByLabelText('Question') as HTMLTextAreaElement).value!=='Summarize this scan.')throw Error('Question not retained')})}
export const SendBusy:Story={globals:{locale:'en'},args:{scenario:'send-busy'},play:admissionFailure}
export const SendInterrupted:Story={globals:{locale:'en'},args:{scenario:'send-interrupted'},play:admissionFailure}
