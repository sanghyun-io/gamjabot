import VaultCards from '../components/VaultCards'
import VaultContacts from '../components/VaultContacts'
import VaultCsvImport from '../components/VaultCsvImport'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { vaultApi, type VaultItem, type VaultMetadata, type VaultSession } from '../api/vault'

const empty:VaultMetadata={website:'',username:'',nickname:'',hint:''}
const commandId=()=>crypto.randomUUID().replaceAll('-','')

export default function Vault() {
 const {t}=useI18n(), owner=useAuthStore(state=>state.user?.id), epoch=useAuthStore(state=>state.epoch)
 const [session,setSession]=useState<VaultSession|null>(null), [items,setItems]=useState<VaultItem[]>([]), [cursor,setCursor]=useState<string|null>(null)
 const [editing,setEditing]=useState<VaultItem|null>(null), [form,setForm]=useState<VaultMetadata>(empty), [busy,setBusy]=useState(false)
 const [notice,setNotice]=useState<'unavailable'|'cancelled'|'unknown'|'conflict'|null>(null), [pending,setPending]=useState<string|null>(null)
 const [pendingNative,setPendingNative]=useState<{itemId:string;command:string;operation:'replace'|'delete'}|null>(null)
 const [fillPending,setFillPending]=useState<{itemId:string;command:string}|null>(null)
 const [fillNotice,setFillNotice]=useState<'fillDone'|'fillPasswordOnly'|'fillFailed'|'fillRetry'|'fillCancelled'|'fillUnknown'|null>(null)
 const [fillWindow,setFillWindow]=useState<{itemId:string;command:string}|null>(null)
 const [closeNotice,setCloseNotice]=useState<'fillClosed'|'fillCloseUnknown'|null>(null)
 const [priorFillUnknown,setPriorFillUnknown]=useState(false)
 const generation=useRef(0)
 const stillCurrent=(guard:number)=>guard===generation.current && useAuthStore.getState().epoch===epoch && useAuthStore.getState().user?.id===owner
 const storageKey=owner ? `vault.pending.${owner}` : ''
 const fillStorageKey=owner ? `vault.fill.pending.${owner}` : ''
 const windowStorageKey=owner ? `vault.window.${owner}` : ''
 const priorFillKey=owner ? `vault.fill.previous.${owner}` : ''
 const remember=(record:Record<string,string>)=>{try{if(!session?.owner_epoch || !storageKey)return false;sessionStorage.setItem(storageKey,JSON.stringify({...record,owner_epoch:session.owner_epoch}));return true}catch{return false}}
 const forget=()=>{try{if(storageKey)sessionStorage.removeItem(storageKey)}catch{/* keep recovery conservative */}}

 const refresh=useCallback(async()=>{
  const guard=generation.current
  try {const s=await vaultApi.session();if(!stillCurrent(guard))return;if(s.owner_id!==owner){setNotice('unavailable');return}const page=await vaultApi.list();if(!stillCurrent(guard))return;setSession(s);setItems(page.items);setCursor(page.next_cursor)
   try {const raw=storageKey && sessionStorage.getItem(storageKey);if(raw){const r=JSON.parse(raw) as Record<string,string>;if(r.owner_epoch===s.owner_epoch && /^[0-9a-f]{32}$/.test(r.key || '')){if(r.kind==='metadata')setPending(r.key);else if(r.kind==='native' && /^[0-9a-f]{32}$/.test(r.item_id || '') && (r.operation==='replace'||r.operation==='delete'))setPendingNative({itemId:r.item_id,command:r.key,operation:r.operation});setNotice('unknown')}}}catch{setNotice('unavailable')}
   try {const raw=fillStorageKey && sessionStorage.getItem(fillStorageKey);if(raw){const r=JSON.parse(raw) as Record<string,string>;if(r.owner_epoch===s.owner_epoch && /^[0-9a-f]{32}$/.test(r.item_id || '') && /^[0-9a-f]{32}$/.test(r.command || '')){setFillPending({itemId:r.item_id,command:r.command});setFillNotice('fillUnknown')}}}catch{setFillPending({itemId:'',command:''});setFillNotice('fillUnknown')}
   try {const raw=windowStorageKey && sessionStorage.getItem(windowStorageKey);if(raw){const r=JSON.parse(raw) as Record<string,string>;if(r.owner_epoch===s.owner_epoch && /^[0-9a-f]{32}$/.test(r.item_id || '') && /^[0-9a-f]{32}$/.test(r.command || ''))setFillWindow({itemId:r.item_id,command:r.command})}}catch{setCloseNotice('fillCloseUnknown')}
   try {const raw=priorFillKey && sessionStorage.getItem(priorFillKey);if(raw){const entries=JSON.parse(raw) as Array<{owner_epoch:string}>;if(Array.isArray(entries) && entries.some(e=>e.owner_epoch===s.owner_epoch))setPriorFillUnknown(true)}}catch{setPriorFillUnknown(true)}}
  catch {if(stillCurrent(guard))setNotice('unavailable')}
 },[owner,storageKey,fillStorageKey,windowStorageKey,priorFillKey,epoch])
 useEffect(()=>{generation.current++;setItems([]);setSession(null);setEditing(null);setForm(empty);setPending(null);setPendingNative(null);setFillPending(null);setFillNotice(null);setFillWindow(null);setCloseNotice(null);setPriorFillUnknown(false);setNotice(null);void refresh();return()=>{generation.current++}},[refresh,epoch])
 const save=async()=>{
  if(busy || pending || pendingNative || fillPending || !session || session.owner_id!==owner)return
  const guard=generation.current,key=commandId();if(!remember({kind:'metadata',key})){setNotice('unavailable');return}setPending(key);setBusy(true);setNotice(null)
  try {const r=editing?await vaultApi.update(editing.item_id,form,editing.revision,key):await vaultApi.create(form,key)
   if(!stillCurrent(guard))return
   if(r.version!==1 || r.command_key!==key || !/^[0-9a-f]{32}$/.test(r.item_id) || r.operation!==(editing?'update':'create') || r.revision!==(editing?editing.revision+1:1) || (editing && r.item_id!==editing.item_id))throw Error('invalid')
   forget();setPending(null);setEditing(null);setForm(empty);await refresh()
  } catch(e) {if(stillCurrent(guard)){const status=(e as {response?:{status?:number}})?.response?.status;if(status && status>=400 && status<500){forget();setPending(null);setNotice(status===409?'conflict':'unavailable')}else{setPending(key);setNotice('unknown')}}}
  finally {if(stillCurrent(guard))setBusy(false)}
 }
 const check=async()=>{
  if(!pending || busy)return;const guard=generation.current;setBusy(true)
  try{const r=await vaultApi.receipt(pending);if(!stillCurrent(guard))return;if(r.version!==1 || r.command_key!==pending || !/^[0-9a-f]{32}$/.test(r.item_id) || !['create','update','delete'].includes(r.operation) || !Number.isSafeInteger(r.revision) || r.revision<1)throw Error('invalid');forget();setPending(null);setNotice(null);await refresh()}
  catch{if(stillCurrent(guard))setNotice('unknown')}
  finally{if(stillCurrent(guard))setBusy(false)}
 }
 const native=async(item:VaultItem,operation:'replace'|'delete')=>{
  if(busy || pending || pendingNative || fillPending || !session?.owner_epoch || !session.identity_attested || !vaultApi.nativeAvailable() || session.owner_id!==owner)return
  const guard=generation.current,id=commandId();let dispatched=false;setBusy(true);setNotice(null)
  try{
   const current=await vaultApi.read(item.item_id);if(!stillCurrent(guard))return
   if(current.revision!==item.revision || current.website!==item.website || current.username!==item.username){setNotice('conflict');return}
   if(current.secret_status.status==='unavailable'){setNotice('unavailable');return}
   if(operation==='replace' && current.secret_status.status==='deleted'){setNotice('conflict');return}
   if(operation==='delete' && current.secret_status.status==='deleted'){
    const key=commandId();if(!remember({kind:'metadata',key})){setNotice('unavailable');return}setPending(key);try{await vaultApi.delete(item.item_id,item.revision,key);forget();setPending(null);await refresh()}catch(e){const status=(e as {response?:{status?:number}})?.response?.status;if(status && status>=400 && status<500){forget();setPending(null);setNotice(status===409?'conflict':'unavailable')}else{setNotice('unknown')}}return
   }
   if(!remember({kind:'native',key:id,item_id:item.item_id,operation})){setNotice('unavailable');return}
   setPendingNative({itemId:item.item_id,command:id,operation});dispatched=true
   const result=await vaultApi.native({operation,ownerId:session.owner_id,ownerEpoch:session.owner_epoch,itemId:item.item_id,expectedRevision:current.secret_status.revision ?? 0,commandId:id,...(operation==='replace'?{website:item.website,username:item.username}:{})})
   if(!stillCurrent(guard))return
   if(result.version!==1){setPendingNative({itemId:item.item_id,command:id,operation});setNotice('unknown');return}
   if(result.status==='conflict'){forget();setPendingNative(null);setNotice('conflict');return}
   if(result.command_id!==id){setPendingNative({itemId:item.item_id,command:id,operation});setNotice('unknown');return}
   if(result.status!==(operation==='replace'?'stored':'deleted')){setPendingNative({itemId:item.item_id,command:id,operation});setNotice('unknown');return}
   if(operation==='delete'){
    const fresh=await vaultApi.read(item.item_id);if(!stillCurrent(guard))return
    if(fresh.secret_status.status!=='deleted'){setNotice('unknown');return}
    const key=commandId();if(!remember({kind:'metadata',key})){setNotice('unavailable');return}setPendingNative(null);setPending(key);try{await vaultApi.delete(item.item_id,item.revision,key);forget();setPending(null)}catch(e){const status=(e as {response?:{status?:number}})?.response?.status;if(status && status>=400 && status<500){forget();setPending(null);setNotice(status===409?'conflict':'unavailable')}else{setNotice('unknown')}return}
   } else {forget();setPendingNative(null)}
   await refresh()
  }catch(e){if(stillCurrent(guard)){if(String(e)==='vault_cancelled'){forget();setPendingNative(null);setNotice('cancelled')}else if(dispatched){setPendingNative({itemId:item.item_id,command:id,operation});setNotice('unknown')}else setNotice('unavailable')}}
  finally{if(stillCurrent(guard))setBusy(false)}
 }
 const fill=async(item:VaultItem)=>{
  if(busy || pending || pendingNative || fillPending || !session?.owner_epoch || !session.identity_attested || !vaultApi.nativeAvailable() || session.owner_id!==owner || item.secret_status.status!=='present' || item.secret_status.revision===null)return
  const guard=generation.current,id=commandId();let dispatched=false;setBusy(true);setFillNotice(null)
  try{
   const current=await vaultApi.read(item.item_id);if(!stillCurrent(guard))return
   if(current.revision!==item.revision || current.website!==item.website || current.username!==item.username || current.secret_status.status!=='present' || current.secret_status.revision!==item.secret_status.revision){setNotice('conflict');return}
   try{sessionStorage.setItem(fillStorageKey,JSON.stringify({owner_epoch:session.owner_epoch,item_id:item.item_id,command:id}));if(!fillWindow){sessionStorage.setItem(windowStorageKey,JSON.stringify({owner_epoch:session.owner_epoch,item_id:item.item_id,command:id}));setFillWindow({itemId:item.item_id,command:id})}}catch{setFillNotice('fillFailed');return}
   setFillPending({itemId:item.item_id,command:id});setCloseNotice(null);dispatched=true
   const result=await vaultApi.fill({ownerId:session.owner_id,ownerEpoch:session.owner_epoch,itemId:item.item_id,expectedRevision:current.secret_status.revision,commandId:id,website:item.website,username:item.username})
   if(!stillCurrent(guard))return
   if(!result || result.version!==1 || !['filled','filled_password_only','not_filled','cancelled','unknown','unavailable'].includes(result.status) || result.status==='unknown' || (result.command_id!==null && result.command_id!==id) || (result.revision!==null && result.revision!==current.secret_status.revision) || (['filled','filled_password_only'].includes(result.status) && (result.command_id!==id || result.revision!==current.secret_status.revision))){setFillNotice('fillUnknown');return}
   sessionStorage.removeItem(fillStorageKey);setFillPending(null)
   setFillNotice(result.status==='filled'?'fillDone':result.status==='filled_password_only'?'fillPasswordOnly':result.status==='not_filled'?'fillRetry':result.status==='cancelled'?'fillCancelled':'fillFailed')
  }catch{if(stillCurrent(guard))setFillNotice(dispatched?'fillUnknown':'fillFailed')}
  finally{if(stillCurrent(guard))setBusy(false)}
 }
 const closeBrowser=async()=>{
  if(!fillWindow || !session?.owner_epoch || session.owner_id!==owner || busy)return
  const guard=generation.current;setBusy(true);setCloseNotice(null)
  try{
   if(fillPending){const raw=sessionStorage.getItem(priorFillKey);const entries=raw?JSON.parse(raw):[];if(!Array.isArray(entries))throw Error('recovery_unavailable');if(!entries.some((e:{command?:string;owner_epoch?:string})=>e.command===fillPending.command && e.owner_epoch===session.owner_epoch)){if(entries.length>=20)throw Error('recovery_unavailable');entries.push({owner_epoch:session.owner_epoch,item_id:fillPending.itemId,command:fillPending.command})};sessionStorage.setItem(priorFillKey,JSON.stringify(entries))}
   const result=await vaultApi.closeBrowser({ownerId:session.owner_id,ownerEpoch:session.owner_epoch,itemId:fillWindow.itemId,commandId:fillWindow.command})
   if(!stillCurrent(guard))return
   if(!result || result.version!==1 || result.status!=='closed' || result.revision!==null || (result.command_id!==null && result.command_id!==fillWindow.command)){setCloseNotice('fillCloseUnknown');return}
   sessionStorage.removeItem(windowStorageKey);setFillWindow(null);if(fillPending){sessionStorage.removeItem(fillStorageKey);setFillPending(null);setFillNotice(null);setPriorFillUnknown(true)}setCloseNotice('fillClosed')
  }catch{if(stillCurrent(guard))setCloseNotice('fillCloseUnknown')}
  finally{if(stillCurrent(guard))setBusy(false)}
 }
 const checkNative=async()=>{
  if(!pendingNative || !session?.owner_epoch || busy)return
  const guard=generation.current;setBusy(true)
  try{const result=await vaultApi.native({operation:'status',ownerId:session.owner_id,ownerEpoch:session.owner_epoch,itemId:pendingNative.itemId})
   if(!stillCurrent(guard))return
   if(result.version!==1 || result.command_id!==pendingNative.command || result.status!==(pendingNative.operation==='replace'?'present':'deleted')){setNotice('unknown');return}
   forget();setPendingNative(null);setNotice(null);await refresh()
  }catch{if(stillCurrent(guard))setNotice('unknown')}
  finally{if(stillCurrent(guard))setBusy(false)}
 }
 return <main className="page-frame page-content stack">
  <h1>{t('vault.title')}</h1><p>{t('vault.intro')}</p><p className="field-hint">{t('vault.notYet')}</p><p className="field-hint">{t('vault.fillInfo')}</p>
  {fillNotice && <div role="status" className="notice">{t(`vault.${fillNotice}`)}</div>}
  {closeNotice && <div role="status" className="notice">{t(`vault.${closeNotice}`)}</div>}
  {priorFillUnknown && <div role="status" className="notice">{t('vault.fillPriorUnknown')}</div>}
  {fillWindow && <button className="button button--secondary button--compact" type="button" disabled={busy || !session?.owner_epoch} onClick={()=>void closeBrowser()}>{t('vault.fillClose')}</button>}
  {notice && <div role="alert" className="notice">{t(`vault.${notice}`)} {pending && <button className="button button--secondary button--compact" type="button" onClick={()=>void check()} disabled={busy}>{t('vault.check')}</button>}{pendingNative && <button className="button button--secondary button--compact" type="button" onClick={()=>void checkNative()} disabled={busy}>{t('vault.check')}</button>}</div>}
  {!session ? <>{!notice && <p>{t('vault.loading')}</p>}<button type="button" onClick={()=>void refresh()}>{t('vault.refresh')}</button></> : <>
   <button type="button" onClick={()=>void refresh()} disabled={busy || !!pending || !!pendingNative}>{t('vault.refresh')}</button>
   {items.length===0 && <p>{t('vault.empty')}</p>}
   <ul className="stack stack--tight">{items.map(item=><li className="stack stack--tight" key={item.item_id}>
    <strong>{item.nickname || item.website}</strong> <span>{item.website} · {item.username}</span>
    {item.hint && <p>{item.hint}</p>}<p>{t(`vault.${item.secret_status.status}`)}</p>
    <div className="page-actions"><button className="button button--secondary button--compact" type="button" disabled={busy || !!pending || !!pendingNative || !!fillPending} onClick={()=>{setEditing(item);setForm({website:item.website,username:item.username,nickname:item.nickname,hint:item.hint})}}>{t('vault.edit')}</button>
    <button className="button button--secondary button--compact" type="button" disabled={busy || !!pending || !!pendingNative || !!fillPending || item.secret_status.status!=='present' || item.secret_status.revision===null || !session.identity_attested || !session.owner_epoch || !vaultApi.nativeAvailable()} onClick={()=>void fill(item)}>{t('vault.fill')}</button>
    <button className="button button--secondary button--compact" type="button" disabled={busy || !!pending || !!pendingNative || !!fillPending || item.secret_status.status==='unavailable' || !session.identity_attested || !session.owner_epoch || !vaultApi.nativeAvailable()} onClick={()=>void native(item,'replace')}>{t('vault.replace')}</button>
    <button className="button button--secondary button--compact" type="button" disabled={busy || !!pending || !!pendingNative || !!fillPending || item.secret_status.status==='unavailable' || !session.identity_attested || !session.owner_epoch || !vaultApi.nativeAvailable()} onClick={()=>void native(item,'delete')}>{t('vault.remove')}</button></div>
   </li>)}</ul>
   {cursor && <button type="button" disabled={busy} onClick={()=>{const guard=generation.current;setBusy(true);void vaultApi.list(cursor).then(page=>{if(stillCurrent(guard)){setItems(old=>[...old,...page.items]);setCursor(page.next_cursor)}}).catch(()=>{if(stillCurrent(guard))setNotice('unavailable')}).finally(()=>{if(stillCurrent(guard))setBusy(false)})}}>{t('vault.more')}</button>}
   <VaultCsvImport session={session} onChanged={()=>void refresh()} />
   <VaultContacts session={session} />
   <VaultCards session={session} />
   <h2>{editing?t('vault.edit'):t('vault.new')}</h2>
   {(['website','username','nickname','hint'] as const).map(field=><label key={field}>{t(`vault.${field}`)}<input value={form[field]} onChange={event=>setForm(old=>({...old,[field]:event.target.value}))} autoComplete="off" /></label>)}
   <button type="button" disabled={busy || !!pending || !!pendingNative || !!fillPending || !form.website || !form.username} onClick={()=>void save()}>{t('vault.save')}</button>
  </>}
 </main>
}
