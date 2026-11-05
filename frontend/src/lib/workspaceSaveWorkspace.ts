import type { WorkspaceExportSummary } from './workspaceExportSnapshot'
import { directoryViewGeneration } from './directoryViewMemory'
export type WorkspaceSaveReceipt={path:string;bytes:number;replaced:boolean;durability_unconfirmed:boolean;reveal_id?:string|null}
export type WorkspaceSaveCommand=Readonly<{id:string;exportId:string;summary:WorkspaceExportSummary|null;startedAt:string;finishedAt?:string;phase:'choosing'|'saving'|'saved'|'cancelled'|'failed'|'unknown';filename?:string;code?:string;receipt?:WorkspaceSaveReceipt}>
type Workspace=Readonly<{commands:readonly WorkspaceSaveCommand[];pending:string|null}>
const empty:Workspace={commands:[],pending:null},owners=new Map<string,Workspace>(),listeners=new Set<()=>void>();let version=0
const emit=()=>{version++;listeners.forEach(listener=>listener())}
export const workspaceSaveVersion=()=>version
export function subscribeWorkspaceSaves(listener:()=>void){listeners.add(listener);return()=>{listeners.delete(listener)}}
export const readWorkspaceSaves=(identity:string)=>owners.get(identity)||empty
export function clearWorkspaceSaves(){owners.clear();emit()}
export function beginWorkspaceSave(identity:string,generation:number,exportId:string,summary:WorkspaceExportSummary|null){
 const previous=readWorkspaceSaves(identity);if(generation!==directoryViewGeneration()||previous.pending)return null
 const command:WorkspaceSaveCommand={id:crypto.randomUUID(),exportId,summary:summary?JSON.parse(JSON.stringify(summary)):null,startedAt:new Date().toISOString(),phase:'choosing'}
 owners.set(identity,{pending:command.id,commands:[...previous.commands,command]});emit();return command
}
export function settleWorkspaceSave(identity:string,generation:number,id:string,outcome:Pick<WorkspaceSaveCommand,'phase'|'filename'|'receipt'|'code'>){
 const previous=readWorkspaceSaves(identity);if(generation!==directoryViewGeneration()||!previous.commands.some(command=>command.id===id&&['choosing','saving'].includes(command.phase)))return
 owners.set(identity,{pending:outcome.phase==='choosing'||outcome.phase==='saving'?previous.pending:previous.pending===id?null:previous.pending,commands:previous.commands.map(command=>command.id===id?{...command,...outcome,...(!['choosing','saving'].includes(outcome.phase)?{finishedAt:new Date().toISOString()}:{} )}:command)});emit()
}
