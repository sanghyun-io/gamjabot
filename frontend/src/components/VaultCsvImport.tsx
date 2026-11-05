import type { MessageKey } from '../i18n/locales/en'
import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { vaultApi, type VaultCsvDraft, type VaultCsvPage, type VaultCsvResult, type VaultImportReceipt, type VaultSession } from '../api/vault'

const hex=/^[0-9a-f]{32}$/
const reasons=new Set(['column_count','unsupported_item_type','archived','invalid_website','invalid_username','invalid_nickname','missing_password','secret_too_large','duplicate'])
const fields=(v:object,names:string[])=>Object.keys(v).sort().join(',')===[...names].sort().join(',')
type Recovery={owner_epoch:string;draft_id:string;row:number;key:string;stage:'reserve'|'ready'|'store'|'skip';item_id?:string;command_id?:string}
type Outcome='stored'|'duplicate'|'rejected'|'unknown'|'ready'
const id=()=>crypto.randomUUID().replaceAll('-','')
function validDraft(x:VaultCsvDraft):boolean {
 return !!x && fields(x,['version','draft_id','total','invalid_count','expires_in_seconds']) && x.version===1 && hex.test(x.draft_id) && Number.isSafeInteger(x.total) && x.total>=0 && Number.isSafeInteger(x.invalid_count) && x.invalid_count>=0 && x.total+x.invalid_count<=5000 && Number.isSafeInteger(x.expires_in_seconds) && x.expires_in_seconds>0 && x.expires_in_seconds<=3600
}
function validPage(x:VaultCsvPage,offset:number):boolean {
 if(!x || !fields(x,['version','rows','errors','next_offset']) || x.version!==1 || !Array.isArray(x.rows) || !Array.isArray(x.errors) || x.rows.length+x.errors.length>50 || !(x.next_offset===null || Number.isSafeInteger(x.next_offset) && x.next_offset>offset && x.next_offset<=5000))return false
 const nums:number[]=[]
 for(const row of x.rows){if(!row || !fields(row,['row','website','username','nickname','hint','state']) || !Number.isSafeInteger(row.row) || row.row<2 || row.row>5001 || !['pending','stored','unknown','rejected'].includes(row.state) || ![row.website,row.username,row.nickname,row.hint].every(v=>typeof v==='string') || row.website.length>2048 || row.username.length>512 || row.nickname.length>200 || row.hint.length>2000)return false;nums.push(row.row)}
 for(const e of x.errors){if(!e || !fields(e,['row','reason']) || !Number.isSafeInteger(e.row) || e.row<2 || e.row>5001 || !reasons.has(e.reason))return false;nums.push(e.row)}
 return new Set(nums).size===nums.length
}
function validReceipt(r:VaultImportReceipt,key:string,draftId:string,row:number):boolean {
 return !!r && fields(r,['version','operation','command_key','state','item_id','revision','draft_id','row']) && r.version===1 && r.operation==='import_reserve' && r.command_key===key && r.draft_id===draftId && r.row===row && ['created','duplicate'].includes(r.state) && hex.test(r.item_id) && Number.isSafeInteger(r.revision) && r.revision>0 && (r.state!=='created' || r.revision===1)
}
function validResult(r:VaultCsvResult):boolean {
 return !!r && fields(r,['version','status','revision','command_id']) && r.version===1 && ['stored','unknown','rejected','unavailable'].includes(r.status) && (r.revision===null || Number.isSafeInteger(r.revision) && r.revision>=0) && (r.command_id===null || hex.test(r.command_id))
}

const csvReasonKeys: Record<string, MessageKey> = {
  column_count: 'vault.csvReason.column_count', unsupported_item_type: 'vault.csvReason.unsupported_item_type', archived: 'vault.csvReason.archived',
  invalid_website: 'vault.csvReason.invalid_website', invalid_username: 'vault.csvReason.invalid_username', invalid_nickname: 'vault.csvReason.invalid_nickname',
  missing_password: 'vault.csvReason.missing_password', secret_too_large: 'vault.csvReason.secret_too_large', duplicate: 'vault.csvReason.duplicate',
}

