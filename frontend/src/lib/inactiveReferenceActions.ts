import type {InfiniteData} from '@tanstack/react-query'
import {inactiveAgentReferencesApi,type InactiveAgentReferencePage} from '../api/agents'
import {useAuthStore} from '../stores/authStore'
import {queryClient} from './queryClient'
import {directoryViewGeneration} from './directoryViewMemory'
import {editorSaveFailure} from '../components/EditorSaveNotice'
import {inactiveReferenceSending,settleInactiveReference,type InactiveReferenceRequest} from './inactiveReferenceWorkspace'
export function inactiveReferenceOwnerCurrent(identity:string,generation:number){const state=useAuthStore.getState();return !!state.token&&directoryViewGeneration()===generation&&`${state.user?.id||0}:${state.epoch}`===identity}
export async function sendInactiveReference(identity:string,generation:number,original:InactiveReferenceRequest){
 if(!inactiveReferenceOwnerCurrent(identity,generation)||!inactiveReferenceSending(identity,original.id))return
 try{
  const result=await inactiveAgentReferencesApi.detach(original.target)
  if(result.id!==original.target.id||result.is_active!==false||result.connection_id!==null||result.model!==original.target.model||result.model_id!==original.target.model_id)throw Error('Detach outcome not confirmed')
  if(!inactiveReferenceOwnerCurrent(identity,generation)||!inactiveReferenceSending(identity,original.id))return
  queryClient.setQueryData<InfiniteData<InactiveAgentReferencePage>>(['inactive-agent-references',identity,original.connection.id],previous=>previous?{...previous,pages:previous.pages.map(page=>({...page,items:page.items.filter(row=>row.id!==original.target.id)}))}:previous)
  settleInactiveReference(identity,generation,original,'received',result)
 }catch(error){if(inactiveReferenceOwnerCurrent(identity,generation))settleInactiveReference(identity,generation,original,editorSaveFailure(error)==='rejected'?'rejected':'unknown')}
}
