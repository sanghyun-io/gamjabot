import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useDialogFocus } from '../lib/useDialogFocus'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { vaultApi } from '../api/vault'
import { formatProfileOrigins, vaultProfileHandoffApi, type HandoffStatus, type CustodyState } from '../api/vaultProfileHandoff'

type RecoveryTarget = Readonly<{handoffId:string;revision:number;ownerEpoch:string;site:string;username:string;account:string;profile:string;identity:string;run:number}>
function sameRecovery(target:RecoveryTarget, value:HandoffStatus|null):boolean {
 const h=value?.handoff
 return !!h&&!!value?.can_recover&&value.revision===target.revision&&h.handoff_id===target.handoffId&&h.owner_epoch===target.ownerEpoch
  &&h.origin===target.site&&h.username===target.username&&h.account_label===target.account&&h.profile_label===target.profile
}
function RecoveryDialog({target,busy,actionable,onClose,onConfirm}:{target:RecoveryTarget;busy:boolean;actionable:boolean;onClose:()=>void;onConfirm:()=>void}) {
 const {t}=useI18n(),root=useRef<HTMLDivElement>(null),title=useId(),help=useId()
 useDialogFocus(root,onClose)
 return createPortal(<div className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack">
  <h2 id={title} className="page-title">{t('vaultHandoff.recover')}</h2>
  <dl className="metadata"><dt>{t('vaultHandoff.site')}</dt><dd>{target.site}</dd><dt>{t('vaultHandoff.account')}</dt><dd>{target.username}{target.account&&target.account!==target.username?` · ${target.account}`:''}</dd><dt>{t('vaultHandoff.profile')}</dt><dd>{target.profile}</dd></dl>
  <p id={help}>{t('vaultHandoff.recoverHelp')}</p><p className="field-hint">{t('vaultHandoff.recoverLimit')}</p>
  {!actionable&&<p role="status" className="notice notice--warning">{t('vaultHandoff.unavailable')}</p>}
  <div className="page-actions"><button type="button" data-dialog-initial className="button button--secondary" disabled={busy} onClick={onClose}>{t('common.cancel')}</button><button type="button" className="button button--primary" disabled={busy||!actionable} onClick={onConfirm}>{t('vaultHandoff.recover')}</button></div>
 </div></div>,document.body)
}

