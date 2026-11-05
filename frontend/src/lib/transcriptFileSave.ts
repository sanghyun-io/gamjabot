import { invoke } from '@tauri-apps/api/core'
import { directoryViewGeneration } from './directoryViewMemory'
import type { WorkspaceSaveReceipt } from './workspaceSaveWorkspace'
export type TranscriptExportScope=Readonly<{range:'all'|'matches';body:'visible'|'original';messageCount:number;query?:string;actor?:'all'|'people'|'agents'}>
export type TranscriptSnapshot={text:string;title:string;capturedAt:string;scope?:TranscriptExportScope;filenameKind?:'result'}
export type TranscriptSave=Readonly<{id:string;exportId:string;title:string;capturedAt:string;suggestedFilename:string;scope?:TranscriptExportScope;phase:'choosing'|'saving'|'saved'|'cancelled'|'failed'|'unknown';filename?:string;receipt?:WorkspaceSaveReceipt;code?:string}>
const records=new Map<string,readonly TranscriptSave[]>(),listeners=new Set<()=>void>();let version=0,pending:string|null=null
const emit=()=>{version++;listeners.forEach(listener=>listener())}
export const transcriptSaveVersion=()=>version
const empty:readonly TranscriptSave[]=[]
export const readTranscriptSaves=(key:string)=>records.get(key)||empty
export const readTranscriptSave=(key:string,id?:string)=>id?readTranscriptSaves(key).find(record=>record.id===id):readTranscriptSaves(key).at(-1)
export const transcriptSavePending=()=>pending!==null
export function subscribeTranscriptSaves(listener:()=>void){listeners.add(listener);return()=>{listeners.delete(listener)}}
export function clearTranscriptSaves(){records.clear();pending=null;emit()}
const noWriteErrors=new Set(['export_unavailable','export_busy','export_canceled','export_invalid_payload','export_markdown_required','export_destination_unavailable','export_destination_changed','export_save_failed','export_selection_expired'])
// Metadata outcomes accumulate in this Owner/room login. No body is stored here.
function suggestedFilename(title:string,capturedAt:string,kind:'transcript'|'result'='transcript'){
 const encoder=new TextEncoder(),source=title.normalize('NFKC').replace(/[\\/:*?"<>|\u0000-\u001f\u007f-\u009f]/g,'-').replace(/\s+/g,' ').trim()
 let base='',bytes=0
 for(const point of source){const size=encoder.encode(point).byteLength;if(bytes+size>190)break;base+=point;bytes+=size}
 const utc=capturedAt.replace(/[-:]/g,'').replace(/\.\d+Z$/,'Z')
 return `${base||'room'}-${kind}${/^\d{8}T\d{6}Z$/.test(utc)?'-'+utc:''}.md`
}
// The complete Markdown exists only in the explicit save invocation until it settles.
export async function saveTranscript(key:string,generation:number,snapshot:TranscriptSnapshot,panelTitle:string,ownerCurrent:()=>boolean,viewCurrent:()=>boolean){
 if(pending||generation!==directoryViewGeneration()||!ownerCurrent()||!viewCurrent())return
 const original:TranscriptSave={id:crypto.randomUUID(),exportId:crypto.randomUUID(),title:snapshot.title,capturedAt:snapshot.capturedAt,suggestedFilename:suggestedFilename(snapshot.title,snapshot.capturedAt,snapshot.filenameKind==='result'?'result':'transcript'),scope:snapshot.scope?Object.freeze({...snapshot.scope}):undefined,phase:'choosing'}
 pending=original.id;records.set(key,[...readTranscriptSaves(key),original]);emit()
 const valid=()=>generation===directoryViewGeneration()&&ownerCurrent()
 const settle=(outcome:Pick<TranscriptSave,'phase'|'filename'|'receipt'|'code'>)=>{
  if(!valid()||!readTranscriptSave(key,original.id))return
  records.set(key,readTranscriptSaves(key).map(record=>record.id===original.id?{...record,...outcome}:record));if(outcome.phase!=='choosing'&&outcome.phase!=='saving'&&pending===original.id)pending=null;emit()
 }
 let selection:{selection_id:string;filename:string;replacing:boolean}|null=null
 const discard=async()=>{if(selection)try{await invoke('discard_workspace_export',{selectionId:selection.selection_id,context:original.id})}catch{/* No file is created by a selection; next choice retires it. */}}
 try{
  const bytes=new TextEncoder().encode(snapshot.text).byteLength
  if(!bytes||bytes>2*1024*1024||snapshot.text.includes('\0'))throw 'export_invalid_payload'
  selection=await invoke('choose_transcript_export',{context:original.id,exportId:original.exportId,title:panelTitle,filename:original.suggestedFilename})
  if(!selection){settle({phase:'cancelled'});return}
  if(!/^[a-f0-9]{32}$/.test(selection.selection_id)||typeof selection.filename!=='string'||typeof selection.replacing!=='boolean')throw 'export_transport_unknown'
  // Native chooser legitimately takes focus. The original Owner, room view and
  // intent must still exist after it closes; fresh incoming messages do not
  // replace the frozen body. Once OS writing starts, leaving the view cannot undo it.
  if(!valid()||!viewCurrent()){await discard();settle({phase:'cancelled'});return}
  settle({phase:'saving',filename:selection.filename})
  const receipt=await invoke<WorkspaceSaveReceipt>('save_transcript_export',{selectionId:selection.selection_id,context:original.id,exportId:original.exportId,content:snapshot.text})
  if(!receipt||typeof receipt.path!=='string'||!receipt.path||receipt.bytes!==bytes||receipt.replaced!==selection.replacing||typeof receipt.durability_unconfirmed!=='boolean'||receipt.reveal_id!=null&&!/^[a-f0-9]{32}$/.test(receipt.reveal_id))throw 'export_transport_unknown'
  settle({phase:'saved',filename:selection.filename,receipt})
 }catch(error){
  await discard()
  const code=typeof error==='string'&&noWriteErrors.has(error)?error:'export_transport_unknown'
  settle({phase:code==='export_canceled'?'cancelled':noWriteErrors.has(code)?'failed':'unknown',filename:selection?.filename,code})
 }
}
