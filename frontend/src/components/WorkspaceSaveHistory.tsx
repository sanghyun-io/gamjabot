import { useEffect,useId,useRef,useState,useSyncExternalStore } from 'react'
import { invoke,isTauri } from '@tauri-apps/api/core'
import { WorkspaceExportSummaryView } from './WorkspaceExportPreview'
import ClipboardTextFallback from './ClipboardTextFallback'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useClipboardText } from '../lib/useClipboardText'
import { useDirectoryValue } from '../lib/useDirectoryView'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { transferOwnerCurrent } from '../lib/transferActions'
import { readWorkspaceSaves,subscribeWorkspaceSaves,workspaceSaveVersion,type WorkspaceSaveCommand } from '../lib/workspaceSaveWorkspace'
type SaveFilter='all'|'saved'|'active'|'attention'|'cancelled'
function savePhaseKey(phase:WorkspaceSaveCommand['phase']):MessageKey{
 return phase==='saved'?'projects.fileExport.saved':phase==='choosing'?'projects.fileExport.choosing':phase==='saving'?'projects.fileExport.saving':phase==='cancelled'?'projects.fileExport.cancelled':phase==='failed'?'projects.fileExport.failed':'projects.fileExport.unknown'
}
function saveGroup(phase:WorkspaceSaveCommand['phase']):SaveFilter{
 return phase==='saved'?'saved':phase==='cancelled'?'cancelled':phase==='choosing'||phase==='saving'?'active':'attention'
}
function SaveOutcome({identity,command}:{identity:string;command:WorkspaceSaveCommand}){
 const {t,formatDateTime}=useI18n(),generation=useRef(directoryViewGeneration()).current,mounted=useRef(false),[revealing,setRevealing]=useState(false),[revealStatus,setRevealStatus]=useState<'sent'|'changed'|'expired'|'failed'|''>(''),revealPending=useRef(false)
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
 const pathCopy=useClipboardText<{text:string;commandId:string;exportId:string}>(`${identity}:${generation}:${command.id}:${command.exportId}`,
  item=>mounted.current&&transferOwnerCurrent(identity,generation)&&command.id===item.commandId&&command.exportId===item.exportId&&command.receipt?.path===item.text)
 const pathSnapshot=()=>command.receipt?{text:command.receipt.path,commandId:command.id,exportId:command.exportId}:null
 const copyPath=()=>{const value=pathSnapshot();if(value)void pathCopy.copy(value)}
 const selectPath=()=>{const value=pathSnapshot();if(value)pathCopy.openManual(value)}
 const reveal=async()=>{
  if(!isTauri()||!transferOwnerCurrent(identity,generation)||command.phase!=='saved'||!command.receipt?.reveal_id||revealPending.current)return
  revealPending.current=true;setRevealing(true);setRevealStatus('')
  try{
   await invoke('reveal_workspace_export',{context:command.id,exportId:command.exportId,revealId:command.receipt.reveal_id})
   if(mounted.current&&transferOwnerCurrent(identity,generation))setRevealStatus('sent')
  }catch(error){if(mounted.current&&transferOwnerCurrent(identity,generation))setRevealStatus(error==='export_reveal_changed'?'changed':error==='export_reveal_expired'?'expired':'failed')}
  finally{revealPending.current=false;if(mounted.current&&transferOwnerCurrent(identity,generation))setRevealing(false)}
 }
 return <div className={`notice stack stack--tight ${command.phase==='saved'?'notice--success':command.phase==='failed'||command.phase==='unknown'?'notice--warning':'notice--info'}`}>
  <p className="metadata">{t('projects.workspace.started',{time:formatDateTime(command.startedAt,{dateStyle:'medium',timeStyle:'short'})})}</p>
  <p role={command.phase==='failed'||command.phase==='unknown'?'alert':'status'}>{t(savePhaseKey(command.phase))}</p>
  {command.filename&&<p>{command.filename}</p>}
  <p className="field-hint">{t('projects.exportSnapshot.savedOriginal')}</p>
  {command.summary?<WorkspaceExportSummaryView identity={identity} summary={command.summary}/>:<p>{t('projects.exportSnapshot.summaryUnavailable')}</p>}
  {command.code==='export_destination_changed'&&<p>{t('projects.fileExport.changed')}</p>}
  {command.receipt&&<><p className="technical">{command.receipt.path}</p>
   <div className="page-actions"><button type="button" className="button button--secondary button--compact" disabled={pathCopy.busy} onClick={copyPath}>{t(pathCopy.busy?'common.loading':'projects.fileExport.copyPath')}</button><button type="button" className="text-link" onClick={selectPath}>{t('clipboard.path.select')}</button>
   {isTauri()&&command.phase==='saved'&&command.receipt.reveal_id&&<button type="button" className="button button--secondary button--compact" disabled={revealing} onClick={()=>void reveal()}>{revealing?t('common.loading'):t('projects.fileReveal.action')}</button>}</div>
   {revealStatus&&<p role={revealStatus==='sent'?'status':'alert'}>{t(`projects.fileReveal.${revealStatus}`)}</p>}
   {pathCopy.result&&<p role={pathCopy.result.status==='copied'?'status':'alert'}>{t(pathCopy.result.status==='copied'?'projects.fileExport.pathCopied':'projects.fileExport.copyFailed')}</p>}
   {pathCopy.manual&&<ClipboardTextFallback rows={3} text={pathCopy.manual.text} label={t('clipboard.path.label')} caption={t('clipboard.path.help')} canUse={pathCopy.manualAllowed} onClose={pathCopy.closeManualAndRestore} onInteract={pathCopy.touchManual} busy={pathCopy.busy} onRetry={()=>{if(pathCopy.manual)void pathCopy.copy(pathCopy.manual)}}/>}
   {command.receipt.durability_unconfirmed&&<p>{t('projects.fileExport.durability')}</p>}<details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p>{t('projects.fileExport.receipt',{bytes:command.receipt.bytes})}</p><p>{t(command.receipt.replaced?'projects.fileExport.replaced':'projects.fileExport.newFile')}</p><p>{t('projects.fileExport.limit')}</p><p>{t('projects.fileReveal.limit')}</p></details></>}
 </div>
}
export default function WorkspaceSaveHistory({identity}:{identity:string}){
 const {t}=useI18n(),generation=directoryViewGeneration(),listId=useId()
 useSyncExternalStore(subscribeWorkspaceSaves,workspaceSaveVersion)
 const [query,setQuery]=useDirectoryValue<string>(identity,'exports:save-history-query','')
 const [filter,setFilter]=useDirectoryValue<SaveFilter>(identity,'exports:save-history-filter','all')
 const [olderOpen,setOlderOpen]=useDirectoryValue<boolean>(identity,'exports:save-history-older-open',false)
 const commands=readWorkspaceSaves(identity).commands,latest=commands.at(-1);if(!latest)return null
 const important=new Set(commands.filter(command=>command.phase==='choosing'||command.phase==='saving'||command.phase==='unknown').map(command=>command.id));important.add(latest.id)
 const match=directoryMatcher(query),hasQuery=!!query.trim(),filtered=hasQuery||filter!=='all'
 // Search only the received filename/path and the same status text displayed
 // by the card. No blueprint contents, credentials, error payload or disk read.
 const matching=new Set(commands.filter(command=>(filter==='all'||saveGroup(command.phase)===filter)&&(!hasQuery||match([command.filename,command.receipt?.path,t(savePhaseKey(command.phase))]))).map(command=>command.id))
 const older=commands.filter(command=>!important.has(command.id)),olderMatches=older.filter(command=>matching.has(command.id)).length
 const pinned=commands.filter(command=>important.has(command.id)&&!matching.has(command.id)).length
 const changeView=(nextQuery:string,nextFilter:SaveFilter)=>{
  if(!transferOwnerCurrent(identity,generation))return
  setQuery(nextQuery);setFilter(nextFilter)
  // An explicit find action opens older results. Background record updates
  // leave this disclosure and focus alone; remember explicit close/reopen.
  if(nextQuery.trim()||nextFilter!=='all')setOlderOpen(true)
 }
 const hideOlder=older.length===0||filtered&&olderMatches===0
 return <section className="stack stack--tight" aria-label={t('projects.fileExport.history')}>
  <h3 className="section-title">{t('projects.fileHistory.find')}</h3>
  <DirectorySearch value={query} onChange={value=>changeView(value,filter)} label={t('projects.fileHistory.find')} hint={t('projects.fileHistory.placeholder')}
   shown={matching.size} total={commands.length} known loading={false} controls={listId} preventSubmit/>
  <div className="page-actions"><label className="field">{t('projects.fileHistory.state')}
   <select value={filter} aria-controls={listId} onChange={event=>changeView(query,event.target.value as SaveFilter)}>
    <option value="all">{t('projects.fileHistory.all')}</option>
    <option value="saved">{t('projects.fileHistory.saved')}</option>
    <option value="active">{t('projects.fileHistory.active')}</option>
    <option value="attention">{t('projects.fileHistory.attention')}</option>
    <option value="cancelled">{t('projects.fileHistory.cancelled')}</option>
   </select>
  </label>{filtered&&<button type="button" className="button button--secondary button--compact" onClick={()=>changeView('','all')}>{t('projects.fileHistory.reset')}</button>}</div>
  <p className="field-hint">{t('projects.fileHistory.scope')}</p>
  {pinned>0&&<p role="status" className="notice notice--info">{t('projects.fileHistory.pinned',{count:pinned})}</p>}
  {matching.size===0&&<p role="status">{t('directory.noMatches')}</p>}
  <div id={listId} className="stack stack--tight">
   {commands.filter(command=>important.has(command.id)).map(command=><div key={command.id} className="stack stack--tight">
    {!matching.has(command.id)&&<p className="metadata">{t('projects.fileHistory.pinnedRow')}</p>}
    <SaveOutcome key={command.id} identity={identity} command={command}/>
   </div>)}
   <details key={`${identity}:${generation}`} open={olderOpen} onToggle={event=>{if(transferOwnerCurrent(identity,generation))setOlderOpen(event.currentTarget.open)}} className="compact-details" hidden={hideOlder} style={{display:hideOlder?'none':undefined}}>
    <summary>{filtered?t('projects.fileHistory.previousMatches',{count:olderMatches}):t('projects.workspace.previous')}</summary>
    {older.map(command=><div key={command.id} hidden={!matching.has(command.id)} style={{display:matching.has(command.id)?undefined:'none'}}>
     {/* Keep copy/reveal/manual states mounted while this view is filtered. */}
     <SaveOutcome key={command.id} identity={identity} command={command}/>
    </div>)}
   </details>
  </div>
 </section>
}
