import api from '../lib/api'
import type { RoomEvent } from './events'
import type { LibraryUpload } from './libraryUploads'
import type { ContextPreviewResponse } from './contextPreviews'

export type FileQuestionInput = { bot_id:number; upload_id:string; sha256:string; content:string; mode?:'image'|'pdf_ocr'; full_access?:boolean }
type FileQuestionPreviewBase = { basis:string; file:LibraryUpload; target:{bot_id:number;bot_name:string;connection_id:number;model_id:string;connection_name:string;provider_id:string}; context_preview:ContextPreviewResponse }
export type TextFileQuestionPreview = FileQuestionPreviewBase & {mode?:'text'|'pdf_ocr'; extraction:{text_sha256:string;format:string;page_count:number|null;truncated:boolean;extractor:string;chars:number}; excerpt:string; full_access?:boolean; excerpt_sha256?:string; excerpt_chars?:number; range?:{max_chars:number;pdf_max_pages:number;ocr_max_pages:number}}
export type ImageFileQuestionPreview = FileQuestionPreviewBase & {mode:'image'; image:{mime:'image/png';width:number;height:number;size_bytes:number};requires_image_consent:true}
export type FileQuestionPreview = TextFileQuestionPreview | ImageFileQuestionPreview
const path=(room:number)=>`/conversations/${room}/file-question`
export const libraryFileQuestionsApi = {
 preview:async(room:number, input:FileQuestionInput, signal?:AbortSignal)=>(await api.post<FileQuestionPreview>(`${path(room)}/preview`,input,{signal})).data,
 send:async(room:number,input:FileQuestionInput,basis:string,key:string,signal?:AbortSignal,imageConsent=false,fullAccessConsent=false)=>{if(input.mode==='image'&&!imageConsent)throw Error('Image consent required');if(input.full_access===true&&(!fullAccessConsent||input.mode==='image'))throw Error('Full file access consent required');return (await api.post<RoomEvent>(path(room),{...input,basis,consent:true,...(input.mode==='image'?{image_consent:true}:{}),...(input.full_access===true?{full_access_consent:true}:{})},{headers:{'Idempotency-Key':key},signal})).data},
 receipt:async(room:number,key:string,signal?:AbortSignal)=>(await api.get<RoomEvent>(`${path(room)}/receipt`,{headers:{'Idempotency-Key':key},signal})).data,
}
