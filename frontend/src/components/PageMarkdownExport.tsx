import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { useQuery } from '@tanstack/react-query'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { pageMarkdownExport } from '../lib/pageMarkdownExport'
import { readTranscriptSaves, saveTranscript, subscribeTranscriptSaves, transcriptSavePending, transcriptSaveVersion } from '../lib/transcriptFileSave'
import TranscriptSaveHistory from './TranscriptSaveHistory'

type SyntheticSave='saved'|'cancelled'|'unknown'|'failed'
export type PageMarkdownBridge={available:boolean;transport:()=>Promise<string>;save:(text:string)=>Promise<SyntheticSave>}
type Props={ownerId:number;epoch:number;roomId:number;resultId:string;version:number;title:string;content:string;canUse:()=>boolean;bridge?:PageMarkdownBridge}
export default function PageMarkdownExport({ownerId,epoch,roomId,resultId,version,title,content,canUse,bridge}:Props) {
 const {locale,t}=useI18n(),ko=locale==='ko',tr=(a:string,b:string)=>ko?a:b
 const generation=useRef(directoryViewGeneration()).current,key=`${ownerId}:${epoch}:${roomId}:page-markdown:${resultId}:${version}`
 const root=useRef<HTMLDetailsElement>(null),mounted=useRef(false),intent=useRef(0),live=useRef({canUse,content,title,key});live.current={canUse,content,title,key}
 const [open,setOpen]=useState(false),[ack,setAck]=useState<string|null>(null),[fakeOutcome,setFakeOutcome]=useState<SyntheticSave|'saving'|null>(null)
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;intent.current++}},[])
 const ownerCurrent=()=>{const a=useAuthStore.getState();return a.user?.id===ownerId&&a.epoch===epoch&&directoryViewGeneration()===generation}
 const viewCurrent=()=>mounted.current&&ownerCurrent()&&live.current.key===key&&live.current.canUse()&&!!root.current?.open&&root.current.isConnected&&!!root.current.getClientRects().length
 useSyncExternalStore(subscribeTranscriptSaves,transcriptSaveVersion)
 const records=readTranscriptSaves(key),pending=transcriptSavePending(),latest=records.at(-1)
 const native=bridge?bridge.available:isTauri()
 const transport=useQuery({queryKey:['page-markdown-transport',ownerId,epoch,!!bridge],queryFn:()=>bridge?bridge.transport():invoke<string>('workspace_export_transport'),enabled:open&&native,retry:false,refetchOnWindowFocus:false,refetchOnReconnect:false})
 const output=useMemo(()=>open?pageMarkdownExport(content):null,[open,content])
 const save=()=>{if(!output?.ok||pending||fakeOutcome==='saving'||transport.data!=='native'||!viewCurrent()||(latest?.phase==='unknown'&&ack!==latest.id)||(fakeOutcome==='unknown'&&ack!=='synthetic'))return
  const captured=++intent.current,frozen=content,frozenTitle=title,text=output.text
  if(bridge){setAck(null);setFakeOutcome('saving');void bridge.save(text).then(result=>{if(viewCurrent()&&intent.current===captured&&live.current.content===frozen&&live.current.title===frozenTitle)setFakeOutcome(result)}).catch(()=>{if(viewCurrent()&&intent.current===captured)setFakeOutcome('unknown')});return}
  void saveTranscript(key,generation,{text,title:`${frozenTitle} · v${version}`,capturedAt:new Date().toISOString(),filenameKind:'result'},tr('Page Markdown 저장','Save Page Markdown'),ownerCurrent,()=>viewCurrent()&&intent.current===captured&&live.current.content===frozen&&live.current.title===frozenTitle)
 }
 return <details ref={root} className="compact-details" onToggle={event=>{if(event.target!==event.currentTarget)return;if(!event.currentTarget.open){intent.current++;setFakeOutcome(null)}setOpen(event.currentTarget.open)}}><summary>{tr('읽기용 Markdown 저장','Save readable Markdown')} · v{version}</summary>
 {open&&<div className="stack stack--tight"><p className="field-hint">{tr('전체 데이터와 모든 탭을 담습니다. 화면 필터는 적용되지 않으며 차트는 숫자 표로 저장됩니다. 저장된 파일은 상호작용하지 않습니다.','Includes all data and tabs. Current filters are not applied; charts become data tables. The saved file is not interactive.')}</p>
 {!output?.ok&&<p role="alert">{output?.reason==='too_large'?tr('내용이 너무 커서 저장할 수 없습니다.','The export is too large to save.'):output?.reason==='unsupported'?tr('일부 값을 안전하게 변환할 수 없어 저장하지 않았습니다.','Some values cannot be converted safely.'):tr('Page 구조를 확인할 수 없어 저장할 수 없습니다.','The Page structure could not be verified.')}</p>}
 {output?.ok&&<><p>{tr('저장 내용 미리보기','Export preview')} · {output.bytes.toLocaleString()} bytes</p><pre className="skill-content" style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxHeight:'18rem',overflowY:'auto'}}>{output.text.slice(0,16000)}</pre>{output.text.length>16000&&<p className="field-hint">{tr('미리보기 일부만 표시합니다. 저장에는 전체 내용이 포함됩니다.','Preview is shortened; the saved file contains the complete export.')}</p>}</>}
 {!native?<p>{tr('기기에 저장하려면 Mac 앱을 사용하세요.','Use the Mac app to save to your device.')}</p>:<><button type="button" className="button button--secondary button--compact" disabled={!output?.ok||pending||fakeOutcome==='saving'||transport.data!=='native'||latest?.phase==='unknown'&&ack!==latest.id||fakeOutcome==='unknown'&&ack!=='synthetic'} onClick={save}>{tr('기기에 저장','Save to device')}</button>{transport.isFetching&&<p role="status">{t('common.loading')}</p>}{(transport.isError||transport.isSuccess&&transport.data!=='native')&&<p role="alert">{tr('저장 기능을 사용할 수 없습니다.','Saving is unavailable.')}</p>}{latest?.phase==='unknown'&&ack!==latest.id&&<div className="notice notice--warning"><p>{tr('파일이 저장됐는지 확인할 수 없습니다. 선택한 위치를 먼저 확인하세요.','The save result is unknown. Check the selected location first.')}</p><button type="button" className="button button--secondary button--compact" onClick={()=>setAck(latest.id)}>{tr('위치를 확인했습니다','I checked the location')}</button></div>}{bridge&&fakeOutcome&&<p role={fakeOutcome==='unknown'||fakeOutcome==='failed'?'alert':'status'}>{fakeOutcome==='saving'?tr('저장 중','Saving'):fakeOutcome==='saved'?tr('파일을 저장했습니다.','File saved.'):fakeOutcome==='cancelled'?tr('저장을 취소했습니다.','Save cancelled.'):fakeOutcome==='failed'?tr('저장하지 못했습니다.','Save failed.'):tr('저장 결과를 확인할 수 없습니다.','Save result is unknown.')}</p>}{bridge&&fakeOutcome==='unknown'&&ack!=='synthetic'&&<button type="button" className="button button--secondary button--compact" onClick={()=>setAck('synthetic')}>{tr('위치를 확인했습니다','I checked the location')}</button>}{!bridge&&<TranscriptSaveHistory kind="result" scope={key} records={records} canUse={viewCurrent}/>}</>}
 </div>}</details>
}
