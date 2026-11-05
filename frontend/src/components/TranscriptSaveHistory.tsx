import { useEffect,useId,useLayoutEffect,useMemo,useRef,useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { useI18n } from '../i18n'
import { readTranscriptSave,type TranscriptSave } from '../lib/transcriptFileSave'
import { useClipboardText } from '../lib/useClipboardText'
import ClipboardTextFallback from './ClipboardTextFallback'
import { TranscriptExportScopeSummary } from './TranscriptExportOptions'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
function SaveOutcome({scope,record,ordinal,canUse,kind}:{scope:string;record:TranscriptSave;ordinal:number;canUse:()=>boolean;kind?:'result'}){
 const {t,formatDateTime}=useI18n(),mounted=useRef(false),root=useRef<HTMLDivElement>(null),live=useRef(canUse);live.current=canUse
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
 const viewCurrent=()=>mounted.current&&live.current()&&!!root.current?.isConnected&&!!root.current.getClientRects().length
 const pathCopy=useClipboardText<{text:string;id:string}>(`${scope}:${record.id}`,item=>viewCurrent()&&readTranscriptSave(scope,item.id)?.receipt?.path===item.text)
 const [revealing,setRevealing]=useState(false),[revealStatus,setRevealStatus]=useState<'sent'|'changed'|'expired'|'failed'|''>(''),revealLock=useRef(false)
 const reveal=async()=>{
  const original=record
  if(!viewCurrent()||original?.phase!=='saved'||!original.receipt?.reveal_id||revealLock.current)return
  revealLock.current=true;setRevealing(true);setRevealStatus('')
  try{await invoke('reveal_workspace_export',{context:original.id,exportId:original.exportId,revealId:original.receipt.reveal_id});if(viewCurrent()&&readTranscriptSave(scope,original.id)?.receipt?.path===original.receipt?.path)setRevealStatus('sent')}
  catch(error){if(viewCurrent()&&readTranscriptSave(scope,original.id)?.receipt?.path===original.receipt?.path)setRevealStatus(error==='export_reveal_changed'?'changed':error==='export_reveal_expired'?'expired':'failed')}
  finally{revealLock.current=false;if(mounted.current)setRevealing(false)}
 }
 const pathSnapshot=()=>record?.receipt?{text:record.receipt.path,id:record.id}:null
 const copyPath=()=>{const value=pathSnapshot();if(value)void pathCopy.copy(value)}
 return (
  <div ref={root} className={`notice stack stack--tight ${record.phase==='saved'?'notice--success':record.phase==='failed'||record.phase==='unknown'?'notice--warning':'notice--info'}`}>
   <p role={record.phase==='failed'||record.phase==='unknown'?'alert':'status'}>{t(kind==='result'&&record.phase==='saved'?'results.fileSave.saved':`chat.fileSave.${record.phase}`)}</p>
   <p className="metadata">{t('chat.fileHistory.ordinal',{number:ordinal})} · {record.title} · {t('chat.fileSave.snapshot',{time:formatDateTime(record.capturedAt,{dateStyle:'medium',timeStyle:'medium'})})}</p>
   {record.scope&&<TranscriptExportScopeSummary scope={record.scope}/>}
   <p className="technical">{record.filename||record.suggestedFilename}</p>
   {!record.filename&&<p className="field-hint">{t('chat.fileHistory.suggested')}</p>}
   {record.code==='export_invalid_payload'&&<p>{t(kind==='result'?'results.fileSave.size':'chat.fileSave.size')}</p>}
   {record.code==='export_destination_changed'&&<p>{t('projects.fileExport.changed')}</p>}
   {record.receipt&&<><p className="technical">{record.receipt.path}</p>
    <div className="page-actions"><button type="button" className="text-link" disabled={pathCopy.busy} onClick={copyPath}>{t('projects.fileExport.copyPath')}</button><button type="button" className="text-link" onClick={()=>{const value=pathSnapshot();if(value)pathCopy.openManual(value)}}>{t('clipboard.path.select')}</button>
    {record.receipt.reveal_id&&<button type="button" className="text-link" disabled={revealing} onClick={()=>void reveal()}>{t(revealing?'common.loading':'projects.fileReveal.action')}</button>}</div>
    {pathCopy.result&&<p role={pathCopy.result.status==='copied'?'status':'alert'}>{t(pathCopy.result.status==='copied'?'projects.fileExport.pathCopied':'projects.fileExport.copyFailed')}</p>}
    {pathCopy.manual&&<ClipboardTextFallback rows={3} text={pathCopy.manual.text} label={t('clipboard.path.label')} caption={t('clipboard.path.help')} canUse={pathCopy.manualAllowed} onClose={pathCopy.closeManualAndRestore} onInteract={pathCopy.touchManual} busy={pathCopy.busy} onRetry={()=>{if(pathCopy.manual)void pathCopy.copy(pathCopy.manual)}}/>}
    {revealStatus&&<p role={revealStatus==='sent'?'status':'alert'}>{t(`projects.fileReveal.${revealStatus}`)}</p>}
    {record.receipt.durability_unconfirmed&&<p>{t('projects.fileExport.durability')}</p>}
    <p className="metadata">{t('projects.fileExport.receipt',{bytes:record.receipt.bytes})} · {t(record.receipt.replaced?'chat.fileHistory.replaced':'chat.fileHistory.newFile')}</p>
   </>}
  </div>
 )
}
type Filter='all'|'saved'|'active'|'unknown'|'failed'|'cancelled'
export default function TranscriptSaveHistory({scope,records,canUse,kind}:{scope:string;records:readonly TranscriptSave[];canUse:()=>boolean;kind?:'result'}){
 const {t}=useI18n(),listId=useId(),[open,setOpen]=useState(false),[query,setQuery]=useState(''),[filter,setFilter]=useState<Filter>('all'),[anchor,setAnchor]=useState<string|null>(null)
 const readingRoot=useRef<HTMLDivElement>(null),pageIntent=useRef<{context:object;id:string;origin:HTMLButtonElement}|null>(null)
 const pageContext=useMemo(()=>({}),[scope,records,query,filter])
 const latest=records.at(-1),older=records.slice(0,-1).map((record,index)=>({record,ordinal:index+1})).reverse()
 const matches=(value:string,state:Filter)=>{const match=directoryMatcher(value);return older.filter(({record,ordinal})=>(state==='all'||(state==='active'?record.phase==='choosing'||record.phase==='saving':record.phase===state))&&(!value.trim()||match([record.title,record.filename,record.suggestedFilename,record.receipt?.path,String(ordinal),t(kind==='result'&&record.phase==='saved'?'results.fileSave.saved':`chat.fileSave.${record.phase}`)])))}
 const found=open?matches(query,filter):[],start=anchor?Math.max(0,found.findIndex(row=>row.record.id===anchor)):0,shown=found.slice(start,start+5)
 const unknown=older.filter(row=>row.record.phase==='unknown').length
 const change=(value:string,state:Filter)=>{if(!canUse())return;pageIntent.current=null;setQuery(value);setFilter(state);setAnchor(matches(value,state)[0]?.record.id||null)}
 const move=(index:number,origin:HTMLButtonElement)=>{
  if(!canUse()||!open||!found.length||!Number.isSafeInteger(index)||!origin.isConnected||origin.disabled)return
  const target=found[Math.max(0,Math.min(index,found.length-1))]
  if(!target||target.record.id===shown[0]?.record.id)return
  origin.focus({preventScroll:true});pageIntent.current={context:pageContext,id:target.record.id,origin};setAnchor(target.record.id)
 }
 useLayoutEffect(()=>{
  const intent=pageIntent.current;pageIntent.current=null
  if(!intent||intent.context!==pageContext||intent.id!==shown[0]?.record.id||!open||!canUse()
    ||document.visibilityState!=='visible'||!document.hasFocus()||document.activeElement!==intent.origin&&document.activeElement!==document.body)return
  const target=readingRoot.current
  if(!target?.isConnected||!target.getClientRects().length||target.closest('[hidden], [inert], [aria-hidden="true"]'))return
  const top=Array.from(document.querySelectorAll<HTMLElement>('[aria-modal="true"]')).filter(node=>node.getClientRects().length>0
    &&!node.closest('[hidden], [inert], [aria-hidden="true"]')&&getComputedStyle(node).visibility!=='hidden').at(-1)
  if(top&&!top.contains(target))return
  // A reading region cannot activate copy/reveal when the paging Enter key repeats.
  target.focus({preventScroll:true});target.scrollIntoView({block:'start',inline:'nearest'})
 },[anchor,pageContext,open,shown[0]?.record.id])
 if(!latest)return null
 return <div className="stack stack--tight">
  <SaveOutcome key={latest.id} kind={kind} scope={scope} record={latest} ordinal={records.length} canUse={canUse}/>
  {unknown>0&&<p className="notice notice--warning" role="status">{t('chat.fileHistory.unknown',{count:unknown})} <button type="button" className="text-link" onClick={()=>{if(canUse()){change('','unknown');setOpen(true)}}}>{t('chat.fileHistory.showUnknown')}</button></p>}
  {older.length>0&&<details className="compact-details" open={open} onToggle={event=>{if(event.target!==event.currentTarget||!canUse())return;const next=event.currentTarget.open;if(!next)pageIntent.current=null;setOpen(next);if(next&&!open)setAnchor(matches(query,filter)[0]?.record.id||null)}}>
   <summary>{t('chat.fileHistory.previous',{count:older.length})}</summary>
   {open&&<div className="stack stack--tight">
    <p className="field-hint">{t(kind==='result'?'results.fileSave.historyScope':'chat.fileHistory.scope')}</p>
    <DirectorySearch value={query} onChange={value=>change(value,filter)} label={t('chat.fileHistory.find')} hint={t('chat.fileHistory.find')} shown={found.length} total={older.length} known loading={false} controls={listId} preventSubmit/>
    <label className="field">{t('projects.fileHistory.state')}<select value={filter} aria-controls={listId} onChange={event=>change(query,event.target.value as Filter)}>
     <option value="all">{t('projects.fileHistory.all')}</option><option value="saved">{t('projects.fileHistory.saved')}</option><option value="active">{t('projects.fileHistory.active')}</option><option value="unknown">{t('chat.fileSave.unknownShort')}</option><option value="failed">{t('chat.fileSave.failedShort')}</option><option value="cancelled">{t('projects.fileHistory.cancelled')}</option>
    </select></label>
    {(query||filter!=='all')&&<button type="button" className="text-link" onClick={()=>change('','all')}>{t('projects.fileHistory.reset')}</button>}
    <p className="metadata" role="status">{found.length?t('chat.fileHistory.range',{from:start+1,to:Math.min(start+5,found.length),total:found.length}):t('directory.noMatches')}</p>
    <div className="page-actions"><button type="button" className="text-link" disabled={!found.length||start===0} onClick={event=>move(0,event.currentTarget)}>{t('chat.fileHistory.newest')}</button><button type="button" className="text-link" disabled={start===0} onClick={event=>move(start-5,event.currentTarget)}>{t('chat.fileHistory.newer')}</button><button type="button" className="text-link" disabled={start+5>=found.length} onClick={event=>move(start+5,event.currentTarget)}>{t('chat.fileHistory.older')}</button><button type="button" className="text-link" disabled={start+5>=found.length} onClick={event=>move(Math.max(0,found.length-5),event.currentTarget)}>{t('chat.fileHistory.oldest')}</button></div>
    <p className="field-hint">{t('chat.fileHistory.pageReadHelp')}</p>
    <div ref={readingRoot} id={listId} role="region" tabIndex={-1} aria-label={found.length?t('chat.fileHistory.range',{from:start+1,to:Math.min(start+5,found.length),total:found.length}):t('directory.noMatches')} className="stack stack--tight save-history-reading-page">{shown.map(({record,ordinal})=><SaveOutcome key={record.id} kind={kind} scope={scope} record={record} ordinal={ordinal} canUse={canUse}/>)}</div>
   </div>}
  </details>}
 </div>
}
