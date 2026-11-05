import { useEffect,useRef,useSyncExternalStore } from 'react'
import { invoke,isTauri } from '@tauri-apps/api/core'
import { useQuery } from '@tanstack/react-query'
import { Download } from 'lucide-react'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { readTranscriptSaves,saveTranscript,subscribeTranscriptSaves,transcriptSavePending,transcriptSaveVersion,type TranscriptSnapshot } from '../lib/transcriptFileSave'
import TranscriptSaveHistory from './TranscriptSaveHistory'
export default function TranscriptFileSave({ownerId,epoch,roomId,ready,snapshot,canUse}:{ownerId:number;epoch:number;roomId:number;ready:boolean;snapshot:()=>TranscriptSnapshot|null;canUse:()=>boolean}){
 const {t}=useI18n(),generation=useRef(directoryViewGeneration()).current,key=`${ownerId}:${epoch}:${roomId}`
 const mounted=useRef(false),live=useRef({canUse,key});live.current={canUse,key}
 const button=useRef<HTMLButtonElement>(null),intent=useRef(0)
 useEffect(()=>{mounted.current=true
  const details=button.current?.closest('details')
  const toggle=(event:Event)=>{if(event.target===details&&!details?.open)intent.current++}
  details?.addEventListener('toggle',toggle)
  return()=>{mounted.current=false;intent.current++;details?.removeEventListener('toggle',toggle)}
 },[])
 const ownerCurrent=()=>{const auth=useAuthStore.getState();return auth.user?.id===ownerId&&auth.epoch===epoch&&generation===directoryViewGeneration()}
 const viewCurrent=()=>mounted.current&&live.current.key===key&&ownerCurrent()&&live.current.canUse()&&!!button.current?.isConnected&&!!button.current.getClientRects().length
 useSyncExternalStore(subscribeTranscriptSaves,transcriptSaveVersion)
 const records=readTranscriptSaves(key),record=records.at(-1),pending=transcriptSavePending()
 const transport=useQuery({queryKey:['transcript-save-transport',ownerId,epoch],queryFn:()=>invoke<string>('workspace_export_transport'),enabled:isTauri(),retry:false,refetchOnWindowFocus:false,refetchOnReconnect:false})
 const save=()=>{
  if(!ready||transport.data!=='native'||pending||!viewCurrent())return
  const frozen=snapshot();if(!frozen)return
  const originalIntent=++intent.current
  void saveTranscript(key,generation,frozen,t('chat.fileSave.panelTitle'),ownerCurrent,()=>viewCurrent()&&intent.current===originalIntent)
 }
 if(!isTauri())return null
 return <section className="stack stack--tight" style={{gridColumn:'1 / -1',minWidth:0}} aria-label={t('chat.fileSave.action')}>
  <div className="page-actions"><button ref={button} type="button" className="button button--secondary" disabled={!ready||pending||transport.data!=='native'} onClick={save}><Download size={16} aria-hidden="true"/>{t('chat.fileSave.action')}</button></div>
  <p className="field-hint">{t('chat.fileSave.scope')}</p><details className="compact-details"><summary>{t('common.technicalDetails')}</summary><p>{t('chat.fileSave.limit')}</p></details>
  {transport.isFetching&&<p role="status">{t('common.loading')}</p>}
  {(transport.isError||transport.isSuccess&&transport.data!=='native')&&<p role="status">{t('chat.fileSave.unavailable')}</p>}
  {pending&&record?.phase!=='choosing'&&record?.phase!=='saving'&&<p role="status">{t('chat.fileSave.otherPending')}</p>}
  <TranscriptSaveHistory key={key} scope={key} records={records} canUse={viewCurrent}/>
 </section>
}
