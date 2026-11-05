import { useId,useRef } from 'react'
import { createPortal } from 'react-dom'
import type { Conversation } from '../api/conversations'
import type { Project } from '../api/projects'
import { useDialogFocus } from '../lib/useDialogFocus'
import { useI18n } from '../i18n'
export type RoomCloseTarget=Readonly<{roomId:number;ownerId:number;title:string;projectId:number;projectName:string}>
export function roomCloseTarget(room:Conversation|undefined,project:Project|undefined):RoomCloseTarget|null{
 if(!room?.is_active||!project?.is_active||room.project_id!==project.id||room.user_id!==project.user_id||typeof room.title!=='string'||typeof project.name!=='string')return null
 return Object.freeze({roomId:room.id,ownerId:room.user_id,title:room.title,projectId:project.id,projectName:project.name})
}
export function sameRoomCloseTarget(target:RoomCloseTarget,room:Conversation|undefined,project:Project|undefined){
 const value=roomCloseTarget(room,project)
 return !!value&&Object.entries(target).every(([key,field])=>value[key as keyof RoomCloseTarget]===field)
}
export default function RoomCloseDialog({target,room,project,ready,reading,draftCharacters,unconfirmedMessages,canUse,onRead,onClose,onConfirm}:{
 target:RoomCloseTarget;room:Conversation|undefined;project:Project|undefined;ready:boolean;reading:boolean;draftCharacters:number;unconfirmedMessages:number;canUse:()=>boolean;onRead:()=>void;onClose:()=>void;onConfirm:()=>void
}){
 const {t,formatNumber}=useI18n(),root=useRef<HTMLDivElement>(null),title=useId(),help=useId()
 useDialogFocus(root,()=>{if(canUse())onClose()})
 const actionable=ready&&!reading&&sameRoomCloseTarget(target,room,project)
 return createPortal(<div className="modal-backdrop"><div ref={root} role="dialog" aria-modal="true" aria-labelledby={title} aria-describedby={help} className="modal-panel stack">
  <h2 id={title} className="page-title">{t('chat.closeDialog.title')}</h2>
  <div className="notice notice--info stack stack--tight" style={{overflowWrap:'anywhere'}}><strong>{target.title}</strong><p>{target.projectName} · {t('chat.roomTitle',{id:String(target.roomId)})}</p><p className="metadata">{t('chat.closeDialog.projectId',{id:String(target.projectId)})}</p></div>
  <p id={help}>{t('chat.closeDialog.effect')}</p>
  <p className="field-hint">{t('chat.closeDialog.history')}</p>
  <p className="notice notice--warning">{t('chat.closeDialog.external')}</p>
  {(draftCharacters>0||unconfirmedMessages>0)&&<p className="notice notice--warning">{t('chat.closeDialog.unsent',{characters:formatNumber(draftCharacters),requests:formatNumber(unconfirmedMessages)})}</p>}
  <p className="field-hint">{t('chat.closeDialog.stopInstead')}</p>
  <p className="field-hint">{t('chat.closeDialog.received')}</p>
  {!actionable&&<p role="status" className="notice notice--warning">{t('chat.closeDialog.changed')}</p>}
  <div className="page-actions"><button type="button" data-dialog-initial className="button button--secondary" onClick={()=>{if(canUse())onClose()}}>{t('common.cancel')}</button><button type="button" className="text-link" disabled={reading} onClick={()=>{if(canUse())onRead()}}>{t(reading?'common.loading':'chat.closeDialog.read')}</button><button type="button" className="button button--danger" disabled={!actionable} onClick={()=>{if(canUse()&&actionable)onConfirm()}}>{t('chat.close')}</button></div>
 </div></div>,document.body)
}
