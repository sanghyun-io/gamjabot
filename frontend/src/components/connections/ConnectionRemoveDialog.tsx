import {agentConnectionHref} from '../../lib/agentConnectionFilter'
import { useId, useRef } from 'react'
import { Link } from 'react-router-dom'
import type { Connection } from '../../api/connections'
import { useI18n } from '../../i18n'
import { useDialogFocus } from '../../lib/useDialogFocus'

export type ConnectionRemovalTarget=Pick<Connection,'id'|'owner_id'|'label'|'provider_kind'|'provider_id'|'protocol_id'|'default_model_id'|'enabled'|'auth_required'|'has_secret'|'auth_method_id'|'config_revision'|'auth_grant_revision'>
export type ConnectionRemovalFailure='in_use'|'rejected'|'unknown'|'not_sent'
export function connectionRemovalTarget(row:Connection):ConnectionRemovalTarget{
 const {id,owner_id,label,provider_kind,provider_id,protocol_id,default_model_id,enabled,auth_required,has_secret,auth_method_id,config_revision,auth_grant_revision}=row
 return Object.freeze({id,owner_id,label,provider_kind,provider_id,protocol_id,default_model_id,enabled,auth_required,has_secret,auth_method_id,config_revision,auth_grant_revision})
}
export function sameConnectionRemovalTarget(target:ConnectionRemovalTarget,current:Connection|undefined){
 return !!current&&Object.entries(target).every(([key,value])=>current[key as keyof ConnectionRemovalTarget]===value)
}
export default function ConnectionRemoveDialog({target,current,ready,blocked,pending,reading,readFailed,failure,canAdopt,canUse,onClose,onRead,onAdopt,onConfirm,onInactive}:{
 target:ConnectionRemovalTarget;current:Connection|undefined;ready:boolean;blocked:boolean;pending:boolean;reading:boolean;readFailed:boolean;failure:ConnectionRemovalFailure|null;canAdopt:boolean;canUse:()=>boolean;onClose:()=>void;onRead:()=>void;onAdopt:()=>void;onConfirm:()=>void;onInactive?:()=>void
}){
 const {t}=useI18n(),root=useRef<HTMLDivElement>(null),title=useId(),help=useId()
 const close=()=>{if(!pending&&canUse())onClose()}
 useDialogFocus(root,close)
 const unchanged=sameConnectionRemovalTarget(target,current)
 return <div className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack" style={{overflowWrap:'anywhere'}}>
  <h2 id={title} className="page-title">{t('connections.removeDialog.title')} · {target.label} · #{target.id}</h2>
  <div className="notice notice--info stack stack--tight"><p>{target.provider_id||target.provider_kind}{target.protocol_id&&` · ${target.protocol_id}`}</p><p>{t('connections.defaultModel')} · {target.default_model_id||t('connections.removeDialog.noModel')}</p><p>{t(target.enabled?'connections.enabled':'connections.disabled')} · {t(target.has_secret?'connections.keySaved':target.auth_required?'connections.keyMissing':'connections.keyNotRequired')}</p></div>
  <p id={help}>{t('connections.deleteConfirm',{name:target.label})}</p><p className="field-hint">{t('connections.removeDialog.effect')}</p>
  {!ready||!current?<p role="status" className="notice notice--warning">{t('connections.removeDialog.unavailable')}</p>:!unchanged&&<div role="status" className="notice notice--warning"><p>{t('connections.removeDialog.changed')}</p><p>{current.label} · #{current.id} · {current.provider_id||current.provider_kind} · {current.default_model_id||t('connections.removeDialog.noModel')}</p></div>}
  {blocked&&<p role="status" className="field-hint">{t('connections.removeDialog.busy')}</p>}
  {failure&&<div role="alert" className="notice notice--warning stack stack--tight"><p>{t(`connections.removeDialog.${failure}`)}</p>{failure==='in_use'&&<Link className="text-link" to={agentConnectionHref(target.id)} onClick={event=>{if(pending||!canUse()){event.preventDefault();return}onClose()}}>{t('connections.removeDialog.agents')}</Link>}{failure==='in_use'&&onInactive&&<button type="button" className="text-link" disabled={pending} onClick={()=>{if(!pending&&canUse())onInactive()}}>{t('inactiveRefs.title')}</button>}</div>}
  {readFailed&&<p role="alert">{t('connections.loadFailure')}</p>}
  {pending&&<p role="status">{t('connections.deleting')}</p>}
  <div className="page-actions">
   <button type="button" data-dialog-initial className="button button--secondary" disabled={pending} onClick={close}>{t('common.cancel')}</button>
   <button type="button" className="button button--secondary" disabled={pending||reading} onClick={()=>{if(canUse())onRead()}}>{t(reading?'common.loading':'connections.removeDialog.read')}</button>
   {canAdopt&&<button type="button" className="button button--secondary" disabled={pending||reading||blocked} onClick={()=>{if(canUse())onAdopt()}}>{t('connections.removeDialog.adopt')}</button>}
   <button type="button" className="button button--danger" disabled={pending||reading||blocked||!ready||!unchanged||!!failure} onClick={()=>{if(canUse()&&ready&&unchanged&&!blocked&&!failure)onConfirm()}}>{t('common.delete')}</button>
  </div>
 </div></div>
}