export default function VaultCsvImport({session,onChanged}:{session:VaultSession;onChanged:()=>void}) {
 const {t}=useI18n(), authEpoch=useAuthStore(s=>s.epoch),owner=useAuthStore(s=>s.user?.id)
 const [draft,setDraft]=useState<VaultCsvDraft|null>(null),[page,setPage]=useState<VaultCsvPage|null>(null),[outcomes,setOutcomes]=useState<Record<number,Outcome>>({})
 const [recovery,setRecovery]=useState<Recovery|null>(null),[notice,setNotice]=useState<'csvCancelled'|'csvUnavailable'|'csvUnknown'|'csvExpired'|'csvReserved'|'csvSkipPending'|'csvStopped'|'csvHistoryFull'|null>(null),[busy,setBusy]=useState(false)
 const [batch,setBatch]=useState<{created:number;duplicate:number;stored:number;invalid:number;rejected:number}|null>(null),[batchRunning,setBatchRunning]=useState(false),[batchDone,setBatchDone]=useState(false)
 const [reviewedAll,setReviewedAll]=useState(false)
 const [history,setHistory]=useState<Recovery[]>([]),[historyNotice,setHistoryNotice]=useState<Record<string,'saved'|'reserved'|'duplicate'|'unknown'>>({})
 const stopBatch=useRef(false),batchActive=useRef(false)
 const lock=useRef(false),generation=useRef(0),storage=`vault.csv.pending.${session.owner_id}`
 const sessionCurrent=()=>useAuthStore.getState().epoch===authEpoch && useAuthStore.getState().user?.id===owner && owner===session.owner_id
 const current=(g:number)=>g===generation.current && useAuthStore.getState().epoch===authEpoch && useAuthStore.getState().user?.id===owner && owner===session.owner_id
 const saveRecovery=(r:Recovery)=>{try{sessionStorage.setItem(storage,JSON.stringify(r));setRecovery(r);return true}catch{return false}}
 const historyStorage=`vault.csv.history.${session.owner_id}`
 const saveHistory=(items:Recovery[])=>{try{sessionStorage.setItem(historyStorage,JSON.stringify(items));setHistory(items);return true}catch{return false}}
 const clearRecovery=()=>{sessionStorage.removeItem(storage);setRecovery(null)}
 const ctx=()=>({ownerId:session.owner_id,ownerEpoch:session.owner_epoch!})
 useEffect(()=>{generation.current++;setDraft(null);setPage(null);setOutcomes({});setRecovery(null);setBatch(null);setBatchRunning(false);setBatchDone(false);setReviewedAll(false);stopBatch.current=true;setNotice(null)
  try{const saved=JSON.parse(sessionStorage.getItem(historyStorage) || '[]');if(!Array.isArray(saved) || saved.length>20 || !saved.every((r:Recovery)=>r && fields(r,r.stage==='store'?['owner_epoch','draft_id','row','key','stage','item_id','command_id']:['owner_epoch','draft_id','row','key','stage']) && ['reserve','store'].includes(r.stage) && /^[0-9a-f]{64}$/.test(r.owner_epoch) && hex.test(r.draft_id) && hex.test(r.key) && Number.isSafeInteger(r.row) && r.row>=2 && r.row<=5001 && (r.stage!=='store' || !!r.item_id && hex.test(r.item_id) && r.command_id===r.key)))throw Error('invalid');setHistory(saved);setHistoryNotice({});const raw=sessionStorage.getItem(storage);if(raw){const r=JSON.parse(raw) as Recovery;if(r.owner_epoch===session.owner_epoch && hex.test(r.draft_id) && hex.test(r.key) && Number.isSafeInteger(r.row) && r.row>=2 && r.row<=5001 && ['reserve','ready','store','skip'].includes(r.stage) && (['reserve','skip'].includes(r.stage) || !!r.item_id && hex.test(r.item_id)) && (r.stage!=='store' || r.command_id===r.key)){setRecovery(r);setNotice(r.stage==='ready'?'csvReserved':r.stage==='skip'?'csvSkipPending':'csvUnknown')}}}catch{setNotice('csvUnavailable')}
  return()=>{generation.current++}
 },[session.owner_id,session.owner_epoch,authEpoch,storage,historyStorage])
 const choose=async()=>{
  if(lock.current || recovery || !sessionCurrent() || !session.owner_epoch || !session.identity_attested || owner!==session.owner_id || !vaultApi.nativeAvailable())return
  lock.current=true;setBusy(true);const g=generation.current
  try{const d=await vaultApi.csvChoose(ctx());if(!current(g))return;if(!validDraft(d))throw Error('invalid');const p=await vaultApi.csvPage({...ctx(),draftId:d.draft_id,offset:0,limit:50});if(!current(g))return;if(!validPage(p,0))throw Error('invalid');setDraft(d);setPage(p);setOutcomes({});setBatch(null);setBatchDone(false);setReviewedAll(p.next_offset===null);setNotice(null)}
  catch(e){if(current(g))setNotice(['cancelled','vault_cancelled'].includes(String(e))?'csvCancelled':'csvUnavailable')}
  finally{lock.current=false;if(current(g))setBusy(false)}
 }
 const loadNext=async()=>{
  if(lock.current || !sessionCurrent() || !draft || !page || page.next_offset===null || !session.owner_epoch)return
  const offset=page.next_offset,g=generation.current;lock.current=true;setBusy(true)
  try{const p=await vaultApi.csvPage({...ctx(),draftId:draft.draft_id,offset,limit:50});if(!current(g))return;if(!validPage(p,offset))throw Error('invalid');setPage(p);if(p.next_offset===null)setReviewedAll(true)}catch{if(current(g))setNotice('csvExpired')}finally{lock.current=false;if(current(g))setBusy(false)}
 }
 const finish=(r:Recovery,outcome:Outcome):Outcome=>{setOutcomes(old=>({...old,[r.row]:outcome}));if(outcome!=='unknown' && outcome!=='ready'){clearRecovery();onChanged()}else setNotice(outcome==='ready'?'csvReserved':'csvUnknown');return outcome}
 const checkStored=async(r:Recovery,res:VaultCsvResult,g:number)=>{
  if(!validResult(res) || !current(g))return 'unknown' as const
  if(res.status==='rejected' && res.command_id===r.command_id)return 'rejected' as const
  if(res.status!=='stored' || res.revision!==1 || res.command_id!==r.command_id || !r.item_id)return 'unknown' as const
  const item=await vaultApi.read(r.item_id);if(!current(g))return 'unknown' as const
  return item.secret_status.status==='present' && item.secret_status.revision===1 && item.secret_status.command_id===r.command_id?'stored' as const:'unknown' as const
 }
 const store=async(r:Recovery,g:number):Promise<Outcome>=>{
  if(!r.item_id || !session.owner_epoch || !sessionCurrent())return 'ready'
  const proof=await vaultApi.importReceipt(r.key);if(!current(g))return 'unknown';if(!validReceipt(proof,r.key,r.draft_id,r.row) || proof.state!=='created' || proof.item_id!==r.item_id){setNotice('csvUnknown');return 'ready'}
  const binding=await vaultApi.importBind(r.key);if(!current(g))return 'unknown';if(!binding || !fields(binding,['version','operation','command_key','native_ready']) || binding.version!==1 || binding.operation!=='import_bind' || binding.command_key!==r.key || binding.native_ready!==true){setNotice('csvReserved');return 'ready'}
  if(stopBatch.current && batchActive.current){setNotice('csvReserved');return 'ready'}
  const next:Recovery={...r,stage:'store',command_id:r.key};if(!saveRecovery(next)){setNotice('csvUnavailable');return 'ready'}
  try{const result=await vaultApi.csvStore({...ctx(),draftId:next.draft_id,row:next.row,itemId:next.item_id!,commandId:next.command_id!});if(!current(g))return 'unknown';return finish(next,await checkStored(next,result,g))}
  catch{if(current(g))return finish(next,'unknown');return 'unknown'}
 }
 const skip=async(r:Recovery,g:number):Promise<Outcome>=>{
  try{const result=await vaultApi.csvSkip({...ctx(),draftId:r.draft_id,row:r.row});if(!current(g))return 'unknown'
   if(!validResult(result) || result.status!=='rejected' || result.revision!==null || result.command_id!==null){setNotice('csvSkipPending');return 'ready'}
   return finish(r,'duplicate')
  }catch{if(current(g))setNotice('csvSkipPending');return 'ready'}
 }
 const beginSkip=async(r:Recovery,g:number):Promise<Outcome>=>{
  const next:Recovery={...r,stage:'skip'};if(!saveRecovery(next)){setNotice('csvUnknown');return 'unknown'}return await skip(next,g)
 }
 const retrySkip=async()=>{if(lock.current || !recovery || !sessionCurrent() || !session.owner_epoch || recovery.stage!=='skip')return;const g=generation.current;lock.current=true;setBusy(true);try{await skip(recovery,g)}finally{lock.current=false;if(current(g))setBusy(false)}}
 const processRow=async(row:VaultCsvPage['rows'][number],draftId:string,g:number):Promise<{outcome:Outcome;created:boolean}>=>{
  if(!session.owner_epoch || !current(g))return {outcome:'unknown',created:false}
  const r:Recovery={owner_epoch:session.owner_epoch,draft_id:draftId,row:row.row,key:id(),stage:'reserve'}
  if(!saveRecovery(r)){setNotice('csvUnavailable');return {outcome:'unknown',created:false}}
  let created=false
  try{const receipt=await vaultApi.importReserve({website:row.website,username:row.username,nickname:row.nickname,hint:row.hint},r.key,r.draft_id,r.row);if(!current(g))return {outcome:'unknown',created:false};if(!validReceipt(receipt,r.key,r.draft_id,r.row))return {outcome:finish(r,'unknown'),created:false}
   if(receipt.state==='duplicate')return {outcome:await beginSkip(r,g),created:false}
   created=true;const ready:Recovery={...r,stage:'ready',item_id:receipt.item_id};if(!saveRecovery(ready)){setNotice('csvUnknown');return {outcome:'unknown',created:true}}onChanged()
   if(stopBatch.current && batchActive.current){setNotice('csvReserved');return {outcome:'ready',created:true}}
   return {outcome:await store(ready,g),created:true}
  }catch(e){if(current(g)){const status=(e as {response?:{status?:number}})?.response?.status;return {outcome:finish(r,status && status>=400 && status<500?'rejected':'unknown'),created}}return {outcome:'unknown',created}}
 }
 const importRow=async(row:VaultCsvPage['rows'][number])=>{
  if(lock.current || recovery || !sessionCurrent() || !draft || !session.owner_epoch || row.state!=='pending')return
  const g=generation.current;stopBatch.current=false;lock.current=true;setBusy(true);setNotice(null)
  try{await processRow(row,draft.draft_id,g)}finally{lock.current=false;if(current(g))setBusy(false)}
 }
 const importBatch=async()=>{
  if(lock.current || recovery || !sessionCurrent() || !draft || !session.owner_epoch || draft.total===0 || !reviewedAll || batchDone)return
  const g=generation.current, counts={created:0,duplicate:0,stored:0,invalid:draft.invalid_count,rejected:0};let offset=0,interrupted=false,unstoredRecovery=false,completed=false
  const seen=new Set<number>();stopBatch.current=false;batchActive.current=true;lock.current=true;setBusy(true);setBatchRunning(true);setBatch({...counts});setNotice(null)
  try{while(current(g) && !stopBatch.current){
    const next=await vaultApi.csvPage({...ctx(),draftId:draft.draft_id,offset,limit:50});if(!current(g))return;if(!validPage(next,offset))throw Error('invalid');setPage(next)
    for(const row of next.rows){
     if(!current(g) || stopBatch.current)break
     if(seen.has(row.row)){interrupted=true;setNotice('csvUnavailable');break}seen.add(row.row)
     if(row.state==='unknown'){interrupted=true;setNotice('csvUnknown');break}
     if(row.state!=='pending' || outcomes[row.row])continue
     const result=await processRow(row,draft.draft_id,g);if(!current(g))return
     if(result.created)counts.created++;if(result.outcome==='stored')counts.stored++;if(result.outcome==='duplicate')counts.duplicate++;if(result.outcome==='rejected')counts.rejected++;setBatch({...counts})
     if(result.outcome==='unknown' || result.outcome==='ready'){if(stopBatch.current && result.outcome==='ready')unstoredRecovery=true;else interrupted=true;break}
    }
    if(interrupted || stopBatch.current)break
    if(next.next_offset===null){completed=true;break}
    offset=next.next_offset
   }
   if(current(g) && completed && !interrupted && !stopBatch.current)setBatchDone(true)
   if(current(g) && stopBatch.current && !interrupted){const result=await vaultApi.csvCancel({...ctx(),draftId:draft.draft_id});if(!current(g))return;if(result.version!==1 || result.status!=='cancelled')throw Error('invalid');setDraft(null);setPage(null);if(unstoredRecovery){clearRecovery();onChanged()}setNotice('csvStopped')}
  }catch{if(current(g))setNotice('csvExpired')}
  finally{lock.current=false;batchActive.current=false;if(current(g)){setBusy(false);setBatchRunning(false)}}
 }
 const check=async()=>{
  if(lock.current || !recovery || !sessionCurrent() || !session.owner_epoch)return
  const g=generation.current,r=recovery;lock.current=true;setBusy(true)
  try{if(r.stage==='reserve'){const receipt=await vaultApi.importReceipt(r.key);if(!current(g))return;if(!validReceipt(receipt,r.key,r.draft_id,r.row)){finish(r,'unknown');return}if(receipt.state==='duplicate'){await beginSkip(r,g);return}const ready:Recovery={...r,stage:'ready',item_id:receipt.item_id};if(!saveRecovery(ready)){setNotice('csvUnknown');return}finish(ready,'ready')}
   else if(r.stage==='store' && r.item_id && r.command_id){const item=await vaultApi.read(r.item_id);if(!current(g))return;if(item.secret_status.status==='present' && item.secret_status.revision===1 && item.secret_status.command_id===r.command_id){finish(r,'stored');return}const result=await vaultApi.csvReconcile({...ctx(),draftId:r.draft_id,row:r.row,itemId:r.item_id,commandId:r.command_id});if(current(g))finish(r,await checkStored(r,result,g))}
  }catch{if(current(g))setNotice('csvUnknown')}finally{lock.current=false;if(current(g))setBusy(false)}
 }
 const resume=async()=>{if(lock.current || !recovery || !sessionCurrent() || recovery.stage!=='ready')return;const g=generation.current;lock.current=true;setBusy(true);try{await store(recovery,g)}catch{if(current(g))setNotice('csvUnknown')}finally{lock.current=false;if(current(g))setBusy(false)}}
 const cancel=async()=>{const draftId=draft?.draft_id ?? recovery?.draft_id;if(lock.current || !sessionCurrent() || !draftId || !session.owner_epoch)return;const g=generation.current;lock.current=true;setBusy(true);try{const r=await vaultApi.csvCancel({...ctx(),draftId});if(!current(g))return;if(r.version!==1 || r.status!=='cancelled')throw Error('invalid');setDraft(null);setPage(null);if(recovery?.stage==='reserve' || recovery?.stage==='store'){if(!history.some(x=>x.key===recovery.key) && (history.length>=20 || !saveHistory([...history,recovery]))){setNotice('csvHistoryFull');return}clearRecovery();setNotice(null)}else if(recovery){clearRecovery();onChanged();setNotice('csvExpired')}else setNotice(null)}catch{if(current(g))setNotice('csvUnavailable')}finally{lock.current=false;if(current(g))setBusy(false)}}
 const checkHistory=async(r:Recovery)=>{
  if(lock.current || !sessionCurrent())return;const g=generation.current;lock.current=true;setBusy(true)
  try{let value:'saved'|'reserved'|'duplicate'|'unknown'='unknown'
   if(r.stage==='reserve'){const proof=await vaultApi.importReceipt(r.key);if(!current(g))return;if(validReceipt(proof,r.key,r.draft_id,r.row))value=proof.state==='created'?'reserved':'duplicate'}
   else if(r.stage==='store' && r.item_id){const item=await vaultApi.read(r.item_id);if(!current(g))return;if(item.secret_status.status==='present' && item.secret_status.revision===1 && item.secret_status.command_id===r.key)value='saved'}
   setHistoryNotice(old=>({...old,[r.key]:value}))
  }catch{if(current(g))setHistoryNotice(old=>({...old,[r.key]:'unknown'}))}finally{lock.current=false;if(current(g))setBusy(false)}
 }
 return <section className="stack stack--tight"><h2>{t('vault.csvTitle')}</h2><p>{t('vault.csvIntro')}</p>
  {notice && <div role="status" className="notice">{t(`vault.${notice}`)} {recovery && recovery.stage!=='ready' && recovery.stage!=='skip' && <button className="button button--secondary button--compact" type="button" disabled={busy} onClick={()=>void check()}>{t('vault.csvCheck')}</button>}{recovery?.stage==='skip' && <button className="button button--secondary button--compact" type="button" disabled={busy} onClick={()=>void retrySkip()}>{t('vault.csvSkip')}</button>}{recovery?.stage==='ready' && <button className="button button--secondary button--compact" type="button" disabled={busy} onClick={()=>void resume()}>{t('vault.csvContinue')}</button>}</div>}
  {!draft && recovery && <button className="button button--secondary button--compact" type="button" disabled={busy} onClick={()=>void cancel()}>{t('vault.csvCancel')}</button>}
  {!draft && <button className="button button--secondary" type="button" disabled={busy || !!recovery || !session.owner_epoch || !session.identity_attested || !vaultApi.nativeAvailable()} onClick={()=>void choose()}>{t('vault.csvChoose')}</button>}
  {history.length>0 && <section><h3>{t('vault.csvHistory')}</h3>{history.map(r=><p key={r.key}>{t('vault.csvHistoryRow')} {r.row} · {t(`vault.csvHistory.${historyNotice[r.key] || 'unknown'}`)} <button className="button button--secondary button--compact" type="button" disabled={busy} onClick={()=>void checkHistory(r)}>{t('vault.csvCheck')}</button> <button className="button button--secondary button--compact" type="button" disabled={busy} onClick={()=>{if(!saveHistory(history.filter(x=>x.key!==r.key)))setNotice('csvUnavailable')}}>{t('vault.csvHistoryRemove')}</button></p>)}</section>}
  {batchDone && <p role="status">{t('vault.csvBatchDone')}</p>}
  {batch && <p role="status">{t('vault.csvProgress')}: {t('vault.csvCreated')} {batch.created} · {t('vault.csvDuplicates')} {batch.duplicate} · {t('vault.csvStored')} {batch.stored} · {t('vault.csvInvalid')} {batch.invalid} · {t('vault.csvRejectedCount')} {batch.rejected}</p>}
  {draft && page && <><p>{t('vault.csvReview')} ({draft.total}; {t('vault.csvInvalid')}: {draft.invalid_count})</p><p>{draft.total} {t('vault.csvBatchHint')}</p>{!reviewedAll && <p>{t('vault.csvReviewAll')}</p>}<div className="page-actions"><button className="button button--primary" type="button" disabled={busy || !!recovery || !reviewedAll || batchDone || draft.total===0} onClick={()=>void importBatch()}>{draft.total} {t('vault.csvBatch')}</button>{batchRunning && <button className="button button--secondary" type="button" onClick={()=>{stopBatch.current=true}}>{t('vault.csvStop')}</button>}</div><ul className="stack stack--tight">{page.rows.map(row=><li className="stack stack--tight" key={row.row}><strong>{row.website}</strong><span>{row.username}</span>{row.nickname && <span>{row.nickname}</span>}{row.hint && <p>{row.hint}</p>}{(outcomes[row.row] || row.state!=='pending') && <p>{t(`vault.csv${(outcomes[row.row] || row.state)==='stored'?'Stored':outcomes[row.row]==='duplicate'?'Duplicate':outcomes[row.row]==='ready'?'Reserved':(outcomes[row.row] || row.state)==='rejected'?'Rejected':'Unknown'}`)}</p>}<button className="button button--secondary button--compact" type="button" disabled={busy || !!recovery || row.state!=='pending' || !!outcomes[row.row]} onClick={()=>void importRow(row)}>{t('vault.csvImport')}</button></li>)}</ul>
   {page.errors.length>0 && <ul>{page.errors.map(e=><li key={e.row}>{e.row} {t('vault.csvRow')} · {csvReasonKeys[e.reason] ? t(csvReasonKeys[e.reason]) : t('vault.csvInvalid')}</li>)}</ul>}
   {page.next_offset!==null && <button className="button button--secondary button--compact" type="button" disabled={busy || !!recovery} onClick={()=>void loadNext()}>{t('vault.csvMore')}</button>}
   <button className="button button--secondary button--compact" type="button" disabled={busy} onClick={()=>void cancel()}>{t('vault.csvCancel')}</button></>}
 </section>
}
