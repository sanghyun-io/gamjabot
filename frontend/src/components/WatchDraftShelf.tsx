import { useState } from 'react'
import { useI18n } from '../i18n'
import { watchDraftKey,type WatchForm } from '../lib/roomPlanEditorState'
export default function WatchDraftShelf({drafts,busy,canUse,onSelect}:{drafts:readonly WatchForm[];busy:boolean;canUse:()=>boolean;onSelect:(key:string)=>void}){
 const {t}=useI18n(),[open,setOpen]=useState(false)
 if(!drafts.length)return null
 return <details className="compact-details" open={open} onToggle={event=>{if(event.target===event.currentTarget&&canUse())setOpen(event.currentTarget.open)}}>
  <summary>{t('roomPlans.watchDrafts.title',{count:drafts.length})}</summary>
  {open&&<div className="stack stack--tight">
   <p className="field-hint">{t('roomPlans.watchDrafts.help')}</p>
   <ul className="stack stack--tight" style={{maxHeight:'min(40dvh,300px)',overflow:'auto'}}>{drafts.map(form=><li key={watchDraftKey(form)} className="notice stack stack--tight" style={{overflowWrap:'anywhere'}}>
    <strong>{form.name||t('roomPlans.watchDrafts.unnamed')}</strong><p>{form.id?t('roomPlans.watchDrafts.existing'):t('roomPlans.watchDrafts.new')}</p>
    <p className="technical">{form.url||t('roomPlans.watchDrafts.noUrl')}</p>
    <p className="metadata">{form.interval_minutes} {t('roomPlans.minutes')} · {form.timezone}</p>
    <div><button type="button" className="button button--secondary button--compact" disabled={busy} onClick={()=>{if(canUse())onSelect(watchDraftKey(form))}}>{t('roomPlans.watchDrafts.restore')}</button></div>
   </li>)}</ul>
   <p className="field-hint">{t('roomPlans.watchDrafts.limit')}</p>
  </div>}
 </details>
}
