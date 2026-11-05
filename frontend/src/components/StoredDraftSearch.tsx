import { useExplicitReaderReveal } from '../lib/useExplicitReaderReveal'
import StoredDraftReader, { type ReceivedDraft } from './StoredDraftReader'
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import { draftsApi, storedDraftsApi, type StoredDraftItem } from '../api/drafts'
import { listUnsavedDrafts } from '../lib/unsavedComposerDrafts'
import { reviewTimestamp, reviewTimeOptions } from '../lib/reviewTime'
export type StoredDraftSearchHandle = { search: () => void }
export default forwardRef<StoredDraftSearchHandle, { identity:string;query:string;canUse:()=>boolean;onOpen:(room:number)=>void }>(function StoredDraftSearch({ identity, query, canUse, onOpen }, ref) {
 const {t,formatDateTime}=useI18n(),mounted=useRef(false),lock=useRef(false),bodyLock=useRef(false),request=useRef(0),live=useRef(canUse);live.current=canUse
 const [status,setStatus]=useState<'active'|'closed'|'all'>('all'),[busy,setBusy]=useState(false),[failed,setFailed]=useState(false),[rows,setRows]=useState<StoredDraftItem[]>([]),[snapshot,setSnapshot]=useState<{query:string;status:'active'|'closed'|'all';next:string|null}|null>(null)
 const [bodyQuery,setBodyQuery]=useState(''),[bodyBusy,setBodyBusy]=useState(false),[bodyFailed,setBodyFailed]=useState(false),[body,setBody]=useState<ReceivedDraft|null>(null),[reading,setReading]=useState<{room:number;label:string}|null>(null),[failedRead,setFailedRead]=useState<number|null>(null),bodyRequest=useRef(0)
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;request.current++;bodyRequest.current++}},[])
 const valid=()=>mounted.current&&live.current()
 const conditionsChanged=!!snapshot&&(snapshot.query!==query.trim()||snapshot.status!==status)
 const reveal=useExplicitReaderReveal(body?.readId??null,valid)
 const closeReader=()=>{if(!valid())return;reveal.back();bodyRequest.current++;reveal.cancel();setBody(null);setBodyFailed(false)}
 const search=async(more=false)=>{
  if(!valid()||lock.current||query.trim().length>200||more&&(!snapshot?.next||snapshot.query!==query.trim()||snapshot.status!==status))return
  const q=more?snapshot!.query:query.trim(),filter=more?snapshot!.status:status,cursor=more?snapshot!.next!:undefined,run=++request.current
  lock.current=true;setBusy(true);setFailed(false)
  try{
   const page=await storedDraftsApi.list(q,filter,cursor)
   if(!valid()||run!==request.current)return
   if(!Array.isArray(page.items)||page.items.some(row=>!Number.isSafeInteger(row.room_id)||row.room_id<1||!Number.isSafeInteger(row.version)||row.version<1||typeof row.preview!=='string'||typeof row.room_title!=='string'||typeof row.project_name!=='string'||typeof row.read_only!=='boolean')||page.next_cursor!==null&&typeof page.next_cursor!=='string')throw new Error('Invalid draft list')
   setRows(previous=>{const map=new Map((more?previous:[]).map(row=>[row.room_id,row]));for(const row of page.items){const old=map.get(row.room_id);if(!old||row.version>=old.version)map.set(row.room_id,row)}return [...map.values()]})
   setSnapshot({query:q,status:filter,next:page.next_cursor})
  }catch{if(valid()&&run===request.current)setFailed(true)}finally{lock.current=false;if(mounted.current)setBusy(false)}
 }
 useImperativeHandle(ref,()=>({search:()=>{void search()}}))
 const read=async(row:StoredDraftItem,origin:HTMLButtonElement)=>{
  if(!valid()||bodyLock.current)return
  const run=++bodyRequest.current
  const queryAtRead=snapshot?.query||''
  let completed=false;reveal.begin(origin);setFailedRead(null)
  bodyLock.current=true;setBodyBusy(true);setBodyFailed(false);setReading({room:row.room_id,label:row.room_title+' · #'+row.room_id})
  try{
   const value=await draftsApi.get(row.room_id)
   if(!valid()||run!==bodyRequest.current)return
   if(typeof value.content!=='string'||!Number.isSafeInteger(value.version)||value.version<0)throw new Error('Invalid draft')
   setBody({readId:run,room:row.room_id,title:row.room_title,project:row.project_name,listedVersion:row.version,version:value.version,content:value.content});setBodyQuery(queryAtRead);completed=true
  }catch{if(valid()&&run===bodyRequest.current){setBodyFailed(true);setFailedRead(row.room_id)}}finally{if(!completed)reveal.cancel();bodyLock.current=false;if(mounted.current){setBodyBusy(false);setReading(null)}}
 }

 return <section className="stack stack--tight" aria-label={t('switcher.storedDrafts.title')}>
  <h3 className="section-title">{t('switcher.storedDrafts.title')}</h3><p className="field-hint">{t('switcher.storedDrafts.help')}</p>
  <label className="field">{t('switcher.status')}<select disabled={busy} value={status} onChange={event=>{if(valid())setStatus(event.target.value as typeof status)}}><option value="all">{t('switcher.all')}</option><option value="active">{t('switcher.active')}</option><option value="closed">{t('switcher.closed')}</option></select></label>
  <button type="button" className="button button--secondary button--compact" disabled={busy||query.trim().length>200} onClick={()=>void search()}>{t(busy?'common.loading':'switcher.storedDrafts.search')}</button>
  {query.trim().length>200&&<p role="alert">{t('switcher.storedDrafts.long')}</p>}
  {snapshot&&<p className="metadata">{t('switcher.storedDrafts.snapshot',{query:snapshot.query||t('switcher.all')})} · {t(`switcher.${snapshot.status}`)}</p>}
  {conditionsChanged&&<div className="notice notice--info stack stack--tight">
   <p role="status">{t('switcher.storedDrafts.conditionsChanged')}</p>
   <div><button type="button" className="button button--secondary button--compact" disabled={busy||query.trim().length>200} onClick={()=>void search()}>{t(busy?'common.loading':'switcher.storedDrafts.searchCurrent')}</button></div>
  </div>}
  {failed&&<p role="alert">{t('switcher.storedDrafts.failure')}</p>}
  {snapshot&&!rows.length&&<p role="status">{t('switcher.storedDrafts.empty')}</p>}
  {body&&<div ref={reveal.target} tabIndex={-1} onKeyDown={event=>{if(event.target!==event.currentTarget||event.key!=='Escape')return;event.stopPropagation();if(event.nativeEvent.isComposing||event.nativeEvent.keyCode===229)return;event.preventDefault();closeReader()}} aria-label={t('switcher.storedDrafts.read')}><StoredDraftReader key={body.readId} focusScope={reveal.target} value={body} initialQuery={bodyQuery} canUse={valid} onBack={reveal.back} onOpen={()=>onOpen(body.room)} onClose={closeReader}/></div>}
  {bodyFailed&&!rows.some(row=>row.room_id===failedRead)&&<p role="alert">{t('switcher.storedDrafts.bodyFailure')}</p>}
  {reading&&!rows.some(row=>row.room_id===reading.room)&&<p role="status">{t('switcher.draftReader.loading',{room:reading.label})}</p>}
  <ul id="quick-stored-draft-list" className="stack stack--tight">{rows.map(row=>{const local=listUnsavedDrafts(identity).find(item=>item.room===row.room_id),time=reviewTimestamp(row.updated_at);return <li key={row.room_id} className="notice stack stack--tight"><strong>{row.room_title} · #{row.room_id}</strong><p className="metadata">{row.project_name} · v{row.version} · {row.character_count}{row.read_only&&<> · {t('switcher.readOnly')}</>}{time&&<> · {formatDateTime(time,reviewTimeOptions)}</>}</p><p>{row.preview}</p>{local&&<p className="metadata">{t('switcher.storedDrafts.local')}</p>}<div className="page-actions"><button type="button" className="text-link" onClick={()=>{if(valid())onOpen(row.room_id)}}>{t('switcher.storedDrafts.open')}</button><button type="button" className="text-link" disabled={bodyBusy} onClick={event=>void read(row,event.currentTarget)}>{t('switcher.storedDrafts.read')}</button>{body?.room===row.room_id&&<button type="button" className="text-link" onClick={()=>reveal.jump()}>{t('switcher.readerNav.view')}</button>}</div>{reading?.room===row.room_id&&<p role="status">{t('common.loading')}</p>}{failedRead===row.room_id&&bodyFailed&&<p role="alert">{t('switcher.storedDrafts.bodyFailure')}</p>}</li>})}</ul>
  {snapshot?.next&&<button type="button" className="button button--secondary button--compact" disabled={busy||conditionsChanged} onClick={()=>void search(true)}>{t('switcher.more')}</button>}



 </section>
})
