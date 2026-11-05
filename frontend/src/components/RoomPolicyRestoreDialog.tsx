import { useId,useRef } from 'react'
import { createPortal } from 'react-dom'
import { useDialogFocus } from '../lib/useDialogFocus'
import type { DraftSchedule } from '../lib/roomScheduleEditorState'
import { useI18n } from '../i18n'
const fields=[{name:'max_rounds',label:'chat.policy.rounds'},{name:'max_tool_calls',label:'chat.policy.tools'},{name:'max_executor_seconds',label:'chat.policy.executor'}] as const
export type PolicyRestoreTarget=Readonly<{id:string;identity:string;generation:number;roomId:number;revision:number;draft:Readonly<DraftSchedule>;received:Readonly<DraftSchedule>}>
export function samePolicyDraft(left:Readonly<DraftSchedule>|null,right:Readonly<DraftSchedule>|null){return !!left&&!!right&&fields.every(field=>left[field.name]===right[field.name])}
export default function RoomPolicyRestoreDialog({target,roomId,draft,revision,received,ready,busy,unknown,canUse,onClose,onRead,onApply}:{
 target:PolicyRestoreTarget;roomId:number;draft:DraftSchedule|null;revision:number;received:DraftSchedule|null;ready:boolean;busy:boolean;unknown:boolean;canUse:()=>boolean;onClose:()=>void;onRead:()=>void;onApply:()=>void
}){
 const {t}=useI18n(),root=useRef<HTMLDivElement>(null),title=useId(),help=useId()
 useDialogFocus(root,()=>{if(canUse())onClose()})
 const inputSame=revision===target.revision&&samePolicyDraft(draft,target.draft),savedSame=samePolicyDraft(received,target.received)
 const actionable=ready&&!busy&&inputSame&&savedSame
 return createPortal(<div className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack">
  <h2 id={title} className="page-title">{t('chat.policy.compare.title')}</h2>
  <p className="metadata">{t('chat.roomTitle',{id:String(roomId)})}</p>
  <p id={help}>{t('chat.policy.compare.effect')}</p>
  <ul className="stack stack--tight">{fields.map(field=><li key={field.name} className="notice stack stack--tight" style={{minWidth:0,overflowWrap:'anywhere'}}>
   <h3 className="section-title">{t(field.label)} · {t(target.draft[field.name]===target.received[field.name]?'chat.policy.compare.same':'chat.policy.compare.changed')}</h3>
   <dl className="stack stack--tight"><div><dt className="metadata">{t('chat.policy.compare.input')}</dt><dd style={{margin:0,maxHeight:'5em',overflow:'auto'}}><code style={{whiteSpace:'pre-wrap'}}>{JSON.stringify(target.draft[field.name])}</code></dd></div>
    <div><dt className="metadata">{t('chat.policy.compare.received')}</dt><dd style={{margin:0,maxHeight:'5em',overflow:'auto'}}><code style={{whiteSpace:'pre-wrap'}}>{JSON.stringify(target.received[field.name])}</code></dd></div></dl>
  </li>)}</ul>
  <p className="field-hint">{t('chat.policy.compare.literal')}</p>
  <p className="field-hint">{t('chat.policy.compare.observation')}</p>
  {unknown&&<p className="notice notice--warning">{t('chat.policy.compare.unknown')}</p>}
  {!actionable&&<p role="status" className="notice notice--warning">{t(!inputSame?'chat.policy.compare.inputChanged':!savedSame?'chat.policy.compare.savedChanged':'chat.policy.compare.unavailable')}</p>}
  <div className="page-actions"><button type="button" data-dialog-initial className="button button--secondary" onClick={()=>{if(canUse())onClose()}}>{t('common.cancel')}</button><button type="button" className="text-link" disabled={busy} onClick={()=>{if(canUse())onRead()}}>{t('chat.policy.refreshValues')}</button><button type="button" className="button button--primary" disabled={!actionable} onClick={()=>{if(canUse()&&actionable)onApply()}}>{t('chat.policy.compare.apply')}</button></div>
 </div></div>,document.body)
}
