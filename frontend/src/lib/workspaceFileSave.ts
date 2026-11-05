import { invoke,isTauri } from '@tauri-apps/api/core'
import { transferOwnerCurrent } from './transferActions'
import { summarizeWorkspaceExport } from './workspaceExportSnapshot'
import { readTransfer } from './transferWorkspace'
import { beginWorkspaceSave,settleWorkspaceSave,type WorkspaceSaveReceipt } from './workspaceSaveWorkspace'
type Destination={selection_id:string;filename:string;replacing:boolean}
const noWriteErrors=new Set(['export_unavailable','export_busy','export_canceled','export_invalid_payload','export_json_required','export_destination_unavailable','export_destination_changed','export_save_failed','export_selection_expired'])
export async function saveReceivedWorkspace(identity:string,generation:number,exportId:string,title:string,viewCurrent:()=>boolean){
 const state=readTransfer(identity),snapshot=state.receivedExport
 if(state.exported?.phase==='pending'||!isTauri()||!transferOwnerCurrent(identity,generation)||!viewCurrent()||snapshot?.id!==exportId||snapshot.phase!=='succeeded'||!snapshot.result)return
 const original=beginWorkspaceSave(identity,generation,exportId,summarizeWorkspaceExport(snapshot));if(!original)return
 const content=JSON.stringify(snapshot.result),bytes=new TextEncoder().encode(content).byteLength
 let selection:Destination|null=null
 const discard=async()=>{if(selection)try{await invoke('discard_workspace_export',{selectionId:selection.selection_id,context:original.id})}catch{/* Unused selection owns no disk output; next choice retires it. */}}
 const valid=()=>transferOwnerCurrent(identity,generation)
 try{
  if(bytes>2*1024*1024)throw 'export_invalid_payload'
  selection=await invoke<Destination|null>('choose_workspace_export',{context:original.id,exportId:original.exportId,title})
  if(!selection){if(valid())settleWorkspaceSave(identity,generation,original.id,{phase:'cancelled'});return}
  if(!/^[a-f0-9]{32}$/.test(selection.selection_id)||typeof selection.filename!=='string'||typeof selection.replacing!=='boolean')throw 'export_transport_unknown'
  // Native panel focus is expected to leave the webview. Recheck the original
  // Owner/generation/view/snapshot after it closes, not document.hasFocus().
  if(!valid()||!viewCurrent()||readTransfer(identity).receivedExport?.id!==original.exportId){await discard();if(valid())settleWorkspaceSave(identity,generation,original.id,{phase:'cancelled'});return}
  settleWorkspaceSave(identity,generation,original.id,{phase:'saving',filename:selection.filename})
  const receipt=await invoke<WorkspaceSaveReceipt>('save_workspace_export',{selectionId:selection.selection_id,context:original.id,exportId:original.exportId,content})
  if(!valid())return
  if(typeof receipt.path!=='string'||!receipt.path||receipt.bytes!==bytes||typeof receipt.replaced!=='boolean'||receipt.replaced!==selection.replacing||typeof receipt.durability_unconfirmed!=='boolean'||receipt.reveal_id!=null&&!/^[a-f0-9]{32}$/.test(receipt.reveal_id))throw 'export_transport_unknown'
  settleWorkspaceSave(identity,generation,original.id,{phase:'saved',filename:selection.filename,receipt})
 }catch(error){
  await discard()
  if(!valid())return
  const code=typeof error==='string'&&noWriteErrors.has(error)?error:'export_transport_unknown'
  settleWorkspaceSave(identity,generation,original.id,{phase:code==='export_canceled'?'cancelled':noWriteErrors.has(code)?'failed':'unknown',filename:selection?.filename,code})
 }
}
