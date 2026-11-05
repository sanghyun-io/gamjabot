import { useAuthStore } from '../stores/authStore'
import type { MemoryImportReceipt, MemoryImportRequest } from '../api/memoryImport'
export type ImportOperation={key:string;body:MemoryImportRequest;roomId:number;agentName:string;phase:'sending'|'unknown'|'conflict'|'rejected'|'submitted';receipt?:MemoryImportReceipt}
type Workspace={source:string;content:string;operation:ImportOperation|null}
const empty:Workspace=Object.freeze({source:'',content:'',operation:null})
const values=new Map<string,Workspace>(),listeners=new Set<()=>void>()
export const readImportWorkspace=(id:string)=>values.get(id)||empty
export const subscribeImportWorkspace=(fn:()=>void)=>{listeners.add(fn);return()=>listeners.delete(fn)}
export const updateImportWorkspace=(id:string,fn:(value:Workspace)=>Workspace)=>{const a=useAuthStore.getState();if(!a.token||id!==`${a.user?.id||0}:${a.epoch}`)return false;const old=readImportWorkspace(id),next=fn(old);if(old===next)return false;values.set(id,Object.freeze(next));listeners.forEach(f=>f());return true}
useAuthStore.subscribe((a,b)=>{if(!a.token||a.epoch!==b.epoch||a.user?.id!==b.user?.id){values.clear();listeners.forEach(f=>f())}})
