import { invoke, isTauri } from '@tauri-apps/api/core'
export type SecretResult={state:'stored'|'present';credential_ref:string}|{state:'missing'}
export const mcpCredentialsApi={
 available:()=>isTauri(),
 store:async(serverId:string,expectedRevision:number,operationId:string,secret:string):Promise<SecretResult>=>invoke('mcp_store_secret',{serverId,expectedRevision,operationId,secret}),
 status:async(serverId:string,expectedRevision:number,operationId:string):Promise<SecretResult>=>invoke('mcp_secret_status',{serverId,expectedRevision,operationId}),
}
export function validSecretRef(ref:unknown,owner:number,serverId:string,operationId:string):ref is string{return typeof ref==='string' && ref===`mcp:${owner}:${serverId}:${operationId}`}