export default function VaultProfileHandoff({roomId,profileId,readOnly=false,onChanged,onCustodyStatus}:{roomId:number;profileId:string;readOnly?:boolean;onChanged?:()=>void;onCustodyStatus?:(value:{revision:number;status:'parked'|'borrowed'|'unknown'}|null)=>void}) {
 const {t}=useI18n()
 const ownerId=useAuthStore(s=>s.user?.id||0), authEpoch=useAuthStore(s=>s.epoch)
 const [data,setData]=useState<HandoffStatus|null>(null),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false)
 const [custody,setCustody]=useState<CustodyState|null>(null)
 const [error,setError]=useState(false),[unknown,setUnknown]=useState(false),[verifiedPark,setVerifiedPark]=useState(false),[recovery,setRecovery]=useState<RecoveryTarget|null>(null)
 const dataRef=useRef(data);dataRef.current=data
 const generation=useRef(0),lock=useRef(false),mounted=useRef(false),readonlyRef=useRef(readOnly)
 readonlyRef.current=readOnly
 const identity=`${ownerId}:${authEpoch}`
 const current=(run:number)=>{const s=useAuthStore.getState();return mounted.current&&generation.current===run&&!!s.token&&`${s.user?.id||0}:${s.epoch}`===identity}
 const storage=(handoffId:string)=>`gamja.vaultProfileHandoff.unknown:${ownerId}:${roomId}:${profileId}:${handoffId}`
 const saveUnknown=(handoffId:string)=>{try{sessionStorage.setItem(storage(handoffId),'1');return true}catch{return false}}
 const clearUnknown=(handoffId:string)=>{try{sessionStorage.removeItem(storage(handoffId))}catch{/* keep conservative until next verified state */}}
 const refresh=async(run:number,signal?:AbortSignal)=>{
  try{
   const next=await vaultProfileHandoffApi.read(roomId,profileId,ownerId,signal)
   if(!current(run))return
   let held:CustodyState|null=null
   if(next.handoff)held=await vaultProfileHandoffApi.custody(roomId,profileId,next.handoff.handoff_id,next.handoff.profile_revision,signal)
   if(!current(run))return
   setCustody(held);setData(next);setError(false);setVerifiedPark(false)
   if(next.handoff){
    const h=next.handoff
    if(h.state!=='prepared'&&!next.can_recover){clearUnknown(h.handoff_id);setUnknown(false)}
    else {try{setUnknown(sessionStorage.getItem(storage(h.handoff_id))==='1')}catch{setUnknown(true)}}
   } else setUnknown(false)
  }catch{if(current(run))setError(true)}finally{if(current(run))setLoading(false)}
 }
 useEffect(()=>{const run=++generation.current;mounted.current=true;setRecovery(null);setData(null);setCustody(null);setError(false);setUnknown(false);setVerifiedPark(false);setLoading(true);const abort=new AbortController();void refresh(run,abort.signal);return()=>{abort.abort();mounted.current=false;generation.current++}},[identity,roomId,profileId])
 useEffect(()=>{if(recovery&&(readOnly||!current(recovery.run)||recovery.identity!==identity||!sameRecovery(recovery,data)))setRecovery(null)},[data,readOnly,identity,recovery])
 const begin=async(kind:'fill'|'park'|'return'|'recover', confirmed?:RecoveryTarget)=>{
  const run=generation.current, sent=data, h=sent?.handoff
  if(lock.current||readonlyRef.current||error||!current(run)||!sent||!h||(kind!=='recover'&&h.state!=='prepared'))return
  if(kind==='fill'&&(!sent.can_fill||unknown||custody?.status==='parked'||custody?.status==='borrowed'||!vaultProfileHandoffApi.available()||!['not_started','not_filled','cancelled'].includes(sent.native_status?.fill_status||'')))return
  if(kind==='park'&&(unknown||custody?.status==='parked'||custody?.status==='borrowed'||sent.native_status?.status!=='active'||!vaultProfileHandoffApi.available()))return
  if(kind==='return'&&!sent.can_return&&!['active','parked'].includes(sent.native_status?.status||'')&&!['parked','borrowed'].includes(custody?.status||''))return
  if(kind==='recover'&&(!confirmed||confirmed.identity!==identity||confirmed.run!==run||!sameRecovery(confirmed,dataRef.current)))return
  if(!saveUnknown(h.handoff_id)){setError(true);return}
  lock.current=true;setBusy(true);setUnknown(true);setError(false);setVerifiedPark(false)
  try{
   const session=await vaultApi.session()
   if(!current(run)||readonlyRef.current||session.owner_id!==ownerId||!session.identity_attested||!session.owner_epoch||kind!=='recover'&&session.owner_epoch!==h.owner_epoch||kind==='recover'&&(!confirmed||!sameRecovery(confirmed,dataRef.current)))throw Error('changed')
   if(kind==='fill'){
    const result=await vaultProfileHandoffApi.native('open_and_fill',ownerId,session.owner_epoch,h.handoff_id,profileId)
    if(!current(run)||readonlyRef.current)return
    if(result.status==='unknown'||result.status==='unavailable'||result.fill_status==='unknown'||result.profile_revision!==h.profile_revision)throw Error('unknown')
    clearUnknown(h.handoff_id);setUnknown(false)
    await refresh(run)
   }else if(kind==='park'){
    const custodyId=await vaultProfileHandoffApi.seal(roomId,profileId,h.handoff_id,sent.revision)
    if(!current(run)||readonlyRef.current)return
    const samePark=()=>{const now=dataRef.current, currentH=now?.handoff;return current(run)&&!readonlyRef.current&&!!now&&!!currentH&&now.profile_id===profileId&&now.revision===sent.revision&&currentH.handoff_id===h.handoff_id&&currentH.owner_id===ownerId&&currentH.owner_epoch===session.owner_epoch&&currentH.room_id===roomId&&currentH.profile_id===profileId&&currentH.profile_revision===h.profile_revision&&currentH.state==='prepared'}
    let nativeFailed=false
    try{
     const result=await vaultProfileHandoffApi.native('park',ownerId,session.owner_epoch,h.handoff_id,profileId)
     if(!samePark())return
     if(result.status!=='parked'||result.profile_revision!==h.profile_revision)throw Error('invalid park result')
    }catch(failure){
     // Only an invocation failure may be reconciled; an invalid reply remains unknown.
     if(failure instanceof Error&&['invalid park result','invalid'].includes(failure.message))throw failure
     nativeFailed=true
    }
    if(!samePark())return
    if(nativeFailed){const before=await vaultApi.session();if(!samePark()||!before.identity_attested||before.owner_id!==ownerId||before.owner_epoch!==session.owner_epoch)throw Error('changed')}
    const held=await vaultProfileHandoffApi.custody(roomId,profileId,h.handoff_id,h.profile_revision)
    if(!samePark())return
    if(held.status!=='parked'||held.custody_id!==custodyId)throw Error('unknown')
    if(nativeFailed){const after=await vaultApi.session();if(!samePark()||!after.identity_attested||after.owner_id!==ownerId||after.owner_epoch!==session.owner_epoch)throw Error('changed')}
    clearUnknown(h.handoff_id);setUnknown(false);setCustody(held);setVerifiedPark(true);await refresh(run);onChanged?.()
   }else if(kind==='return'){
    if(!sent.can_return||custody?.status==='parked'||custody?.status==='borrowed'){
     const result=await vaultProfileHandoffApi.native('close',ownerId,session.owner_epoch,h.handoff_id,profileId)
     if(!current(run)||readonlyRef.current)return
     if(result.status!=='closed'||result.profile_revision!==h.profile_revision)throw Error('unknown')
    }
    if(!sent.can_return||custody?.status==='parked'||custody?.status==='borrowed'){const checked=await vaultProfileHandoffApi.read(roomId,profileId,ownerId);if(!current(run)||checked.handoff?.handoff_id!==h.handoff_id||checked.native_status?.status!=='closed'||!checked.can_return)throw Error('unknown')}
    const result=await vaultProfileHandoffApi.finish(roomId,profileId,ownerId,h.handoff_id,sent.revision)
    if(!current(run))return
    if(result.handoff?.handoff_id!==h.handoff_id||result.handoff.state!=='returned')throw Error('unknown')
    clearUnknown(h.handoff_id);setUnknown(false);setCustody(null);setData(result);onChanged?.()
   }else{
    const result=await vaultProfileHandoffApi.recover(roomId,profileId,ownerId,h.handoff_id,sent.revision)
    if(!current(run))return
    if(result.handoff?.handoff_id!==h.handoff_id||!['returned','recovered'].includes(result.handoff.state)||result.can_recover)throw Error('unknown')
    clearUnknown(h.handoff_id);setUnknown(false);setCustody(null);setData(result);onChanged?.()
   }
  }catch{if(current(run)){setUnknown(true);setError(true)}}finally{lock.current=false;if(current(run))setBusy(false)}
 }
 const statusCallback=useRef(onCustodyStatus);statusCallback.current=onCustodyStatus
 useEffect(()=>{
  const state=!loading&&!busy&&data?.handoff?(unknown||error&&!verifiedPark||custody?.status==='unknown'?'unknown':custody?.status==='parked'||custody?.status==='borrowed'?custody.status:null):null
  statusCallback.current?.(state&&data?{revision:data.revision,status:state}:null)
  return()=>statusCallback.current?.(null)
 },[data,custody,unknown,error,verifiedPark,loading,busy])
 const h=data?.handoff, ns=data?.native_status
 const held=custody?.status==='parked'||custody?.status==='borrowed'
 const uncertain=unknown || custody?.status==='unknown' || custody?.status==='unavailable'&&ns?.status==='parked' || !!h && (!!data?.can_recover && h.state!=='prepared' || h.state==='prepared' && !held && (!ns || ns.status==='unknown' || ns.status==='unavailable' || ns.fill_status==='unknown'))
 if(loading)return <p role="status">{t('common.loading')}</p>
 if(!h&&!error)return null
 return <section className="stack stack--tight" aria-label={t('vaultHandoff.title')}>
  <strong>{t('vaultHandoff.title')}</strong>
  {busy&&<p role="status">{t('vaultHandoff.pending')}</p>}
  {!busy&&error&&!uncertain&&<p role="alert" className="notice notice--warning">{t(verifiedPark?'vaultHandoff.parkRefreshFailed':'vaultHandoff.unavailable')}</p>}
  {!busy&&uncertain&&<p role="alert" className="notice notice--warning">{t('vaultHandoff.unknown')}</p>}
  {h&&<>
   <dl className="metadata"><dt>{t('vaultHandoff.site')}</dt><dd>{h.origin}</dd><dt>{t('vaultHandoff.account')}</dt><dd>{h.username}{h.account_label&&h.account_label!==h.username?` · ${h.account_label}`:''}</dd><dt>{t('vaultHandoff.profile')}</dt><dd>{h.profile_label}</dd></dl>
   {data?.allowed_origins&&<p className="field-hint">{t('vaultHandoff.allowedSites')}: {formatProfileOrigins(data.allowed_origins)?.join(', ')}</p>}
   {!busy&&!uncertain&&<p role="status">{custody?.status==='borrowed'?t('vaultHandoff.borrowed'):custody?.status==='parked'?t('vaultHandoff.parked'):h.state==='returned'?t('vaultHandoff.returned'):h.state==='recovered'?t('vaultHandoff.recovered'):ns?.status==='active'?t('vaultHandoff.active'):t('vaultHandoff.prepared')}</p>}
   {h.state!=='prepared'&&!data?.can_recover&&h.prior_outcome_unknown&&<p className="field-hint">{t('vaultHandoff.recoverLimit')}</p>}
   {ns?.fill_status==='not_filled'&&<p className="field-hint">{t('vaultHandoff.notFilled')}</p>}
   {h.state==='prepared'&&<p className="field-hint">{t(custody?.status==='borrowed'?'vaultHandoff.borrowedHelp':custody?.status==='parked'?'vaultHandoff.parkedHelp':'vaultHandoff.returnHelp')}</p>}
   {!readOnly&&(h.state==='prepared'||data?.can_recover)&&<div className="page-actions">
    {data?.can_fill&&!held&&!uncertain&&!error&&<button type="button" className="button button--primary button--compact" disabled={busy||!vaultProfileHandoffApi.available()||!['not_started','not_filled','cancelled'].includes(ns?.fill_status||'')} onClick={()=>void begin('fill')}>{t('vaultHandoff.fill')}</button>}
    {ns?.status==='active'&&!held&&!uncertain&&<><p className="field-hint">{t('vaultHandoff.parkHelp')}</p><button type="button" className="button button--primary button--compact" disabled={busy||error||!vaultProfileHandoffApi.available()} onClick={()=>void begin('park')}>{t('vaultHandoff.park')}</button></>}
    {(data?.can_return||ns?.status==='active'||ns?.status==='parked'||held)&&<button type="button" className="button button--secondary button--compact" disabled={busy||error||!vaultProfileHandoffApi.available()} onClick={()=>void begin('return')}>{t(held?'vaultHandoff.closeBrowser':'vaultHandoff.return')}</button>}
    {data?.can_recover&&<button type="button" className="button button--secondary button--compact" disabled={busy||error} onClick={()=>{if(!data||!current(generation.current)||readonlyRef.current)return;setRecovery(Object.freeze({handoffId:h.handoff_id,revision:data.revision,ownerEpoch:h.owner_epoch,site:h.origin,username:h.username,account:h.account_label,profile:h.profile_label,identity,run:generation.current}))}}>{t('vaultHandoff.recover')}</button>}
   </div>}
  </>}
  <button type="button" className="button button--secondary button--compact" disabled={busy} onClick={()=>{setLoading(true);void refresh(generation.current)}}>{t('vaultHandoff.check')}</button>
  {recovery&&<RecoveryDialog target={recovery} busy={busy} actionable={!error&&!readOnly&&current(recovery.run)&&recovery.identity===identity&&sameRecovery(recovery,data)} onClose={()=>setRecovery(null)} onConfirm={()=>{const target=recovery;setRecovery(null);void begin('recover',target)}}/>}
 </section>
}
