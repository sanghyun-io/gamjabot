import type { WorkspaceBlueprint } from '../api/workspaceBlueprint'
import type { TransferExport } from './transferWorkspace'
export type WorkspaceExportSummary=Readonly<{
 exportId:string;receivedAt:string|null;version:1|2;compactBytes:number
 counts:{connections:number;agents:number;projects:number;room_templates:number;project_assignments:number;template_members:number}
 connections:readonly {ref:string;name:string;model:string;provider:string;enabled:boolean}[]
 agents:readonly {ref:string;name:string;model:string;connectionRef:string}[]
 projects:readonly {ref:string;name:string;agentRefs:readonly string[];templates:readonly {ref:string;name:string;title:string;agentRefs:readonly string[]}[]}[]
}>
// A received configuration summary, never a provider test or current inventory.
// No endpoints, prompts, credentials or raw JSON are copied into save history.
export function summarizeWorkspaceExport(snapshot:TransferExport):WorkspaceExportSummary|null{
 try{
  const value=snapshot.result
  if(snapshot.phase!=='succeeded'||!value||![1,2].includes(value.version)||!Array.isArray(value.connections)||!Array.isArray(value.agents)||!Array.isArray(value.projects))return null
  const connections=value.connections.map(item=>({ref:item.ref,name:item.label,model:item.default_model_id,provider:'public_config'in item?item.public_config.provider_id:item.provider_kind,enabled:item.enabled}))
  const agents=value.agents.map(item=>({ref:item.ref,name:item.name,model:item.model_id,connectionRef:item.connection_ref}))
  const projects=value.projects.map(item=>({ref:item.ref,name:item.name,agentRefs:[...item.agent_refs],templates:item.room_templates.map(template=>({ref:template.ref,name:template.name,title:template.room_title,agentRefs:[...template.agent_refs]}))}))
  if(connections.some(item=>typeof item.ref!=='string'||typeof item.name!=='string'||typeof item.model!=='string'||typeof item.provider!=='string'||typeof item.enabled!=='boolean')||agents.some(item=>typeof item.ref!=='string'||typeof item.name!=='string'||typeof item.model!=='string'||typeof item.connectionRef!=='string')||projects.some(item=>typeof item.ref!=='string'||typeof item.name!=='string'||item.agentRefs.some(ref=>typeof ref!=='string')||item.templates.some(template=>typeof template.ref!=='string'||typeof template.name!=='string'||typeof template.title!=='string'||template.agentRefs.some(ref=>typeof ref!=='string'))))return null
  return{exportId:snapshot.id,receivedAt:snapshot.finishedAt||null,version:value.version,compactBytes:new TextEncoder().encode(JSON.stringify(value)).byteLength,connections,agents,projects,counts:{connections:connections.length,agents:agents.length,projects:projects.length,room_templates:projects.reduce((n,item)=>n+item.templates.length,0),project_assignments:projects.reduce((n,item)=>n+item.agentRefs.length,0),template_members:projects.reduce((n,item)=>n+item.templates.reduce((count,template)=>count+template.agentRefs.length,0),0)}}
 }catch{return null}
}

// Avoid a formatting-only import overflow. Large snapshots still copy/download
// in full; the UI separately explains the bounded native save/import contract.
export function workspaceExportText(value:WorkspaceBlueprint):string {
 const limit=2*1024*1024,compact=JSON.stringify(value)
 if(typeof compact!=='string')throw new Error('workspace_export_unavailable')
 const pretty=JSON.stringify(value,null,2)+'\n'
 return new TextEncoder().encode(pretty).byteLength<=limit?pretty:compact
}
