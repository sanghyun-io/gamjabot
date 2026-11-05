import { useEffect,useRef,useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { invoke,isTauri } from '@tauri-apps/api/core'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration,readDirectoryView,writeDirectoryView } from '../lib/directoryViewMemory'
import { useI18n } from '../i18n'
import { StatusText } from './ui'
type Permission='granted'|'not_granted'|'unsupported'
type Panel='screen_recording'|'accessibility'
type Observation={receivedAt:string;screen_recording:Permission;accessibility:Permission}
const permissions=new Set<unknown>(['granted','not_granted','unsupported'])
const panels:readonly Panel[]=['screen_recording','accessibility']
export default function LocalPermissionGuide({identity}:{identity:string}){
 const {t,formatDateTime}=useI18n(),generation=useRef(directoryViewGeneration()).current,mounted=useRef(false),lock=useRef(false),root=useRef<HTMLDetailsElement>(null)
 const [open,setOpen]=useState(false),[pending,setPending]=useState<'read'|Panel|null>(null)
 const [record,setRecord]=useState(()=>readDirectoryView<Observation|null>(identity,'home:local-permissions',null))
 const [failure,setFailure]=useState<'read'|Panel|null>(null),[requested,setRequested]=useState<Panel|null>(null)
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
 const current=()=>{const auth=useAuthStore.getState();return mounted.current&&!!auth.token&&`${auth.user?.id||0}:${auth.epoch}`===identity&&directoryViewGeneration()===generation}
 const usable=()=>current()&&!!root.current?.open&&!!root.current.getClientRects().length
 const transport=useQuery({queryKey:['local-permission-transport',identity],enabled:isTauri()&&open,retry:false,refetchOnWindowFocus:false,refetchOnReconnect:false,
  queryFn:async()=>{const value=await invoke<string>('local_permissions_transport');if(!current()||!['native','unavailable'].includes(value))throw new Error('Permission transport unavailable');return value}})
 const read=async()=>{
  if(!usable()||transport.data!=='native'||lock.current)return
  lock.current=true;setPending('read');setFailure(null)
  try{
   const value=await invoke<unknown>('read_local_permissions')
   if(!current())return
   if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length!==2)throw new Error('Permission status unavailable')
   const fields=value as Record<string,unknown>
   if(!permissions.has(fields.screen_recording)||!permissions.has(fields.accessibility))throw new Error('Permission status unavailable')
   const next:Observation={receivedAt:new Date().toISOString(),screen_recording:fields.screen_recording as Permission,accessibility:fields.accessibility as Permission}
   if(writeDirectoryView(identity,'home:local-permissions',next,generation)){setRecord(next);setRequested(null)}
  }catch{if(current())setFailure('read')}
  finally{lock.current=false;if(current())setPending(null)}
 }
 const settings=async(panel:Panel)=>{
  if(!usable()||transport.data!=='native'||lock.current)return
  lock.current=true;setPending(panel);setFailure(null);setRequested(null)
  try{await invoke('open_local_permission_settings',{panel});if(current())setRequested(panel)}
  catch{if(current())setFailure(panel)}
  finally{lock.current=false;if(current())setPending(null)}
 }
 if(!isTauri())return null
 return <details ref={root} className="surface compact-details" open={open} onToggle={event=>{if(event.target===event.currentTarget&&current())setOpen(event.currentTarget.open)}}>
  <summary>{t('localPermissions.title')}</summary>
  {open&&<section className="stack stack--tight" aria-label={t('localPermissions.title')}>
   <p className="section-copy">{t('localPermissions.help')}</p>
   {transport.isFetching&&<p role="status">{t('common.loading')}</p>}
   {transport.isError&&<p role="alert" className="notice notice--warning">{t('localPermissions.transportFailed')} <button type="button" className="text-link" disabled={transport.isFetching} onClick={()=>{if(usable())void transport.refetch({cancelRefetch:false})}}>{t('common.retry')}</button></p>}
   {transport.data==='unavailable'&&<p className="notice notice--info">{t('localPermissions.unavailable')}</p>}
   {transport.data==='native'&&<>
    <div className="page-actions"><button type="button" className="button button--secondary" disabled={pending!==null} onClick={()=>void read()}>{t(pending==='read'?'localPermissions.reading':'localPermissions.read')}</button></div>
    {!record&&<p role="status">{t('localPermissions.notRead')}</p>}
    {record&&<p className="metadata">{t('localPermissions.asOf',{time:formatDateTime(record.receivedAt,{dateStyle:'medium',timeStyle:'medium'})})}</p>}
    {failure==='read'&&<p role="alert" className="notice notice--warning">{t(record?'localPermissions.readFailedRetained':'localPermissions.readFailed')}</p>}
    <ul className="stack stack--tight">{panels.map(panel=><li key={panel} className="surface stack stack--tight">
     <h3 className="section-title">{t(`localPermissions.${panel}`)}</h3>
     {record&&<StatusText tone={record[panel]==='granted'?'success':record[panel]==='not_granted'?'warning':'neutral'}>{t(`localPermissions.${record[panel]}`)}</StatusText>}
     <p className="field-hint">{t(`localPermissions.${panel}Help`)}</p>
     <div><button type="button" className="button button--secondary button--compact" disabled={pending!==null} onClick={()=>void settings(panel)}>{pending===panel?t('common.loading'):t('localPermissions.openSettings',{name:t(`localPermissions.${panel}`)})}</button></div>
     {requested===panel&&<p role="status" className="notice notice--info">{t('localPermissions.settingsRequested')}</p>}
     {failure===panel&&<p role="alert" className="notice notice--warning">{t('localPermissions.settingsFailed')}</p>}
    </li>)}</ul>
   </>}
   <p className="field-hint">{t('localPermissions.limit')}</p>
  </section>}
 </details>
}
