import { invoke, isTauri } from '@tauri-apps/api/core'
export type OAuthState='preparing'|'authorizing'|'authorized'|'failed'|'cancelled'|'expired'
export type OAuthResult={version:1;provider:'notion';server_id:string;config_revision:number;operation_id:string;state:OAuthState;credential_ref?:string;workspace?:{id:string;name:string|null}}
const object=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v)
export function validOAuthResult(value:unknown,owner:number,server:string,revision:number,operation:string):value is OAuthResult {
 if(!object(value)||Object.keys(value).some(k=>!['version','provider','server_id','config_revision','operation_id','state','credential_ref','workspace'].includes(k))||value.version!==1||value.provider!=='notion'||value.server_id!==server||value.config_revision!==revision||value.operation_id!==operation||!['preparing','authorizing','authorized','failed','cancelled','expired'].includes(String(value.state)))return false
 if(value.state==='authorized') {if(value.credential_ref!==`mcp-oauth:${owner}:${server}:${operation}`)return false;if(value.workspace!==undefined&&(!object(value.workspace)||Object.keys(value.workspace).some(k=>!['id','name'].includes(k))||typeof value.workspace.id!=='string'||!value.workspace.id||value.workspace.id.length>256||!(value.workspace.name===null||typeof value.workspace.name==='string'&&value.workspace.name.length<=256)))return false}
 else if(value.credential_ref!==undefined||value.workspace!==undefined)return false
 return true
}
const args=(serverId:string,expectedRevision:number,operationId:string)=>({serverId,expectedRevision,operationId})
export const mcpOAuthApi={available:()=>isTauri(),begin:(server:string,revision:number,operation:string)=>invoke<unknown>('mcp_oauth_begin',args(server,revision,operation)),status:(server:string,revision:number,operation:string)=>invoke<unknown>('mcp_oauth_status',args(server,revision,operation)),cancel:(server:string,revision:number,operation:string)=>invoke<unknown>('mcp_oauth_cancel',args(server,revision,operation)),open:(server:string,revision:number,operation:string)=>invoke<void>('mcp_oauth_open',args(server,revision,operation))}
