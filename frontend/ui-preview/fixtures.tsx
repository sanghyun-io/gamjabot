import api from '../src/lib/api'
import { AxiosHeaders } from 'axios'
import {cardApi, type Card, type CardReceipt} from '../src/api/vault_cards'
import {contactApi, type Contact, type ContactReceipt} from '../src/api/vault_contacts'
import Vault from '../src/pages/Vault'
import { vaultApi, type VaultItem } from '../src/api/vault'
import { templatePublisherApi, type PublishOperation } from '../src/api/templatePublisher'
import { botTemplatesApi, templatePreviewUrl, type BotTemplate, type TemplateSelection, type TemplateReceipt } from '../src/api/botTemplates'
import { botDuplicateApi, type DuplicateBody, type DuplicateReceipt } from '../src/api/botDuplicate'
import { botDirectoryApi, type BotDirectory, type BotDirectoryCommand, type BotDirectoryWrite } from '../src/api/botDirectory'
import { mcpDraftApi, type ActionDraft, type DraftReceipt } from '../src/api/mcpDraft'
import { beginRetryRead, finishRetryRead, beginRetryCommand, finishRetryCommand } from '../src/lib/requestRetryWorkspace'
import { directoryViewGeneration } from '../src/lib/directoryViewMemory'
import { importSourcePrefix, importSourceSuffix } from '../src/lib/memoryImportMessage'
import { avatarFromSeed, randomAvatar } from '../src/lib/avatarRecipe'
import { StrictMode, useState } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import '../src/index.css'
import './preview.css'
const screenshotFixture = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="240" height="160"%3E%3Crect width="240" height="160" fill="%23e2e8f0"/%3E%3C/svg%3E'
import Chat from '../src/pages/Chat'
import Dashboard from '../src/pages/Dashboard'
import Usage from '../src/pages/Usage'
import Devices from '../src/pages/Devices'
import { installSupportingFixtures } from './supportingFixtures'
import Connections from '../src/pages/Connections'
import Agents from '../src/pages/Agents'
import Projects from '../src/pages/Projects'
import { routinesApi, type Routine, type RoutineRun, type RoutineInput } from '../src/api/routines'
import McpServers from '../src/pages/McpServers'
import BotLibrary from '../src/pages/BotLibrary'
import BrowserDemonstration from '../src/components/BrowserDemonstration'
import { browserDemonstrationsApi, demonstrationDraftApi, type Demonstration } from '../src/api/browserDemonstrations'
import MemoryImport from '../src/pages/MemoryImport'
import { memoryImportApi, type MemoryImportReceipt, type MemoryImportRequest } from '../src/api/memoryImport'
import { updateImportWorkspace } from '../src/lib/memoryImportWorkspace'
import { botLibraryApi, type BotLibraryItem } from '../src/api/botLibrary'
import { libraryUploadsApi } from '../src/api/libraryUploads'
import { roomResultsApi, type ResultDetail, type ResultSummary } from '../src/api/roomResults'
import { mcpCredentialsApi } from '../src/api/mcpCredentials'
import { mcpOAuthApi } from '../src/api/mcpOAuth'
import { messageReactionsApi, type ReactionEmoji, type ReactionValue } from '../src/api/messageReactions'
import { mcpApi, type McpServer, type McpInput, type McpBinding } from '../src/api/mcp'
import { conversationsApi } from '../src/api/conversations'
import { connectionsApi } from '../src/api/connections'
import { projectsApi } from '../src/api/projects'
import { agentsApi, type Agent } from '../src/api/agents'
import { botsApi, type PrimaryBot } from '../src/api/bots'
import { botMemoriesApi, type BotMemoryEntry, type BotMemoryWrite, type BotMemoryEdit, type BotMemoryDelete, type BotMemoryReceipt } from '../src/api/botMemories'
import AgentMemoryDialog from '../src/components/AgentMemoryDialog'
import Skills from '../src/pages/Skills'
import { skillPackagesApi, type PackagePreview, type PackageManifest } from '../src/api/skillPackages'
import { skillsApi, type Skill, type SkillFields } from '../src/api/skills'
import { eventsApi, validReply, type RoomEvent, type ReplyThreadItem } from '../src/api/events'
import { teammatesApi, type TeammateHandoff } from '../src/api/teammates'
import { botBatchesApi, type BotBatch, type BotBatchMember } from '../src/api/botBatches'
import { botCreationsApi, type BotCreationReceipt } from '../src/api/botCreations'
import { memorySharingApi, type MemoryPolicy, type CommonFact, type CommonReceipt } from '../src/api/memorySharing'
import { primaryCheckinsApi, type PrimaryCheckin, type CheckinRun } from '../src/api/primaryCheckins'
import { botPersistenceApi, type BotPersistenceReceipt } from '../src/api/botPersistence'
import { botMessagesApi, botMessageRetriesApi, type BotMessage, type BotMessageRef, type BotMessageRetryPlan, type BotMessageRetryReceipt } from '../src/api/botMessages'
import { draftsApi, type RoomDraft } from '../src/api/drafts'
import { readStatesApi, type RoomReadState } from '../src/api/readStates'
import { dashboardApi, type DashboardSummary } from '../src/api/dashboard'
import { schedulesApi, type RoomSchedule } from '../src/api/schedules'
import { summariesApi, type HandoffNoteWrite } from '../src/api/summaries'
import { roomTemplatesApi, type RoomTemplate, type RoomTemplateWrite } from '../src/api/roomTemplates'
import { workspaceBlueprintApi, type WorkspaceBlueprint } from '../src/api/workspaceBlueprint'
import { roomIndexApi } from '../src/api/roomIndex'
import { attentionApi } from '../src/api/attention'
import { agentWorkloadsApi } from '../src/api/agentWorkloads'
import { contextPreviewsApi } from '../src/api/contextPreviews'
import { readinessApi, type ReadinessTestRecord } from '../src/api/readiness'
import { artifactsApi, type ScreenshotArtifactItem } from '../src/api/artifacts'
import { requestInspectorApi } from '../src/api/requestInspector'
import { requestRetriesApi } from '../src/api/requestRetries'
import { requestFinalizeApi } from '../src/api/requestFinalize'
import { starterApi, type StarterIntent, type StarterPreview } from '../src/api/starter'
import { createdStarterFixture } from './starterFixture'
import { finalizeFixtureAuthority } from './finalizeFixture'
import { scopeStopsApi, type ScopeStopReceipt } from '../src/api/scopeStops'
import type { WorkState } from '../src/lib/workState'
import { toolsApi, type ToolActivity, type ActionPreview, type ExecutionBinding } from '../src/api/tools'
import { useAuthStore } from '../src/stores/authStore'
import { I18nProvider } from '../src/i18n'
import QuickRoomSwitcher from '../src/components/QuickRoomSwitcher'
import AttentionInbox from '../src/components/AttentionInbox'
import { buildRecipientGroups } from '../src/lib/roomWorkflow'
import { previewEmitRoomEvent } from './localHost'
import { AppHeaderFrame } from '../src/components/AppHeaderFrame'
import AppMenu from '../src/components/AppMenu'
import KeyboardHelp from '../src/components/KeyboardHelp'

// Synthetic, non-user content for the actual template file picker stories.
export const syntheticBotTemplate:BotTemplate={version:1,name:'Sample helper',description:'Synthetic planning assistant',system_prompt:'Help review the sample plan.',avatar:avatarFromSeed(8),settings:{temperature:'auto',max_tokens:0},skills:[{name:'Sample research',description:'Review public notes',content:'Summarize the sample notes.'}],routines:[{name:'Weekly sample',instruction:'Review the synthetic plan.',schedule:{kind:'weekly',weekdays:[0],hour:9,minute:0,timezone:'Asia/Seoul'},enabled:true},{name:'Event sample',instruction:'Review a sample event.',schedule:{kind:'webhook'},enabled:false}]}
export type PreviewScenarioOptions = { screen?: string; state?: string }
// One active synthetic scenario per iframe. Real product pages render below.
export function createPreviewScenario({ screen = 'chat', state = 'ready' }: PreviewScenarioOptions = {}) {
const previewScreen = screen
const previewState = state
Object.assign(memorySharingApi, { readChange: async () => { throw new Error('Synthetic memory receipt unavailable') }, policy: async () => { throw new Error('Synthetic memory policy unavailable') }, setPolicy: async () => { throw new Error('Synthetic memory policy unavailable') }, visibility: async () => { throw new Error('Synthetic memory visibility unavailable') }, common: async () => { throw new Error('Synthetic common memory unavailable') }, addCommon: async () => { throw new Error('Synthetic common memory unavailable') }, changeCommon: async () => { throw new Error('Synthetic common memory unavailable') } })

const mcpId='b2000000-0000-4000-8000-000000000001',mcpRunId='b2000000-0000-4000-8000-000000000003'
let mcpSequence=100, mcpPollCount=0
const mcpNextId=()=>`b2000000-0000-4000-8000-${String(mcpSequence++).padStart(12,'0')}`
let mcpRows:McpServer[]=previewState==='mcp-empty'?[]:[{version:1,server_id:mcpId,owner_id:1,revision:1,name:'문서 도구',enabled:previewState!=='mcp-disconnected',transport:{type:'http',url:'https://example.invalid/mcp'},credential_ref:null,discovery_state:['mcp-revoked','mcp-many'].includes(previewState)?'ready':'not_discovered'}]
if(previewState.startsWith('notion-oauth-')||previewState.startsWith('notion-skills-')||previewState.startsWith('mcp-discovery-poll-')||previewState.startsWith('mcp-transport-diagnostic-')) mcpRows=[{...mcpRows[0],name:'Notion',transport:{type:'http',url:'https://mcp.notion.com/mcp'}}]
const mcpCommands=new Map<string,unknown>(),mcpSecrets=new Map<string,string>(),mcpDiscoveries=new Map<string,{id:string;revision:number}>(),mcpPreviews=new Map<string,{serverId:string;revision:number}>(),mcpBindings=new Map<string,McpBinding>()
if(['mcp-revoked','mcp-many'].includes(previewState))mcpDiscoveries.set(mcpId,{id:mcpRunId,revision:1})
const mcpCurrent=(server:McpServer)=>{const row=mcpRows.find(x=>x.server_id===server.server_id);if(!row||row.revision!==server.revision)throw {response:{status:409}};return row}
const mcpRevoke=(id:string)=>{for(const [key,b] of mcpBindings)if(b.server_id===id&&b.enabled)mcpBindings.set(key,{...b,enabled:false,revision:b.revision+1})}
Object.assign(mcpApi,{
 list:async()=>structuredClone(mcpRows),
 read:async(id:string)=>{const row=mcpRows.find(x=>x.server_id===id);if(!row)throw Error('synthetic missing');return structuredClone(row)},
 credentialPreview:async(server:McpServer)=>{mcpCurrent(server)},
 credential:async(server:McpServer,ref:string)=>{const row=mcpCurrent(server),next={...row,revision:row.revision+1,credential_ref:ref,credential_state:'configured' as const,discovery_state:'stale' as const};mcpRows=mcpRows.map(x=>x.server_id===server.server_id?next:x);mcpRevoke(server.server_id);return structuredClone(next)},
 create:async(body:McpInput,key:string)=>{const old=mcpCommands.get(key);if(old)return structuredClone(old);const row:McpServer={version:1,server_id:mcpNextId(),owner_id:1,revision:1,...body,credential_ref:null,discovery_state:'not_discovered'};mcpRows=[...mcpRows,row];const result={version:1,operation:'create',server:row};mcpCommands.set(key,result);if(previewState==='mcp-unknown')throw Error('synthetic lost response');return structuredClone(result)},
 update:async(server:McpServer,body:McpInput)=>{if(previewState==='mcp-conflict')throw {response:{status:409}};const row=mcpCurrent(server),next={...row,...body,revision:row.revision+1,discovery_state:'stale' as const};mcpRows=mcpRows.map(x=>x.server_id===server.server_id?next:x);mcpRevoke(server.server_id);return structuredClone(next)},
 preview:async(server:McpServer)=>{const row=mcpCurrent(server),preview_id=mcpNextId();mcpPreviews.set(preview_id,{serverId:row.server_id,revision:row.revision});return {version:1,preview_id,server_id:row.server_id,revision:row.revision,transport:structuredClone(row.transport),expires_at:'2099-10-29T00:00:00Z'}},
 discover:async(server:McpServer,preview:{preview_id:string},key:string)=>{const old=mcpCommands.get(key);if(old)return structuredClone(old);const row=mcpCurrent(server),p=mcpPreviews.get(preview.preview_id);if(!p||p.serverId!==row.server_id||p.revision!==row.revision)throw {response:{status:409}};if(previewState.startsWith('mcp-transport-diagnostic-'))throw {response:{status:503,data:{detail:previewState==='mcp-transport-diagnostic-known'?{code:'mcp_transport_unavailable',stage:'configure',reason:'mcp_configuration_epoch_changed'}:{code:'mcp_transport_unavailable',stage:'untrusted-stage',reason:'untrusted-reason'}}}};const failed=previewState==='mcp-discovery-failure',running=['mcp-discovery-known-unknown','mcp-discovery-missing'].includes(previewState)||previewState.startsWith('mcp-discovery-poll-'),runId=mcpNextId();const discovery={discovery_id:runId,server_id:row.server_id,server_revision:row.revision,state:failed?'failed':running?'running':'completed',failure_code:failed?'synthetic_unavailable':null};const receipt={version:1,operation:'discover',discovery};if(previewState==='mcp-discovery-missing')throw Error('synthetic lost response');mcpCommands.set(key,receipt);if(!failed&&!running){mcpDiscoveries.set(row.server_id,{id:runId,revision:row.revision});mcpRows=mcpRows.map(x=>x.server_id===row.server_id?{...x,discovery_state:'ready'}:x);mcpRevoke(row.server_id)}return structuredClone(receipt)},
 command:async(key:string)=>{const value=mcpCommands.get(key);if(!value)throw Error('synthetic receipt absent');const receipt=value as {version:number;operation:string;discovery:{discovery_id:string;server_id:string;server_revision:number;state:string;failure_code:string|null}};if(receipt.operation==='discover'&&previewState.startsWith('mcp-discovery-poll-')&&++mcpPollCount>=2){const d=receipt.discovery;d.state=previewState==='mcp-discovery-poll-complete'?'completed':previewState==='mcp-discovery-poll-failed'?'failed':'unknown';d.failure_code=d.state==='failed'?'mcp_disc_invalid_list':d.state==='unknown'?'mcp_discovery_uncertain':null;if(d.state==='completed'){mcpDiscoveries.set(d.server_id,{id:d.discovery_id,revision:d.server_revision});mcpRows=mcpRows.map(row=>row.server_id===d.server_id?{...row,discovery_state:'ready'}:row)}}if(previewState==='mcp-discovery-known-unknown'&&receipt.operation==='discover'){receipt.discovery.state='unknown';receipt.discovery.failure_code='mcp_discovery_uncertain'}return structuredClone(value)},
 tools:async(server:McpServer)=>{const row=mcpCurrent(server),run=mcpDiscoveries.get(row.server_id),ready=!!run&&run.revision===row.revision;const count=previewState==='mcp-many'?30:1,serverPrefix=row.server_id.replace(/-/g,'').slice(-12);return {version:1,server_id:row.server_id,server_revision:row.revision,discovery_id:ready?run.id:null,items:ready?Array.from({length:count},(_,i)=>({tool_id:(serverPrefix+(i+1).toString(16)).padStart(64,'0'),name:count>1?`tool_${i+1}`:'read_document',description:count>1?'도구 설명':'문서를 읽습니다.',input_schema:{type:'object'},schema_sha256:'b'.repeat(64)})):[]}},
 bindings:async(agent:number)=>structuredClone(mcpRows.map(s=>mcpBindings.get(`${agent}:${s.server_id}`)||{server_id:s.server_id,revision:previewState==='mcp-revoked'?1:0,enabled:false,tool_ids:null})),
 bind:async(agent:number,b:McpBinding,serverRevision:number,enabled:boolean,toolIds:string[]|null)=>{const row=mcpRows.find(x=>x.server_id===b.server_id);if(!row||row.revision!==serverRevision||previewState==='mcp-conflict')throw {response:{status:409}};if(enabled&&(toolIds===null&&previewState==='mcp-many'||toolIds!==null&&toolIds.length>27))throw {response:{status:409,data:{detail:{code:'mcp_context_limit'}}}};mcpBindings.set(`${agent}:${b.server_id}`,{server_id:b.server_id,revision:b.revision+1,enabled,tool_ids:toolIds})},
})
if(previewState.startsWith('notion-oauth-')) {
  const result=(server:string,revision:number,operation:string,state:string)=>({version:1,provider:'notion',server_id:server,config_revision:revision,operation_id:operation,state,...(state==='authorized'?{credential_ref:`mcp-oauth:1:${server}:${operation}`,workspace:{id:'synthetic-workspace',name:'Synthetic workspace'}}:{})})
  const state=previewState==='notion-oauth-preparing'?'preparing':previewState==='notion-oauth-failed'?'failed':['notion-oauth-authorized','notion-oauth-conflict'].includes(previewState)?'authorized':'authorizing'
  Object.assign(mcpOAuthApi,{available:()=>previewState!=='notion-oauth-unavailable',begin:async(server:string,revision:number,operation:string)=>{if(previewState==='notion-oauth-unknown')throw Error('Synthetic response lost');return result(server,revision,operation,state)},status:async(server:string,revision:number,operation:string)=>result(server,revision,operation,state),cancel:async(server:string,revision:number,operation:string)=>result(server,revision,operation,'cancelled'),open:async()=>undefined})
  if(previewState==='notion-oauth-conflict'){const attach=mcpApi.credential;let changed=false;mcpApi.credential=async(server,ref)=>{if(!changed){changed=true;mcpRows=mcpRows.map(row=>row.server_id===server.server_id?{...row,revision:row.revision+1}:row);throw {response:{status:409}}}return attach(server,ref)}}
}
Object.assign(mcpCredentialsApi, {
 available:()=>previewState.startsWith('mcp-secret'),
 store:async(serverId:string,_revision:number,operationId:string,_secret:string)=>{const credential_ref=`mcp:1:${serverId}:${operationId}`;mcpSecrets.set(operationId,credential_ref);if(previewState==='mcp-secret-unknown')throw Error('synthetic lost response');return {state:'stored' as const,credential_ref}},
 status:async(_serverId:string,_revision:number,operationId:string)=>{const credential_ref=mcpSecrets.get(operationId);return credential_ref?{state:'present' as const,credential_ref}:{state:'missing' as const}},
})
const roomId = 501
const skillId = (n: number) => `af041000-0000-4000-8000-${String(n).padStart(12,'0')}`
let skillRows: Skill[] = previewState === 'skill-empty' ? [] : [{version:1,skill_id:skillId(1),revision:1,name:'회의 정리',description:'결정 사항과 다음 행동부터 정리',content:'회의 내용을 읽고 결정 사항과 담당자, 다음 행동을 정리합니다.',active:previewState !== 'skill-inactive',source:null,created_at:'2026-10-29T01:00:00Z',updated_at:'2026-10-29T01:00:00Z'}]
if(previewState==='package-provenance') skillRows=[{...skillRows[0],active:false,revision:2,package_provenance:{package_id:'gamja.meeting-notes',package_version:'1.0.0',author:'Gamja',license:'CC0-1.0',digest:'a'.repeat(64),original_content_digest:'b'.repeat(64),origin:'builtin'},package_modified:true}]
let skillBindings = new Map<string,{revision:number;enabled:boolean}>(skillRows.map(x=>[x.skill_id,{revision:0,enabled:false}]))
const skillReceipts = new Map<string, Skill>()
const packageManifest:PackageManifest={package_id:'gamja.meeting-notes',package_version:'1.0.0',author:'Gamja',license:'CC0-1.0',name:'회의 정리',description:'결정과 다음 행동을 정리합니다.',content:'제공된 회의 내용을 바탕으로 결정과 다음 행동을 정리합니다.'}
const packagePreview:PackagePreview={version:1,manifest:packageManifest,digest:'a'.repeat(64)}
let packageLost=previewState==='package-unknown'||previewState==='notion-skills-unknown'
Object.assign(skillPackagesApi,{
 catalog:async(q:string='')=>previewState==='package-empty'?[]:[packagePreview].filter(p=>`${p.manifest.name} ${p.manifest.description}`.includes(q)),
 preview:async(manifest:PackageManifest)=>({version:1,manifest:structuredClone(manifest),digest:'a'.repeat(64)}),
 install:async(preview:PackagePreview,key:string)=>{if(previewState==='notion-skills-rejected')throw {response:{status:422}};if(previewState==='package-conflict')throw syntheticSkillError('skill_package_digest_changed');const m=preview.manifest,hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(m.content));const original_content_digest=Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('');const row:Skill={version:1,skill_id:skillId(30),revision:1,name:m.name,description:m.description,content:m.content,active:false,source:null,package_provenance:{package_id:m.package_id,package_version:m.package_version,author:m.author,license:m.license,digest:preview.digest,original_content_digest,origin:previewState.startsWith('notion-skills-')?'manual_manifest':'builtin'},package_modified:false,created_at:'2026-10-29T01:00:00Z',updated_at:'2026-10-29T01:00:00Z'};skillRows=[...skillRows,row];skillBindings.set(row.skill_id,{revision:0,enabled:false});skillReceipts.set(key,row);if(packageLost){packageLost=false;throw Error('Synthetic lost response')};return structuredClone(row)},
})
let skillLost = previewState === 'skill-unknown', skillConflict = previewState === 'skill-conflict'
const syntheticSkillError = (code:string) => Object.assign(new Error('Synthetic Skill failure'),{response:{status:409,data:{detail:{code}}}})
Object.assign(skillsApi, {
  persistence: async () => { throw new Error('Synthetic Skill receipt unavailable') },
  list: async (q = '') => { if (previewState === 'skill-error') throw new Error('Synthetic read failure'); return structuredClone(skillRows.filter(x => `${x.name} ${x.description}`.toLowerCase().includes(q.toLowerCase()))) },
  read: async (id:string) => { const row=skillRows.find(x=>x.skill_id===id); if(!row) throw new Error('Synthetic Skill absent'); return structuredClone(row) },
  command: async (key:string) => { const row=skillReceipts.get(key); if(!row) throw new Error('Synthetic command absent'); return structuredClone(row) },
  versions: async (id:string) => ({version:1,skill_id:id,versions:structuredClone(skillRows.filter(x=>x.skill_id===id)),next_before_revision:null}),
  create: async (body:SkillFields,key:string) => { const row:Skill={version:1,skill_id:skillId(10+skillRows.length),revision:1,...body,active:true,source:null,created_at:'2026-10-29T01:00:00Z',updated_at:'2026-10-29T01:00:00Z'};skillRows=[...skillRows,row];skillBindings.set(row.skill_id,{revision:0,enabled:false});skillReceipts.set(key,row);if(skillLost){skillLost=false;throw new Error('Synthetic response lost')};return structuredClone(row) },
  fromRequest: async (body:SkillFields & {room_id:number;root_event_id:string;result_event_id:string;agent_id:number},key:string) => { if(previewState==='skill-source-rejected') throw syntheticSkillError('skill_source_not_completed'); const row:Skill={version:1,skill_id:skillId(10+skillRows.length),revision:1,name:body.name,description:body.description,content:body.content,active:false,source:{room_id:body.room_id,root_event_id:body.root_event_id,root_seq:1,result_event_id:body.result_event_id,result_seq:2,agent_id:body.agent_id},created_at:'2026-10-29T01:00:00Z',updated_at:'2026-10-29T01:00:00Z'};skillRows=[...skillRows,row];skillBindings.set(row.skill_id,{revision:0,enabled:false});skillReceipts.set(key,row);return structuredClone(row) },
  update: async (id:string,revision:number,body:SkillFields & {active:boolean}) => { const row=skillRows.find(x=>x.skill_id===id);if(!row || revision!==row.revision || skillConflict){skillConflict=false;throw syntheticSkillError('skill_revision_conflict')}; const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(body.content));const contentHash=Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('');const next={...row,...body,revision:row.revision+1,...(row.package_provenance?{package_modified:contentHash!==row.package_provenance.original_content_digest}:{})};skillRows=skillRows.map(x=>x.skill_id===id?next:x);return structuredClone(next) },
  bindings: async (agent:number) => ({version:1,agent_id:agent,items:skillRows.map(row=>({skill_id:row.skill_id,...skillBindings.get(row.skill_id)!}))}),
  setBinding: async (_agent:number,id:string,revision:number,enabled:boolean) => { const old=skillBindings.get(id);if(!old || old.revision!==revision)throw syntheticSkillError('skill_binding_conflict');const value={revision:old.revision+1,enabled};skillBindings.set(id,value);return {skill_id:id,...value} },
})
const screenshotAction = '3d1e0e6c-9eeb-4571-864b-f2592c44db38'
const at = (minute: number) => `2026-10-02T01:${String(minute).padStart(2, '0')}:00Z`
const event = (seq: number, actorType: RoomEvent['actor_type'], actorId: number | null,
  name: string, kind: RoomEvent['kind'], payload: Record<string, unknown>): RoomEvent => ({
  event_id: `synthetic-event-${seq}`, device_id: null, room_id: roomId, seq, actor_type: actorType,
  actor_id: actorId, actor_name_snapshot: name, kind, payload, created_at: at(13 + seq), idempotency_key: null,
})

const baseEvents: RoomEvent[] = [
  event(1, 'user', null, '지민', 'message', { content: '설치 가이드의 마지막 검토를 부탁해.' }),
  event(2, 'agent', 8, '기획', 'message', { content: '설치 가이드의 전체 구성을 검토했습니다. 큰 흐름과 필수 항목은 명확하고, 몇 가지 표현을 더 자연스럽게 다듬으면 좋을 것 같아요.' }),
  event(3, 'agent', 9, '검토', 'message', { content: '지금 문서의 용어와 단계별 설명을 중심으로 검토하고 있어요. 잠시만 기다려주세요.' }),
  event(4, 'agent', 10, '테스트', 'message', { content: '검토가 끝나면 실제 환경에서 설치 과정을 다시 확인하겠습니다.' }),
]
const screenshotEvent = event(5, 'tool', 8, '기획', 'tool_result', { state: 'completed', text: '로컬 미리보기 화면을 공유했습니다.',
  action_id: screenshotAction, screenshot_ref: `rooms/${roomId}/screenshots/${screenshotAction}.png`,
  image_mime: 'image/png', image_width: 1440, image_height: 1024, agent_id: 8 })
const galleryRemovedAction = '4d1e0e6c-9eeb-4571-864b-f2592c44db38'
const galleryUnavailableAction = '5d1e0e6c-9eeb-4571-864b-f2592c44db38'
const galleryEvents: RoomEvent[] = [...baseEvents, screenshotEvent,
  event(6, 'tool', 9, '검토', 'tool_result', { state: 'completed', text: '이전 검토 화면', action_id: galleryRemovedAction,
    screenshot_ref: `rooms/${roomId}/screenshots/${galleryRemovedAction}.png`, image_mime: 'image/png', image_width: 1280, image_height: 720 }),
  event(7, 'tool', 10, '테스트', 'tool_result', { state: 'completed', text: '더 이상 사용할 수 없는 테스트 화면', action_id: galleryUnavailableAction,
    screenshot_ref: `rooms/${roomId}/screenshots/${galleryUnavailableAction}.png`, image_mime: 'image/png', image_width: 1024, image_height: 768 })]
const failureEvent = event(6, 'agent', 12, '조사', 'failure', { code: 'provider_unavailable', message: 'Synthetic provider unavailable.',
  diagnostic: { stage: 'provider_request', correlation_id: 'a'.repeat(32) } })
const quietToolEvent = event(5, 'tool', 10, '테스트', 'tool_result', { state: 'completed', text: 'Synthetic guide check complete.' })
const quietSystemEvent = event(6, 'system', null, '시스템', 'summary', {
  content: 'Extractive room checkpoint: 설치 가이드 검토 상태와 기술 기록을 보존했습니다.',
})
const longHistoryEvents: RoomEvent[] = Array.from({ length: 72 }, (_, index) => ({
  ...event(index + 1, index % 2 === 0 ? 'agent' : 'user', index % 2 === 0 ? 8 : null,
    index % 2 === 0 ? '기획' : '지민', 'message', { content: `긴 대화 기록 ${index + 1}` }),
  created_at: `2026-10-02T02:${String(index % 60).padStart(2, '0')}:00Z`,
}))
const comparisonEvents: RoomEvent[] = [
  event(1, 'user', 1, '지민', 'message', { content: '설치 안내를 처음 사용하는 사람의 관점에서 비교해 줘.' }),
  event(2, 'agent', 8, '기획 (당시)', 'message', { content: '첫 화면에서 설치 전 준비 사항을 먼저 보여주면 흐름을 이해하기 쉽습니다.',
    trigger_event_id: 'synthetic-event-1', round: 1, model_use: { connection_id: 1, model_id: 'qwen3.7-plus-2026-08', provider_kind: 'compatible', watermark_seq: 1 } }),
  event(3, 'agent', 9, '검토 (당시)', 'message', { content: '단계마다 완료 기준을 한 문장으로 붙이면 처음 쓰는 사람도 현재 위치를 확인할 수 있습니다.',
    trigger_event_id: 'synthetic-event-1', round: 1, model_use: { connection_id: 2, model_id: 'claude-sonnet-2026-07', provider_kind: 'anthropic', watermark_seq: 1 } }),
  event(4, 'tool', 8, '기획', 'tool_result', { text: '비교 화면에 표시되지 않는 기술 실행 기록', trigger_event_id: 'synthetic-event-1', round: 1 }),
  event(5, 'user', 1, '지민', 'message', { content: '두 가지 출시 순서 중 위험이 낮은 쪽을 골라 근거를 알려 줘.' }),
  event(6, 'agent', 8, '기획 (초안 시점)', 'message', { content: '기존 사용자에게 영향이 적은 단계적 출시를 권합니다. 먼저 내부 검증을 마친 뒤 공개 범위를 넓히는 순서가 안전합니다.',
    trigger_event_id: 'synthetic-event-5', round: 1, model_use: { connection_id: 1, model_id: 'qwen3.7-plus-2026-09', provider_kind: 'compatible', watermark_seq: 5 } }),
  event(7, 'agent', 9, '검토 (초안 시점)', 'message', { content: '단계적 출시가 적절합니다. 각 단계의 중단 기준을 미리 정하면 문제가 생겨도 영향 범위를 제한할 수 있습니다.',
    trigger_event_id: 'synthetic-event-5', round: 1, model_use: { connection_id: 2, model_id: 'claude-sonnet-2026-09', provider_kind: 'anthropic', watermark_seq: 5 } }),
  event(8, 'agent', 8, '기획 (초안 시점)', 'message', { content: '두 의견을 합치면 내부 검증, 제한 공개, 전체 공개의 세 단계와 각 단계별 중단 기준이 필요합니다.',
    trigger_event_id: 'synthetic-event-5', round: 2, model_use: { connection_id: 1, model_id: 'qwen3.7-plus-2026-09', provider_kind: 'compatible', watermark_seq: 7 } }),
]
const inspectorTrigger = '11111111-1111-4111-8111-111111111111'
const inspectorOutcome = '22222222-2222-4222-8222-222222222222'
const inspectorToolResult = '33333333-3333-4333-8333-333333333333'
const inspectorFailure = '44444444-4444-4444-8444-444444444444'
const inspectorLaterTrigger = '55555555-5555-4555-8555-555555555555'
const inspectorRetryEvent = '66666666-6666-4666-8666-666666666666'
let inspectorRetryStarted = false
const inspectorEvents: RoomEvent[] = [
  { ...event(1, 'user', 1, '지민', 'message', { content: '출시 전 검사 결과를 정확히 정리해 줘.' }), event_id: inspectorTrigger },
  { ...event(2, 'agent', 8, '기획 (요청 당시)', 'message', { content: '설치와 되돌리기 절차를 확인했습니다.', trigger_event_id: inspectorTrigger, round: 1,
    model_use: { connection_id: 1, model_id: 'qwen3.7-plus-2026-08', provider_kind: 'compatible', watermark_seq: 1 } }), event_id: inspectorOutcome },
  { ...event(3, 'tool', 8, '기획 (요청 당시)', 'tool_result', { state: 'completed', text: '검사 완료', trigger_event_id: inspectorTrigger,
    action_id: screenshotAction }), event_id: inspectorToolResult },
  { ...event(4, 'agent', 9, '검토 (요청 당시)', 'failure', { code: 'provider_unavailable', message: 'Synthetic failure',
    trigger_event_id: inspectorTrigger, round: 1 }), event_id: inspectorFailure },
  { ...event(5, 'user', 1, '지민', 'message', { content: '후속 요청은 별도로 처리해 줘.' }), event_id: inspectorLaterTrigger },
  { ...event(6, 'agent', 8, '기획 (후속 요청 당시)', 'message', { content: '후속 요청의 별도 결과입니다.', trigger_event_id: inspectorLaterTrigger, round: 1,
    model_use: { connection_id: 1, model_id: 'qwen3.7-plus-2026-09', provider_kind: 'compatible', watermark_seq: 5 } }) },
]
const finalizeSecondOutcome = '77777777-7777-4777-8777-777777777777'
const finalizeEventId = '88888888-8888-4888-8888-888888888888'
const finalizeResultId = '99999999-9999-4999-8999-999999999999'
let finalizeStarted = false
let finalizeAgentId = 8
const finalizeEvents: RoomEvent[] = [
  { ...event(1, 'user', 1, '지민', 'message', { content: '첫 출시 계획을 검토하고 결론을 제안해 줘.' }), event_id: inspectorTrigger },
  { ...event(2, 'agent', 8, '기획 (요청 당시)', 'message', { content: '단계별 출시와 명확한 중단 기준을 권합니다.', trigger_event_id: inspectorTrigger, round: 1,
    model_use: { connection_id: 1, model_id: 'qwen3.7-plus-2026-08', provider_kind: 'compatible', watermark_seq: 1 } }), event_id: inspectorOutcome },
  { ...event(3, 'agent', 9, '검토 (요청 당시)', 'message', { content: '첫 단계는 소수 사용자에게만 공개하고 지표를 확인해야 합니다.', trigger_event_id: inspectorTrigger, round: 1,
    model_use: { connection_id: 2, model_id: 'claude-sonnet-2026-07', provider_kind: 'anthropic', watermark_seq: 1 } }), event_id: finalizeSecondOutcome },
  { ...event(4, 'agent', 10, '테스트 (요청 당시)', 'failure', { code: 'provider_unavailable', trigger_event_id: inspectorTrigger, round: 1 }), event_id: inspectorFailure },
]
const handoffEvents: RoomEvent[] = [
  event(1, 'user', 1, '지민', 'message', { content: '출시 전 확인해야 할 항목을 정리해 줘.' }),
  event(2, 'agent', 8, '기획', 'message', { content: '설치 안내, 되돌리기 절차, 지원 연락처를 확인해야 합니다.' }),
  event(3, 'tool', 8, '기획', 'tool_result', { state: 'completed', text: '내부 검사 기록' }),
  event(4, 'agent', 9, '검토', 'message', { content: '공개 전에 단계별 중단 기준도 확인 목록에 넣는 편이 안전합니다.' }),
  event(5, 'system', null, '시스템', 'summary', { owner_checkpoint: true, source_start_seq: 1, source_end_seq: 4,
    content: '단계적 출시를 유지하고 각 단계의 중단 기준을 공개 전에 확인한다.' }),
  event(6, 'user', 1, '지민', 'message', { content: '다음 담당자가 바로 이어서 볼 수 있도록 남은 결정을 정리해 줘.' }),
]
// Real Chat renders this explicit synthetic server contract. No Agent or model runs here.
const teammateRoot = '7ad91000-0000-4000-8000-000000000001'
const teammateRequest = '7ad91000-0000-4000-8000-000000000002'
const teammateResult = '7ad91000-0000-4000-8000-000000000004'
const teammateReturn = '7ad91000-0000-4000-8000-000000000005'
const teammateId = '7ad91000-0000-4000-8000-000000000010'
const teammateState = previewState === 'teammates-completed' ? 'completed' : previewState === 'teammates-failed' ? 'failed'
  : previewState === 'teammates-cancelled' ? 'cancelled' : previewState === 'teammates-pending' ? 'pending' : 'running'
const teammateMeta = { version: 1, handoff_id: teammateId, sender_agent_id: 8, recipient_agent_id: 9, depth: 1 }
const teammateEvents: RoomEvent[] = [
  { ...event(1, 'user', 1, '지민', 'message', { content: '@기획 출시 안내를 완성해 줘. 필요한 부분은 검토에게 부탁해.' }), event_id: teammateRoot },
  { ...event(2, 'agent', 8, '기획', 'message', { content: '설치 안내에서 빠진 단계가 있는지 검토하고, 수정이 필요한 부분만 알려 줘.',
    trigger_event_id: teammateRoot, teammate_handoff: { ...teammateMeta, phase: 'request' } }), event_id: teammateRequest },
  { ...event(3, 'system', null, '시스템', 'activity', { category: 'teammate_handoff',
    status: teammateState === 'pending' || teammateState === 'cancelled' ? teammateState : 'running',
    handoff_id: teammateId, root_request_event_id: teammateRoot, sender_agent_id: 8, recipient_agent_id: 9 }),
    event_id: '7ad91000-0000-4000-8000-000000000003' },
]
if (teammateState === 'completed' || teammateState === 'failed') teammateEvents.push(
  { ...event(4, 'agent', 9, '검토', teammateState === 'failed' ? 'failure' : 'message', {
    ...(teammateState === 'failed' ? { code: 'provider_unavailable', message: 'Synthetic teammate connection unavailable.' }
      : { content: '기존 데이터 유지 안내가 빠져 있습니다. 설치 전에 백업 위치와 되돌리는 방법을 추가해 주세요.' }),
    trigger_event_id: teammateRoot, teammate_handoff: { ...teammateMeta, phase: 'response' } }), event_id: teammateResult },
)
if (teammateState === 'completed') teammateEvents.push(
  { ...event(5, 'agent', 8, '기획', 'message', { content: '검토가 지적한 백업 위치와 복구 절차를 반영해 출시 안내를 정리했습니다. 설치 전 백업하고, 문제가 생기면 직전 버전으로 되돌릴 수 있도록 안내합니다.',
    trigger_event_id: teammateRoot, teammate_handoff: { ...teammateMeta, phase: 'return' } }), event_id: teammateReturn },
)
const teammateHandoff: TeammateHandoff = {
  handoff_id: teammateId, root_request_event_id: teammateRoot, root_request_seq: 1, parent_handoff_id: null,
  sender_agent_id: 8, sender_name: '기획', recipient_agent_id: 9, recipient_name: '검토',
  content: String(teammateEvents[1].payload.content), state: teammateState,
  reply_state: teammateState === 'completed' ? 'delivered' : 'none', request_event_id: teammateRequest, request_seq: 2,
  result_event_id: ['completed', 'failed'].includes(teammateState) ? teammateResult : null,
  result_seq: ['completed', 'failed'].includes(teammateState) ? 4 : null,
  return_event_id: teammateState === 'completed' ? teammateReturn : null, return_seq: teammateState === 'completed' ? 5 : null,
  depth: 1, created_at: at(15), updated_at: at(18),
}
let events: RoomEvent[] = previewState === 'error' || previewState === 'attention' ? [...baseEvents, quietToolEvent, failureEvent]
  : previewState === 'media' ? [...baseEvents, screenshotEvent]
    : previewState === 'gallery' ? galleryEvents
    : previewState === 'context' ? [...baseEvents, screenshotEvent, quietSystemEvent]
    : previewState === 'quiet' ? [...baseEvents, quietToolEvent, quietSystemEvent]
      : previewState === 'long' || previewState === 'timeline' ? longHistoryEvents
        : previewState === 'comparison' ? comparisonEvents
          : previewState.startsWith('finalize') ? finalizeEvents
          : previewState.startsWith('retry') ? inspectorEvents.slice(0, 4)
          : previewState.startsWith('inspector') ? inspectorEvents
          : previewState.startsWith('handoff') || previewState === 'closed' ? handoffEvents : baseEvents

// The real Chat component receives a recorded incomplete teammate failure.
if (previewScreen === 'chat' && ['teammate-incomplete-failure', 'teammate-changed-failure'].includes(previewState)) events = [
  event(1, 'user', 1, '지민', 'message', { content: '동료와 함께 문서를 검토해 주세요.' }),
  event(2, 'agent', 8, '기획', 'failure', { code: previewState === 'teammate-changed-failure' ? 'teammate_result_changed' : 'teammate_result_incomplete', message: 'Synthetic incomplete teammate response.' }),
]

// Synthetic direct-root failure with a separately observed held-profile state.
if (previewScreen==='chat' && ['readonly-failure-parked','readonly-failure-unknown'].includes(previewState)) {
 const root='af042000-0000-4000-8000-000000000001',actionId='af042000-0000-4000-8000-000000000002'
 events=[{...event(1,'user',1,'지민','message',{content:'합성 브라우저 페이지를 확인해 주세요.'}),event_id:root},
  {...event(2,'tool',null,'도구','tool_result',{action_id:actionId,trigger_event_id:root,agent_id:8,family:'browser_inspect',state:'completed',text:'합성 페이지를 확인했습니다.'}),event_id:'af042000-0000-4000-8000-000000000003'},
  {...event(3,'agent',8,'기획','failure',{trigger_event_id:root,code:'provider_invalid_response',message:'Synthetic final response failed.'}),event_id:'af042000-0000-4000-8000-000000000004'}]
}

if (previewState === 'duplicate-failure') events = [
 event(1, 'user', 1, '지민', 'message', { content: '감자봇을 직접 써보며 개선할 점을 찾아줘.' }),
 event(2, 'agent', 8, '제안자', 'failure', { code: 'model_feature_unavailable', message: 'Synthetic exact-model feature check unavailable.' }),
 event(3, 'agent', 9, '검토자', 'failure', { code: 'model_feature_unavailable', message: 'Synthetic exact-model feature check unavailable.' }),
 event(4, 'user', 1, '지민', 'message', { content: '다시 진행해봐' }),
 event(5, 'agent', 8, '제안자', 'failure', { code: 'model_feature_unavailable', message: 'Synthetic exact-model feature check unavailable.' }),
 event(6, 'agent', 9, '검토자', 'failure', { code: 'model_feature_unavailable', message: 'Synthetic exact-model feature check unavailable.' }),
]
if (previewScreen === 'chat' && (['tool-review-changed','tool-review-success','tool-review-failure'].includes(previewState) || previewState.startsWith('tool-review-retry-'))) {
  const trigger = 'f01a0000-0000-4000-8000-000000000001'
  const state = previewState === 'tool-review-success' ? 'completed' : previewState.startsWith('tool-review-retry-') ? 'denied' : 'failed'
  const code = previewState === 'tool-review-changed' || previewState.startsWith('tool-review-retry-') ? 'review_changed' : previewState === 'tool-review-failure' ? 'browser_unavailable' : undefined
  events = [
    { ...event(1, 'user', 1, '지민', 'message', { content: '브라우저에서 화면을 확인해 줘.' }), event_id: trigger },
    { ...event(2, 'tool', null, '도구', 'tool_result', { state, agent_id: 8, action_id: 'f01a0000-0000-4000-8000-000000000002', trigger_event_id: trigger, round: 1, ...(code ? {error_code: code} : {}), text: code === 'review_changed' ? 'Action review changed; a new review is required.' : state === 'completed' ? 'Browser check completed.' : 'Browser unavailable.' }), event_id: 'f01a0000-0000-4000-8000-000000000003' },
  ]
}
if (previewScreen === 'chat' && previewState.startsWith('reaction-')) {
  const root='e6ac0000-0000-4000-8000-000000000001',answer='e6ac0000-0000-4000-8000-000000000002'
  events=[{...event(1,'user',1,'지민','message',{content:'계획을 정리해 줘.'}),event_id:root},
    {...event(2,'agent',8,'기획','message',{content:'계획을 세 단계로 정리했어요.',trigger_event_id:root}),event_id:answer}]
  if(['reaction-set','reaction-clear'].includes(previewState))events.push({...event(3,'user',1,'지민','activity',{category:'message_reaction',version:1,target_event_id:answer,emoji:'👍',expected_seq:0}),event_id:'e6ac0000-0000-4000-8000-000000000003'})
}
if (previewScreen === 'chat' && previewState.startsWith('bot-reaction-')) {
  const id=(seq:number)=>`b07e0000-0000-4000-8000-${String(seq).padStart(12,'0')}`
  const root=id(1),peer=id(2)
  const react=(seq:number,agent:number,target:string,targetSeq:number,emoji:ReactionEmoji|null,expected=0):RoomEvent=>({
    ...event(seq,'agent',agent,agent===8?'기획':'검토','activity',{
      category:'bot_message_reaction',version:1,target_event_id:target,target_seq:targetSeq,emoji,expected_seq:expected,
      trigger_event_id:root,call_id:`call_reaction_${seq}`,generation:1,
    }),event_id:id(seq),
  })
  events=[{...event(1,'user',1,'지민','message',{content:'초안을 보고 서로 확인한 내용을 알려 주세요.'}),event_id:root},
    {...event(2,'agent',8,'기획','message',{content:'초안을 정리했어요. 검토할 준비가 됐습니다.',trigger_event_id:root}),event_id:peer}]
  if(previewState==='bot-reaction-malformed')events.push(
    {...react(3,8,root,1,'👍'),actor_type:'user',actor_id:1},
    {...react(4,8,root,1,'👍'),actor_type:'system',actor_id:null},
    react(5,999,root,1,'👍'),react(6,8,root,2,'👍'),react(7,9,root,1,'👀',3),
  )
  else {
    events.push(react(3,8,root,1,'👍'),react(4,9,root,1,'👍'),react(5,9,peer,2,'👀'))
    if(previewState==='bot-reaction-cleared')events.push(react(6,8,root,1,null,3))
    if(previewState==='bot-reaction-changed')events.push(react(6,8,root,1,'❤️',3))
    if(previewState==='bot-reaction-owner')events.push({...event(6,'user',1,'지민','activity',{category:'message_reaction',version:1,target_event_id:root,emoji:'😂',expected_seq:0}),event_id:id(6)})
  }
}
if (previewScreen === 'chat' && (previewState.startsWith('quote-reply-') || previewState.startsWith('persistent-reply-'))) {
  events=[{...event(1,'user',1,'지민','message',{content:'출시 계획을 검토해 줘.'}),event_id:'b33a0000-0000-4000-8000-000000000001'},
    {...event(2,'agent',8,'기획','message',{content:'설치 안내를 먼저 확인하고 위험한 변경은 승인 전에 멈춰야 합니다.',trigger_event_id:'b33a0000-0000-4000-8000-000000000001'}),event_id:'b33a0000-0000-4000-8000-000000000002'}]
}
if (previewScreen === 'chat' && previewState.startsWith('thread-')) {
  const root='d4400000-0000-4000-8000-000000000001'
  const count=previewState==='thread-empty'||previewState==='thread-error'?0:previewState==='thread-more'?22:previewState==='thread-depth-limit'?65:previewState==='thread-nested'?5:2
  const id=(seq:number)=>`d4400000-0000-4000-8000-${String(seq).padStart(12,'0')}`
  const chained=['thread-nested','thread-more','thread-depth-limit'].includes(previewState)
  events=[{...event(1,'user',1,'지민','message',{content:'출시 안내의 핵심을 확인해 주세요.'}),event_id:root},
    ...Array.from({length:count},(_,i)=>({...event(i+2,i%2?'agent':'user',i%2?8:1,i%2?'기획':'지민','message',{
      content:`합성 답장 ${i+1}: 확인 내용을 남깁니다.`,
      reply_to:chained && i>0 && !(previewState==='thread-nested'&&i===4)?{event_id:id(i+1),seq:i+1}:{event_id:root,seq:1},
    }),event_id:id(i+2)}))]
  if(previewState==='thread-nested')events.push({...event(7,'agent',9,'검토','message',{content:'다른 메시지의 답장: 이 모아보기에는 포함되지 않습니다.',reply_to:{event_id:id(99),seq:1}}),event_id:id(7)})
}
if (previewState === 'empty' && previewScreen === 'chat') events = []
if (previewState.startsWith('teammates-')) events = previewState === 'teammates-alone' ? [] : teammateEvents
const agents: Agent[] = [
  { id: 8, name: '기획', description: '기획 및 문서 작성', system_prompt: '', connection_id: 1, model_id: 'qwen3.7-plus', model: 'qwen3.7-plus', temperature: 'auto', max_tokens: 0, is_active: true },
  { id: 9, name: '검토', description: '내용 검토 및 품질 확인', system_prompt: '', connection_id: 2, model_id: 'claude-sonnet', model: 'claude-sonnet', temperature: 'auto', max_tokens: 0, is_active: true },
  { id: 10, name: '테스트', description: '실행 테스트 및 결과 확인', system_prompt: '', connection_id: 1, model_id: 'qwen3.7-plus', model: 'qwen3.7-plus', temperature: 'auto', max_tokens: 0, is_active: true },
  { id: 11, name: '대기', description: '다음 작업 대기', system_prompt: '', connection_id: 1, model_id: 'qwen3.7-plus', model: 'qwen3.7-plus', temperature: 'auto', max_tokens: 0, is_active: true },
  { id: 12, name: '조사', description: '오류 원인 조사', system_prompt: '', connection_id: 1, model_id: 'qwen3.7-plus', model: 'qwen3.7-plus', temperature: 'auto', max_tokens: 0, is_active: true },
].map(agent => ({ ...agent, user_id: 1, created_at: at(0), updated_at: null, avatar: avatarFromSeed(agent.id) }))
if(previewState==='quote-reply-ambiguous')agents[1].name=agents[0].name
// Cross-room histories stay contiguous; equal sequence numbers in different rooms remain distinct references.
const rootApproval = previewState.startsWith('root-approval-'), parallelRoot = previewState.startsWith('parallel-'), createRoot = previewState.startsWith('bot-create-')
const crossRoom = previewState.startsWith('bot-message-') || rootApproval || parallelRoot || createRoot, crossRetry = previewState.startsWith('bot-message-retry-')
const crossId = 'bc012000-0000-4000-8000-000000000001'
const crossRoot = 'bc012000-0000-4000-8000-000000000002'
const crossPhaseIds = { request: 'bc012000-0000-4000-8000-000000000003', delivery: 'bc012000-0000-4000-8000-000000000004',
  response: 'bc012000-0000-4000-8000-000000000005', return_delivery: 'bc012000-0000-4000-8000-000000000006', return: 'bc012000-0000-4000-8000-000000000007' }
const crossContent = '설치 안내에서 빠진 단계를 검토하고, 수정할 부분만 알려 줘. 다른 개인 대화 내용은 필요하지 않아.'
const crossHasAnswer = ['bot-message-answered', 'bot-message-returning', 'bot-message-completed', 'bot-message-incoming', 'bot-message-failed', 'bot-message-return-failed', 'bot-message-return-cancelled', 'bot-message-passed', 'bot-message-return-interrupted', 'bot-message-return-busy-timeout'].includes(previewState) || crossRetry
const crossHasReturn = ['bot-message-completed', 'bot-message-incoming', 'bot-message-return-failed'].includes(previewState) || crossRetry
const crossState: BotMessage['state'] = crossRetry ? 'failed' : previewState === 'bot-message-pending' ? 'pending'
  : ['bot-message-failed', 'bot-message-interrupted', 'bot-message-busy-timeout', 'bot-message-passed'].includes(previewState) ? 'failed'
    : previewState === 'bot-message-cancelled' ? 'cancelled' : crossHasAnswer ? 'completed' : 'running'
const crossReplyState: BotMessage['reply_state'] = crossRetry ? 'failed' : ['bot-message-return-failed', 'bot-message-return-interrupted', 'bot-message-return-busy-timeout'].includes(previewState) ? 'failed'
  : ['bot-message-cancelled', 'bot-message-return-cancelled'].includes(previewState) ? 'cancelled'
    : previewState === 'bot-message-returning' ? 'running' : crossHasReturn ? 'delivered' : crossHasAnswer ? 'pending'
      : previewState === 'bot-message-interrupted' ? 'failed' : 'none'
const crossEvent = (phase: keyof typeof crossPhaseIds, room: number, seq: number): RoomEvent => {
  const system = phase === 'delivery' || phase === 'return_delivery', response = phase === 'response'
  const failure = response && (previewState === 'bot-message-failed' || crossRetry) || phase === 'return' && (previewState === 'bot-message-return-failed' || crossRetry)
  const passed = response && previewState === 'bot-message-passed'
  return { ...event(seq, system ? 'system' : 'agent', system ? null : response ? 9 : 8, system ? '시스템' : response ? '검토' : '기획', system || passed ? 'activity' : failure ? 'failure' : 'message', {
    ...(system ? { category: 'bot_message' } : failure ? { code: 'provider_unavailable', message: 'Synthetic Bot connection unavailable.' }
      : passed ? { category: 'agent_passed' } : { content: phase === 'request' ? crossContent : response
        ? '기존 데이터 유지 안내가 빠져 있습니다. 설치 전에 백업 위치와 되돌리는 방법을 추가해 주세요.'
        : '검토가 지적한 백업 위치와 복구 절차를 반영해 출시 안내를 정리했습니다.' }),
    trigger_event_id: crossRoot, bot_message: { version: 1, message_id: crossId, phase, sender_agent_id: 8, recipient_agent_id: 9,
      root_room_id: 501, root_request_event_id: crossRoot },
  }), room_id: room, event_id: crossPhaseIds[phase] }
}
const crossRootEvent = { ...event(1, 'user', 1, '지민', 'message', { content: '@기획 출시 안내를 완성해 줘. 필요한 부분은 다른 Bot에게 부탁해.' }), event_id: crossRoot }
const crossRequest = crossEvent('request', 501, 2), crossDelivery = crossEvent('delivery', 701, 1)
const crossResult = crossHasAnswer ? crossEvent('response', 701, 2) : null
const crossReturnDelivery = crossHasAnswer ? crossEvent('return_delivery', 501, 3) : null
const crossReturn = crossHasReturn ? crossEvent('return', 501, 4) : null
const crossSourceEvents = [crossRootEvent, crossRequest, ...(crossReturnDelivery ? [crossReturnDelivery] : []), ...(crossReturn ? [crossReturn] : [])]
const crossResolved = previewState === 'bot-message-completed' ? { ...event(3, 'agent', 9, '검토', 'message', { content: '전문가 의견까지 반영한 최종 검토입니다.' }), room_id: 701, event_id: 'bc012000-0000-4000-8000-000000000008' } : null
const crossTargetEvents = [crossDelivery, ...(crossResult ? [crossResult] : []), ...(crossResolved ? [crossResolved] : [])]
const crossRef = (item: RoomEvent): BotMessageRef => ({ room_id: item.room_id, event_id: item.event_id, seq: item.seq, actor_type: item.actor_type, actor_id: item.actor_id, kind: item.kind })
const crossMessage: BotMessage = { version: 1, capabilities: { text_collaboration: true, delegated_os_execution: rootApproval }, message_id: crossId, root_request_event_id: crossRoot, parent_job_id: null, parent_kind: null,
  depth: 1, sender_agent_id: 8, sender_name: '기획', recipient_agent_id: 9, recipient_name: '검토', content: crossContent,
  state: crossState, reply_state: crossReplyState, source_room: { room_id: 501, title: '기획 · 개인 대화' }, target_room: { room_id: 701, title: '검토 · 개인 대화' },
  root_ref: crossRef(crossRootEvent), request_ref: crossRef(crossRequest), delivery_ref: crossRef(crossDelivery), result_ref: crossResult ? crossRef(crossResult) : null,
  resolved_result_ref: crossResolved ? crossRef(crossResolved) : crossReplyState === 'delivered' && crossResult?.kind === 'message' ? crossRef(crossResult) : null,
  return_delivery_ref: crossReturnDelivery ? crossRef(crossReturnDelivery) : null, return_ref: crossReturn ? crossRef(crossReturn) : null,
  failure_code: ['bot-message-interrupted', 'bot-message-return-interrupted'].includes(previewState) ? 'interrupted' : ['bot-message-busy-timeout', 'bot-message-return-busy-timeout'].includes(previewState) ? 'target_busy_timeout' : previewState === 'bot-message-passed' ? 'no_answer' : previewState === 'bot-message-failed' || previewState === 'bot-message-return-failed' || crossRetry ? 'provider_unavailable' : null,
  deadline_at: at(25), created_at: at(15), updated_at: at(18) }
let retryExpectedAt = crossMessage.updated_at, retryPlanHash = 'a'.repeat(64), retryChanged = previewState === 'bot-message-retry-changed'
let retryReadError = previewState === 'bot-message-retry-read-error', retryUnknown = previewState === 'bot-message-retry-unknown'
let activePreviewRoomId = 501
const retryReceipts = new Map<string, { digest: string; receipt: BotMessageRetryReceipt }>()
const retryFailure = (code: string, reason?: string) => Object.assign(new Error('Synthetic retry rejected'), { response: { status: 409, data: { detail: { code, reason } } } })
Object.assign(botMessageRetriesApi, {
  preview: async (message: BotMessage): Promise<BotMessageRetryPlan> => {
    if (retryReadError) { retryReadError = false; throw new Error('Synthetic retry read unavailable') }
    return { version: 1, message_id: message.message_id, root_ref: message.root_ref, expected_updated_at: retryExpectedAt,
      eligible: previewState !== 'bot-message-retry-blocked', blocked_reason: previewState === 'bot-message-retry-blocked' ? 'completed_effect' : null,
      source_agent_id: 8, cost: 'new_model_work', plan_hash: previewState === 'bot-message-retry-blocked' ? null : retryPlanHash }
  },
  start: async (plan: BotMessageRetryPlan, body: Readonly<{ expected_updated_at: string; plan_hash: string }>, key: string): Promise<BotMessageRetryReceipt> => {
    const digest = JSON.stringify([plan.message_id, body]), prior = retryReceipts.get(key)
    if (prior) { if (prior.digest !== digest) throw retryFailure('bot_message_retry_key_changed'); return { ...prior.receipt, replayed: true } }
    if (previewState === 'bot-message-retry-sending' || previewState === 'bot-message-retry-owner-switch') await new Promise(resolve => setTimeout(resolve, 1500))
    if (previewState === 'bot-message-retry-owner-switch') useAuthStore.getState().setAuth('synthetic-second-owner', { id: 2, email: 'second@example.invalid', username: '다른 사용자' })
    if (retryChanged) { retryChanged = false; retryExpectedAt = at(19); retryPlanHash = 'b'.repeat(64); throw retryFailure('bot_message_retry_changed') }
    if (body.expected_updated_at !== retryExpectedAt || body.plan_hash !== retryPlanHash) throw retryFailure('bot_message_retry_changed')
    if (previewState === 'bot-message-retry-blocked') throw retryFailure('bot_message_retry_blocked', 'completed_effect')
    const retryEvent: RoomEvent = { ...event(crossSourceEvents.length + 1, 'user', 1, '지민', 'message', {
      content: '기획에게 새 요청: 앞서 실패한 검토 작업을 다른 Bot에게 다시 부탁해 주세요.',
      bot_message_retry_of: { message_id: plan.message_id, original_root_ref: plan.root_ref, original_failure_ref: crossMessage.result_ref, source_agent_id: 8, plan_hash: body.plan_hash },
    }), event_id: 'bc012000-0000-4000-8000-000000000099' }
    const receipt: BotMessageRetryReceipt = { version: 1, message_id: plan.message_id, root_room_id: 501, retry_event_id: retryEvent.event_id,
      retry_seq: retryEvent.seq, href: `/chat/501?seq=${retryEvent.seq}`, source_agent_id: 8, root_ref: plan.root_ref, replayed: false, cost: 'new_model_work', plan_hash: body.plan_hash }
    crossSourceEvents.push(retryEvent); retryReceipts.set(key, { digest, receipt })
    if (activePreviewRoomId === 501 && previewState !== 'bot-message-retry-owner-switch') previewEmitRoomEvent(retryEvent as unknown as Record<string, unknown>)
    if (retryUnknown) { retryUnknown = false; throw new Error('Synthetic acceptance receipt lost') }
    return receipt
  },
})
if (crossRoom) events = crossSourceEvents
Object.assign(botCreationsApi, { list: async (id: number) => ({ version: 1 as const, room_id: id, creations: [], creations_truncated: false }), read: async () => { throw new Error('Synthetic creation receipt absent') } })
Object.assign(botBatchesApi, { list: async (id: number) => ({ version: 1 as const, room_id: id, batches: [], batches_truncated: false }) })
Object.assign(botMessagesApi, { list: async (id: number) => {
  if (previewState === 'bot-message-unknown') throw new Error('Synthetic cross-room status unavailable')
  return { version: 1 as const, room_id: id, capabilities: { text_collaboration: true as const, delegated_os_execution: rootApproval }, bots: agents.map(agent => ({ agent_id: agent.id, name: agent.name, description: agent.description, model_id: agent.model_id, avatar: agent.avatar })),
    messages: crossRoom && [501, 701].includes(id) ? [{ ...crossMessage, updated_at: retryExpectedAt }] : [], messages_truncated: false }
} })
Object.assign(teammatesApi, { list: async (id: number) => {
  if (previewState === 'teammates-unknown') throw new Error('Synthetic teammate status unavailable')
  return { version: 1 as const, room_id: id, teammates: agents.slice(0, previewState === 'teammates-alone' ? 1 : 2)
    .map(agent => ({ agent_id: agent.id, name: agent.name, description: agent.description, model_id: agent.model_id })),
    handoffs: previewState.startsWith('teammates-') && previewState !== 'teammates-alone' ? [teammateHandoff] : [], handoffs_truncated: false }
} })
if (previewState === 'duplicate-failure') { agents.splice(2); agents[0].name = '제안자'; agents[1].name = '검토자' }
const starterAgents = [
  { id: 18, name: '제안자', description: '첫 협업 제안 역할', system_prompt: '', connection_id: 1, model_id: 'qwen3.7-plus', model: 'qwen3.7-plus', temperature: 'auto', max_tokens: 0, is_active: true },
  { id: 19, name: '검토자', description: '첫 협업 검토 역할', system_prompt: '', connection_id: 1, model_id: 'qwen3.7-plus', model: 'qwen3.7-plus', temperature: 'auto', max_tokens: 0, is_active: true },
]
let appliedStarterAgents: typeof starterAgents = []
const connection = {
  id: 1, owner_id: 1, provider_kind: 'compatible' as const, provider_id: 'compatible', protocol_id: 'openai_chat_completions' as const, auth_method_id: 'api_key' as const, label: 'Alibaba Coding Plan',
  base_url: 'https://api.example.invalid/v1', default_model_id: 'qwen3.7-plus', enabled: true,
  auth_required: true, has_secret: true, is_local: false,
  capabilities: {
    declared: { max_tokens: true, temperature: true, tools: true, vision: true },
    verified: { models: { 'qwen3.7-plus': { max_tokens: true, temperature: true, tools: true, vision: true } } },
  },
}
const reviewConnection = {
  id: 2, owner_id: 1, provider_kind: 'anthropic' as const, provider_id: 'anthropic', protocol_id: 'anthropic_messages' as const, auth_method_id: 'api_key' as const, label: 'Anthropic Review',
  base_url: null, default_model_id: 'claude-sonnet', enabled: true,
  auth_required: true, has_secret: true, is_local: false,
  capabilities: { declared: { tools: true }, verified: { models: {} } },
}
const previewBlueprint: WorkspaceBlueprint = {
  version: 1,
  connections: [{ ref: 'connection-1', provider_kind: 'compatible', label: 'Alibaba Coding Plan', base_url: 'https://api.example.invalid/v1',
    default_model_id: 'qwen3.7-plus', allow_local_http: false, auth_required: true, enabled: true,
    capabilities: { max_tokens: true, temperature: true, tools: true, vision: true } }],
  agents: [{ ref: 'agent-1', name: '가져온 기획', description: '가져온 문서 기획 역할', system_prompt: '명확한 기획안을 작성합니다.',
    connection_ref: 'connection-1', model_id: 'qwen3.7-plus', temperature: 'auto', max_tokens: 0 }],
  projects: [{ ref: 'project-1', name: '데스크톱 협업', description: '가져온 작업 공간', agent_refs: ['agent-1'], room_templates: [{
    ref: 'template-1', name: '릴리스 팀', room_title: '가져온 릴리스 점검', agent_refs: ['agent-1'],
    max_rounds: 3, max_tool_calls: 12, max_executor_seconds: 180,
  }] }],
}
let importedConnection: (Omit<typeof connection, 'capabilities'> & { capabilities: { declared: typeof connection.capabilities.declared; verified: { models: Record<string, typeof connection.capabilities.verified.models['qwen3.7-plus']> } } }) | null = null
let importedAgent: typeof agents[number] | null = null
let importedProject: { id: number; user_id: number; name: string; description: string; is_active: boolean; agents: typeof agents } | null = null
let primaryProject = { id: 40, user_id: 1, name: '데스크톱 협업', description: '설치와 출시 작업', is_active: previewState !== 'archive-legacy',
  archived_at: previewState === 'archive-legacy' ? at(20) : null as string | null, created_at: at(0), updated_at: at(21), agents }
let appliedStarterProject: typeof primaryProject | null = null
const navigationProject = { id: 42, user_id: 1, name: '보관된 조사', description: '이전 조사 기록', is_active: false,
  archived_at: at(18), created_at: at(0), updated_at: at(18), agents: agents.slice(0, 2) }
const layoutProjects = previewState === 'projects-uneven' ? [
  { ...primaryProject, name: '방이 적은 프로젝트', agents: agents.slice(0, 1) },
  { ...primaryProject, id: 43, name: '방이 많은 프로젝트', description: '프로젝트마다 자기 내용만큼 높이를 사용합니다.', agents },
  { ...primaryProject, id: 44, name: '빈 프로젝트와아주긴이름'.repeat(6), description: 'https://example.invalid/'.repeat(12), agents: [] },
] : []
let archivePreviewVersion = 0

const pendingAction = { action_id: '4d1e0e6c-9eeb-4571-864b-f2592c44db38', room_id: roomId, agent_id: 9,
  trigger_event_id: 'synthetic-event-3', family: 'browser_navigate', target: 'http://127.0.0.1:4173',
  expected_effect: 'Navigate browser', state: 'waiting', scope: 'once', grant_id: null, requested_at: at(18),
  started_at: null, completed_at: null }
const completedAction = { action_id: screenshotAction, room_id: roomId, agent_id: 8,
  trigger_event_id: 'synthetic-event-5', family: 'screenshot', target: 'desktop:screen',
  expected_effect: 'Capture screenshot', state: 'completed', scope: 'once', grant_id: null, requested_at: at(20),
  started_at: at(20), completed_at: at(21) }
const runningAction = { action_id: '5d1e0e6c-9eeb-4571-864b-f2592c44db38', room_id: roomId, agent_id: 10,
  trigger_event_id: 'synthetic-event-4', family: 'read_file', target: 'docs/install-guide.md',
  expected_effect: 'Read the installation guide', state: 'started', scope: 'once', grant_id: null, requested_at: at(19),
  started_at: at(19), completed_at: null }
const uncertainAction = { ...runningAction, action_id: '6d1e0e6c-9eeb-4571-864b-f2592c44db38', state: 'uncertain',
  target: 'docs/release-note.md', expected_effect: 'Update the release note' }
type PreviewRoom = { id: number; user_id: number; project_id: number; title: string; is_active: boolean; created_at: string; updated_at: string }
const previewRooms = new Map<number, PreviewRoom>([
  [501, { id: 501, user_id: 1, project_id: 40, title: previewState === 'closed' ? '닫힌 출시 준비실' : '출시 준비실',
    is_active: previewState !== 'closed' && previewState !== 'inspector-archived', created_at: at(0), updated_at: at(21) }],
  [502, { id: 502, user_id: 1, project_id: 40, title: '완료된 설치 가이드', is_active: false, created_at: at(0), updated_at: at(19) }],
])
if (crossRoom) {
  previewRooms.get(501)!.title = crossMessage.source_room.title
  previewRooms.set(701, { id: 701, user_id: 1, project_id: 40, title: crossMessage.target_room.title, is_active: true, created_at: at(0), updated_at: at(21) })
}
if (previewState === 'projects-uneven') {
  for (let index = 0; index < 15; index += 1) previewRooms.set(600 + index, {
    id: 600 + index, user_id: 1, project_id: 43, title: `합성 대화방 ${index + 1}`, is_active: true,
    created_at: at(0), updated_at: at(21),
  })
}
if (previewState === 'first-success-resume') previewRooms.set(583, {
  id: 583, user_id: 1, project_id: 83, title: '첫 협업 결정', is_active: true, created_at: at(32), updated_at: at(32),
})
if (previewState === 'switcher') {
  previewRooms.set(503, { id: 503, user_id: 1, project_id: 40, title: '키보드 탐색 점검', is_active: true, created_at: at(1), updated_at: at(27) })
  previewRooms.set(504, { id: 504, user_id: 1, project_id: 40, title: '완료된 회고', is_active: false, created_at: at(1), updated_at: at(25) })
  previewRooms.set(505, { id: 505, user_id: 1, project_id: 42, title: '보관 프로젝트의 이전 방', is_active: true, created_at: at(1), updated_at: at(23) })
}
if (previewState === 'workload') {
  previewRooms.set(503, { id: 503, user_id: 1, project_id: 40, title: '문서 병렬 검토', is_active: true, created_at: at(1), updated_at: at(27) })
  previewRooms.set(504, { id: 504, user_id: 1, project_id: 40, title: '설치 확인', is_active: true, created_at: at(1), updated_at: at(25) })
}
let previewTemplates: RoomTemplate[] = [{ id: 71, user_id: 1, project_id: 40, name: '릴리스 팀', room_title: '릴리스 점검',
  agent_ids: [8, 9, 10, 11, 12], max_rounds: 3, max_tool_calls: 12, max_executor_seconds: 180,
  created_at: at(20), updated_at: at(20) }]
let templateRoomMembers: number[] = []
let templateRoomSchedule: RoomSchedule | null = null
const launchedRoomMembers = new Map<number, number[]>()
const launchedRoomSchedules = new Map<number, RoomSchedule>()
const personalRoomAgents = new Map<number, number>()
let primaryBotId: number | null = ['primary-unset', 'primary-empty', 'empty', 'setup', 'checkin-no-primary','memory-import-primary-unset'].includes(previewState) ? null : 8
let primaryBotRevision = 1, primaryConflict = previewState === 'primary-conflict'
let primarySaveUnknown = previewState === 'primary-save-unknown'
const personalRooms = new Map<number, number>()
if (crossRoom) { personalRooms.set(8, 501); personalRoomAgents.set(501, 8); personalRooms.set(9, 701); personalRoomAgents.set(701, 9) }
if (previewState.startsWith('memory-import-') && previewState !== 'memory-import-primary-unset') { personalRooms.set(8, 501); personalRoomAgents.set(501, 8); launchedRoomMembers.set(501, [8]) }
if (previewState.startsWith('routine-') && previewState!=='routine-chat-group') { personalRooms.set(8, 501); personalRoomAgents.set(501, 8); launchedRoomMembers.set(501, [8]) }
if(previewState==='routine-chat-delayed'){const base=previewRooms.get(501);if(base)previewRooms.set(503,{...base,id:503,title:'다른 합성 방'});launchedRoomMembers.set(503,[8])}
if(previewState==='routine-chat-group'){personalRooms.set(8,503);personalRoomAgents.set(503,8);launchedRoomMembers.set(501,[8,9]);launchedRoomMembers.set(503,[8]);const base=previewRooms.get(501);if(base)previewRooms.set(503,{...base,id:503,title:'합성 개인 대화'})}
if (previewState.startsWith('checkin-') && previewState !== 'checkin-no-primary') { personalRooms.set(8, 501); personalRoomAgents.set(501, 8); launchedRoomMembers.set(501, [8]) }
if (previewState.startsWith('memory-sharing-chat')) {
  const id = (n: number) => `af039100-0000-4000-8000-${String(n).padStart(12,'0')}`
  const root: RoomEvent = { ...event(1,'user',1,'지민','message',{content:'내 시간대가 서울이라는 걸 내 Bot들이 함께 기억해 줘.'}),event_id:id(1) }
  const meta = {version:1 as const,receipt_id:'b'.repeat(64),scope:'owner_common' as const,operation:'add' as const,state:'saved' as const,agent_id:8,root_request_event_id:root.event_id,content:'Asia/Seoul',memory_id:id(3),revision:1,entry_revision:1,effective:'next_request' as const,history_retained:false,kind:'timezone' as const,origin:{agent_id:8,source_kind:'event',room_id:501,event_id:root.event_id,seq:1}}
  const result: RoomEvent = {...event(2,'system',null,'시스템','activity',{category:'owner_common_memory',status:'saved',trigger_event_id:root.event_id,bot_persistence:meta}),event_id:id(2)}
  events = [root,result,{...event(3,'agent',8,'기획','message',{content:'알겠어요. 내 Bot들이 함께 참고할 시간대로 기억할게요.'}),event_id:id(4)}]
  launchedRoomMembers.set(501,[8,9])
  const receipt = {...meta,root_ref:crossRef(root),result_ref:crossRef(result),replayed:true}
  memorySharingApi.readChange = async () => { if (previewState === 'memory-sharing-chat-unknown') throw new Error('Synthetic receipt unavailable'); return structuredClone(previewState === 'memory-sharing-chat-mismatch' ? {...receipt,content:'다른 내용'} : receipt) }
}
if (previewState === 'primary-closed') { personalRooms.set(8, 501); personalRoomAgents.set(501, 8); previewRooms.get(501)!.is_active = false }
Object.assign(primaryCheckinsApi, { get: async () => { throw new Error('Synthetic check-in unavailable') }, runs: async () => { throw new Error('Synthetic check-in history unavailable') }, put: async () => { throw new Error('Synthetic check-in write unavailable') } })
if (previewState.startsWith('checkin-')) {
  const enabled = ['checkin-on','checkin-failed','checkin-cancelled','checkin-changed-primary'].includes(previewState)
  const changed = previewState === 'checkin-changed-primary'
  let config: PrimaryCheckin = { version: 1, revision: 1, primary_revision: 1, enabled, prompt: '안부를 묻고 지금 해야 할 일이 있는지 확인해 줘.', interval_minutes: 60,
    next_run_at: enabled && !changed ? '2026-10-29T09:00:00Z' : null, last_run_id: null, last_state: null,
    primary_agent_id: previewState === 'checkin-no-primary' ? null : changed ? 9 : 8,
    availability: previewState === 'checkin-no-primary' ? 'unset' : changed ? 'primary_changed' : enabled ? 'available' : 'disabled' }
  const id = (n: number) => `af038000-0000-4000-8000-${String(n).padStart(12, '0')}`
  const state = previewState === 'checkin-failed' ? 'failed' : previewState === 'checkin-cancelled' ? 'cancelled' : 'completed'
  const root: RoomEvent = { ...event(1,'system',null,'시스템','activity',{ category:'primary_checkin',status:'started', content:'안부를 묻고 지금 해야 할 일이 있는지 확인해 줘.',checkin:{version:1,run_id:id(2),scheduled_at:'2026-10-29T08:00:00Z'} }),event_id:id(1) }
  const reply: RoomEvent = { ...event(2,'agent',8,'기획','message',{content:'안녕하세요. 지금 함께 확인할 일이 있나요?',checkin:{version:1,run_id:id(2)}}),event_id:id(3) }
  const row: CheckinRun = {run_id:id(2),state,scheduled_at:'2026-10-29T08:00:00Z',started_at:'2026-10-29T08:00:01Z',completed_at:'2026-10-29T08:00:05Z',root_ref:crossRef(root),result_ref:state === 'completed' ? crossRef(reply) : null,failure_code:state === 'failed' ? 'synthetic' : null,primary_agent_id:8,room_id:501,connection_id:1,model_id:'synthetic'}
  let firstUnknown = previewState === 'checkin-unknown', firstConflict = previewState === 'checkin-conflict'
  primaryCheckinsApi.get = async () => structuredClone(config)
  primaryCheckinsApi.runs = async () => ({version:1,runs: enabled && !changed ? [structuredClone(row)] : []})
  primaryCheckinsApi.put = async body => {
    if (previewState === 'checkin-saving') await new Promise(resolve => setTimeout(resolve, 4000))
    if (firstConflict) { firstConflict = false; config = {...config,revision:config.revision+1}; throw Object.assign(new Error('Synthetic conflict'), {response:{status:409}}) }
    config = {...config, revision: config.revision+1, enabled:body.enabled, prompt:body.prompt,interval_minutes:body.interval_minutes,primary_agent_id:8,availability:body.enabled?'available':'disabled', next_run_at:body.enabled?'2026-10-29T09:00:00Z':null}
    if (firstUnknown) {firstUnknown = false; throw new Error('Synthetic response lost')}
    return structuredClone(config)
  }
  events = enabled && !changed ? [root, ...(state === 'completed' ? [reply] : [])] : []
}
const primaryBotFixture = (): PrimaryBot => {
  const chosen = useAuthStore.getState().user?.id === 2 ? undefined : agents.find(agent => agent.id === primaryBotId)
  const personalRoomId = primaryBotId ? personalRooms.get(primaryBotId) : undefined
  const room = personalRoomId ? previewRooms.get(personalRoomId) : undefined
  return { version: 1, revision: primaryBotRevision, agent_id: chosen?.id || null,
    agent: chosen ? { id: chosen.id, name: chosen.name, description: chosen.description, model_id: chosen.model_id, avatar: chosen.avatar, is_active: chosen.is_active } : null,
    conversation: room ? { room_id: room.id, project_id: room.project_id, href: `/chat/${room.id}`, active: room.is_active } : null,
    availability: !chosen ? 'unset' : room && !room.is_active ? 'room_inactive' : 'available' }
}
Object.assign(botsApi, {
  metadata: async (id: number) => {
    if (previewState === 'bot-membership-error') throw new Error('Synthetic Bot identity unavailable')
    const personalAgentId = personalRoomAgents.get(id) || null
    return { version: 1 as const, room_id: id, personal_agent_id: personalAgentId, can_change_members: personalAgentId === null }
  },
  primary: async () => { if (previewState === 'primary-error') throw new Error('Synthetic primary Bot read failure'); return primaryBotFixture() },
  setPrimary: async (id: number | null, revision: number) => {
    if (previewState === 'primary-saving') await new Promise(resolve => setTimeout(resolve, 1500))
    if (primaryConflict) { primaryConflict = false; primaryBotId = 9; primaryBotRevision += 1 }
    if (revision !== primaryBotRevision) throw Object.assign(new Error('Synthetic selection changed'), { response: { status: 409, data: { detail: { code: 'bot_primary_changed', current_revision: primaryBotRevision } } } })
    primaryBotId = id; primaryBotRevision += 1
    if (primarySaveUnknown) { primarySaveUnknown = false; throw new Error('Synthetic lost selection receipt') }
    return primaryBotFixture()
  },
  conversation: async (id: number, replaceClosedRoomId?: number) => {
    if (previewState === 'primary-owner-switch') {
      await new Promise(resolve => setTimeout(resolve, 1000))
      useAuthStore.getState().setAuth('synthetic-second-owner-token', { id: 2, email: 'second-owner@example.invalid', username: '새 사용자' })
    }
    if (previewState === 'primary-open-error') throw new Error('Synthetic Bot conversation failure')
    const before = personalRooms.get(id), previous = before ? previewRooms.get(before) : undefined
    if (previous && !previous.is_active && replaceClosedRoomId !== previous.id) throw Object.assign(new Error('Synthetic closed Bot conversation'), {
      response: { status: 409, data: { detail: { code: 'bot_conversation_unavailable', reason: 'room_inactive', room_id: previous.id, project_id: previous.project_id, href: `/chat/${previous.id}` } } },
    })
    const created = !previous || !previous.is_active
    const selected = created ? 700 + id : previous.id
    if (created) {
      previewRooms.set(selected, { id: selected, user_id: 1, project_id: 40, title: `${agents.find(agent => agent.id === id)?.name || 'Bot'} · 개인 대화`, is_active: true, created_at: at(30), updated_at: at(30) })
      personalRooms.set(id, selected); personalRoomAgents.set(selected, id); launchedRoomMembers.set(selected, [id])
    }
    return { version: 1 as const, agent_id: id, room_id: selected, project_id: 40, href: `/chat/${selected}`, created, replayed: !created }
  },
})
if(previewState.startsWith('memory-import-')) {
 let receipt:MemoryImportReceipt|null=null,first=true,originalKey='',originalBody:MemoryImportRequest|null=null
 memoryImportApi.submit=async(body,key)=>{
  if(receipt&&key===originalKey&&JSON.stringify(body)===JSON.stringify(originalBody))return {...receipt,replayed:true}
  if(previewState==='memory-import-conflict'&&first){first=false;primaryBotRevision++;throw {response:{status:409,data:{detail:{code:'bot_primary_changed'}}}}}
  if(body.expected_primary_revision!==primaryBotRevision||body.expected_agent_id!==primaryBotId)throw {response:{status:409}}
  const eventId='ac100000-0000-4000-8000-000000000001',seq=1,digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(body.content))),n=>n.toString(16).padStart(2,'0')).join('')
  const row:RoomEvent={...event(seq,'user',1,'지민','message',{content:`${importSourcePrefix}${body.content}${importSourceSuffix}`,memory_import:{version:1,agent_id:body.expected_agent_id,source_label:body.source_label,content_sha256:digest}}),event_id:eventId}
  events=[row];receipt={version:1,agent_id:8,room_id:501,href:`/chat/501?seq=${seq}&event=${eventId}`,event_id:eventId,seq,replayed:false,status:'submitted'};originalKey=key;originalBody=structuredClone(body)
  if(previewState==='memory-import-unknown'&&first){first=false;throw Error('Synthetic response lost')}
  return structuredClone(receipt)
 }
}
const memoryBase = { agent_id: 8, group_allowed: true, scope: 'bot_private' as const, authority: 'context_only' as const,
  source_kind: 'manual' as const, source_room_id: null, source_event_id: null, source_seq: null, source_url: null,
  source_snapshot: {}, revision: 1, created_by: 1, updated_by: 1, created_at: at(20), updated_at: at(20), deleted_at: null }
let botMemoryEntries: BotMemoryEntry[] = previewState === 'bot-memory-empty' ? [] : [
  { ...memoryBase, memory_id: '8ad91000-0000-4000-8000-000000000001', category: 'preference', content: '답변은 한국어로, 핵심부터 짧게 알려 주세요.' },
  { ...memoryBase, memory_id: '8ad91000-0000-4000-8000-000000000002', category: 'role', content: '출시 준비의 담당자로 남은 작업과 다음 행동을 정리합니다.' },
]
if(previewState==='memory-import-provenance') {
 const importedId='ac100000-0000-4000-8000-000000000002'
 events=[{...event(1,'user',1,'지민','message',{content:'합성 가져오기 요청'}),event_id:importedId}]
 botMemoryEntries=[{...botMemoryEntries[0],source_kind:'event',source_room_id:501,source_event_id:importedId,source_seq:1,group_allowed:false,source_snapshot:{memory_import:{version:1,agent_id:8,source_label:'예시 AI',content_sha256:'a'.repeat(64)}},updated_at:at(10)},{...botMemoryEntries[1],updated_at:at(30)}]
}
let botMemoryRevision = botMemoryEntries.length, nextBotMemoryId = 3
let memoryUnknown = previewState === 'bot-memory-unknown', memoryChanged = previewState === 'bot-memory-changed'
const botMemoryReceipts = new Map<string, { digest: string; receipt: BotMemoryReceipt }>()
const memoryFailure = (code: string, status = 409) => Object.assign(new Error('Synthetic Bot memory failure'), { response: { status, data: { detail: { code } } } })
const mutateBotMemory = async (kind: 'add' | 'edit' | 'remove', agent: number, id: string | null,
  body: BotMemoryWrite | BotMemoryEdit | BotMemoryDelete, key: string) => {
  const digest = JSON.stringify([kind, agent, id, body]), prior = botMemoryReceipts.get(key)
  if (prior) { if (prior.digest !== digest) throw memoryFailure('bot_memory_idempotency_conflict'); return { ...prior.receipt, replayed: true } }
  if (previewState === 'bot-memory-saving') await new Promise(resolve => setTimeout(resolve, 1500))
  if (previewState === 'bot-memory-busy') throw memoryFailure('bot_memory_busy')
  if (memoryChanged) { memoryChanged = false; botMemoryRevision += 1 }
  if (body.expected_scope_revision !== botMemoryRevision) throw memoryFailure('bot_memory_revision_conflict')
  let row = id ? botMemoryEntries.find(entry => entry.memory_id === id) : undefined
  if (kind !== 'add' && (!row || row.revision !== (body as BotMemoryEdit).expected_revision)) throw memoryFailure('bot_memory_revision_conflict')
  const memoryId = id || `8ad91000-0000-4000-8000-${String(nextBotMemoryId++).padStart(12, '0')}`
  if (kind === 'add') { row = { ...memoryBase, agent_id: agent, memory_id: memoryId,
    category: (body as BotMemoryWrite).category, content: (body as BotMemoryWrite).content }; botMemoryEntries = [...botMemoryEntries, row] }
  else if (kind === 'edit') { row = { ...row!, category: (body as BotMemoryEdit).category, content: (body as BotMemoryEdit).content, revision: row!.revision + 1, updated_at: at(35) }; botMemoryEntries = botMemoryEntries.map(entry => entry.memory_id === id ? row! : entry) }
  else botMemoryEntries = botMemoryEntries.filter(entry => entry.memory_id !== id)
  botMemoryRevision += 1
  const receipt: BotMemoryReceipt = { agent_id: agent, memory_id: memoryId, scope: 'bot_private', revision: botMemoryRevision,
    entry_revision: kind === 'remove' ? row!.revision + 1 : row!.revision, replayed: false, personal_room_id: 501, activity_event_id: null, activity_seq: null }
  botMemoryReceipts.set(key, { digest, receipt })
  if (memoryUnknown) { memoryUnknown = false; throw new Error('Synthetic lost Bot memory receipt') }
  return receipt
}
Object.assign(botMemoriesApi, {
  list: async (agent: number) => {
    if (previewState === 'bot-memory-read-error') throw new Error('Synthetic Bot memory read failure')
    return { agent_id: agent, scope: 'bot_private' as const, authority: 'context_only' as const,
      revision: agent === 8 ? botMemoryRevision : 0, personal_room_id: personalRooms.get(agent) || null, items: agent === 8 ? botMemoryEntries : [] }
  },
  add: async (agent: number, body: BotMemoryWrite, key: string) => mutateBotMemory('add', agent, null, body, key),
  edit: async (agent: number, id: string, body: BotMemoryEdit, key: string) => mutateBotMemory('edit', agent, id, body, key),
  remove: async (agent: number, id: string, body: BotMemoryDelete, key: string) => mutateBotMemory('remove', agent, id, body, key),
})
if (previewState.startsWith('chat-persistence-')) {
  personalRooms.set(8, 501); personalRoomAgents.set(501, 8); launchedRoomMembers.set(501, [8])
  previewRooms.get(501)!.title = '기획 · 개인 대화'
  const id = (n: number) => `af037000-0000-4000-8000-${String(n).padStart(12, '0')}`
  const operation: BotPersistenceReceipt['operation'] = previewState === 'chat-persistence-updated' ? 'edit' : previewState === 'chat-persistence-forgotten' ? 'delete' : ['chat-persistence-role', 'chat-persistence-role-busy'].includes(previewState) ? 'role' : 'add'
  const content = operation === 'role' ? '답변할 때 근거와 불확실성을 구분해 설명해 주세요.' : '회의 요약은 결정 사항과 다음 행동부터 정리해 주세요.'
  const root = { ...event(1, 'user', 1, '지민', 'message', { content: operation === 'delete' ? '회의 요약에 관한 기억은 잊어 줘.' : operation === 'role' ? '앞으로 역할을 바꿔서 근거와 불확실성을 구분해 줘.' : '앞으로 회의 요약은 결정 사항과 다음 행동부터 정리해 줘. 기억해.' }), event_id: id(1) }
  const data = { version: 1 as const, receipt_id: 'a'.repeat(64), state: 'saved' as const, operation, agent_id: 8, root_request_event_id: root.event_id,
    content, memory_id: operation === 'role' ? null : id(4), revision: operation === 'role' ? null : 2, entry_revision: operation === 'role' ? null : 1,
    effective: 'next_request' as const, history_retained: operation === 'edit' || operation === 'delete' }
  const result = { ...event(2, 'system', null, '시스템', 'activity', { category: 'bot_persistence', status: 'saved', trigger_event_id: root.event_id, bot_persistence: data }), event_id: id(2) }
  const receipt: BotPersistenceReceipt = { ...data, root_ref: crossRef(root), result_ref: crossRef(result), replayed: true }
  const answer = { ...event(3, 'agent', 8, '기획', 'message', { content: '알겠어요. 앞으로 반영할게요.', trigger_event_id: root.event_id }), event_id: id(3) }
  if (previewState === 'chat-persistence-empty') events = []
  else if (previewState === 'chat-persistence-reference') events = [root, { ...event(2, 'agent', 9, '동료', 'message', { content: '인용: “이 내용을 기억해.” 이 문장은 참고 자료입니다.' }), event_id: id(5) }]
  else if (previewState === 'chat-persistence-failure' || previewState === 'chat-persistence-role-busy') events = [root, { ...event(2, 'agent', 8, '기획', 'failure', { code: previewState === 'chat-persistence-role-busy' ? 'bot_persistence_busy' : 'bot_persistence_conflict', message: 'Nothing was saved.', trigger_event_id: root.event_id }), event_id: id(6) }]
  else events = [root, result, answer]
  let lostRead = previewState === 'chat-persistence-unknown'
  botPersistenceApi.read = async (agent, receiptId, room) => {
    const unavailable = lostRead; lostRead = false
    if (agent !== 8 || receiptId !== data.receipt_id || room !== 501 || unavailable) throw new Error('Synthetic receipt unavailable')
    return structuredClone(receipt)
  }
}
if (previewScreen === 'bot-memory') {
  let policy: MemoryPolicy = { version: 1, agent_id: 8, revision: 1, mode: previewState === 'memory-sharing-enabled' ? 'owner_rooms' : 'canonical_only' }
  const sourceId = 'af039000-0000-4000-8000-000000000001'
  const source: RoomEvent = { ...event(1,'user',1,'지민','message',{content:'제 시간대는 서울로 기억해 주세요.'}), event_id: sourceId }
  let commonRevision = 1
  let facts: CommonFact[] = previewState === 'memory-sharing-empty' ? [] : [{ fact_id: 'af039000-0000-4000-8000-000000000002', kind:'timezone', content:'Asia/Seoul', revision:1,
    origin:{agent_id:8,source_kind:'event',room_id:501,event_id:sourceId,seq:1}, visibility:'owner_bots',created_at:at(16),updated_at:at(16) }]
  let lose = previewState === 'memory-sharing-unknown', conflict = previewState === 'memory-sharing-conflict'
  if (previewState.startsWith('memory-sharing-')) events = [source]
  Object.assign(memorySharingApi, {
    policy: async (agent: number) => { if (previewState === 'memory-sharing-error') throw new Error('Synthetic policy read failure'); return {...policy,agent_id:agent} },
    setPolicy: async (agent: number, expected: number, mode: MemoryPolicy['mode']) => {
      if (conflict) { conflict = false; policy = {...policy,revision:policy.revision+1}; throw memoryFailure('revision_conflict') }
      if (expected !== policy.revision) throw memoryFailure('revision_conflict')
      policy = {...policy,agent_id:agent,revision:policy.revision+1,mode}; if (lose) {lose=false; throw new Error('Synthetic response lost')}; return structuredClone(policy)
    },
    visibility: async (agent: number, memoryId: string, body: { expected_scope_revision: number; expected_revision: number; group_allowed: boolean }) => {
      const row = botMemoryEntries.find(x=>x.memory_id === memoryId)
      if (!row || body.expected_scope_revision !== botMemoryRevision || body.expected_revision !== row.revision) throw memoryFailure('revision_conflict')
      botMemoryRevision++; botMemoryEntries = botMemoryEntries.map(x => x.memory_id === memoryId ? {...x,revision:x.revision+1,group_allowed:body.group_allowed} : x)
      if (lose) {lose=false; throw new Error('Synthetic response lost')}
      return {agent_id:agent,memory_id:memoryId,scope:'bot_private',revision:botMemoryRevision,entry_revision:row.revision+1,replayed:false,personal_room_id:501,activity_event_id:null,activity_seq:null}
    },
    common: async () => { if (previewState === 'memory-sharing-error') throw new Error('Synthetic common read failure'); return {version:1,scope:'owner_common',revision:commonRevision,items:structuredClone(facts)} },
    addCommon: async (agent: number, revision: number, kind: CommonFact['kind'], content: string) => {
      if (revision !== commonRevision) throw memoryFailure('revision_conflict')
      commonRevision++; const fact: CommonFact = {fact_id:`af039000-0000-4000-8000-${String(10+commonRevision).padStart(12,'0')}`,kind,content,revision:1,origin:{agent_id:agent,source_kind:'manual',room_id:null,event_id:null,seq:null},visibility:'owner_bots',created_at:at(20),updated_at:at(20)}; facts=[...facts,fact]
      if (lose) {lose=false; throw new Error('Synthetic response lost')}; return {version:1,scope:'owner_common',fact_id:fact.fact_id,kind,revision:commonRevision,entry_revision:1,origin:fact.origin,operation:'add',root_ref:null,result_ref:null,replayed:false}
    },
    changeCommon: async (fact: CommonFact, revision: number, content: string | null): Promise<CommonReceipt> => {
      if (revision !== commonRevision || !facts.some(x => x.fact_id===fact.fact_id && x.revision===fact.revision)) throw memoryFailure('revision_conflict')
      commonRevision++; facts=content===null ? facts.filter(x=>x.fact_id!==fact.fact_id) : facts.map(x=>x.fact_id===fact.fact_id?{...x,content,revision:x.revision+1}:x)
      if (lose) {lose=false; throw new Error('Synthetic response lost')}; return {version:1,scope:'owner_common',fact_id:fact.fact_id,kind:fact.kind,revision:commonRevision,entry_revision:fact.revision+1,origin:fact.origin,operation:content===null?'delete':'edit',root_ref:null,result_ref:null,replayed:false}
    },
  })
}
if (previewScreen === 'bot-memory' || previewState === 'bot-personal') {
  personalRooms.set(8, 501); personalRoomAgents.set(501, 8); launchedRoomMembers.set(501, [8])
  previewRooms.get(501)!.title = '기획 · 개인 대화'
}
let nextLaunchedRoomId = 505
const pinnedRooms = new Set<number>([501])

Object.assign(conversationsApi, {
  get: async (id: number) => { activePreviewRoomId = id; return previewRooms.get(id) || previewRooms.get(roomId)! },
  listByProject: async (projectId: number, status: 'active' | 'closed' | 'all' = 'active') => [...previewRooms.values()]
    .filter((room) => room.project_id === projectId && (status === 'all' || room.is_active === (status === 'active')))
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at) || right.id - left.id),
  listMembers: async (id: number) => previewState === 'context' && id === 501 ? [
    { room_id: id, agent_id: 8, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' as const },
    { room_id: id, agent_id: 9, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' as const },
  ] : starterCommitted?.room_id === id ? (id === 583 ? starterAgents : appliedStarterAgents).map((agent) => ({
    room_id: id, agent_id: agent.id, is_active: true, joined_at: at(32), paused: false, run_state: 'idle' as const,
  })) : previewState.startsWith('finalize') && id === 501 ? [
    { room_id: id, agent_id: 8, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' as const },
    { room_id: id, agent_id: 9, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' as const },
    { room_id: id, agent_id: 10, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' as const },
  ] : previewState.startsWith('retry') && id === 501 ? [
    { room_id: id, agent_id: 8, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' as const },
    { room_id: id, agent_id: 9, is_active: true, joined_at: at(0), paused: false, run_state: inspectorRetryStarted ? 'running' as const : 'idle' as const },
  ] : previewState === 'workload' && (id === 503 || id === 504) ? [
    { room_id: id, agent_id: 8, is_active: true, joined_at: at(22), paused: false, run_state: id === 503 ? 'waiting' as const : 'idle' as const },
  ] : launchedRoomMembers.has(id) ? launchedRoomMembers.get(id)!.map((agentId) => ({
    room_id: id, agent_id: agentId, is_active: true, joined_at: at(28), paused: false, run_state: 'idle' as const,
  })) : id === 502 ? [] : (id === 504 ? templateRoomMembers.map((agentId) => ({
    room_id: id, agent_id: agentId, is_active: true, joined_at: at(27), paused: false, run_state: 'idle' as const,
  })) : id === 503 ? agents.map((agent) => ({
    room_id: id, agent_id: agent.id, is_active: true, joined_at: at(25), paused: false, run_state: 'idle' as const,
  })) : previewState === 'quiet' || previewState === 'media' || previewState === 'long' ? [
    { room_id: id, agent_id: 8, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' },
    { room_id: id, agent_id: 9, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' },
    { room_id: id, agent_id: 10, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' },
  ] : [
    { room_id: roomId, agent_id: 8, is_active: true, joined_at: at(0), paused: false, run_state: 'running' },
    { room_id: roomId, agent_id: 9, is_active: true, joined_at: at(0), paused: false, run_state: 'waiting' },
    { room_id: roomId, agent_id: 10, is_active: true, joined_at: at(0), paused: false, run_state: 'waiting' },
    { room_id: roomId, agent_id: 11, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' },
    { room_id: roomId, agent_id: 12, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' },
  ]),
  update: async (id: number, title: string) => {
    const current = previewRooms.get(id)!
    const updated = { ...current, title: title.trim(), updated_at: at(24) }
    previewRooms.set(id, updated)
    return updated
  },
  launch: async (value: { project_id: number; title: string; agent_ids: number[]; max_rounds: number; max_tool_calls: number; max_executor_seconds: number }) => {
    if (previewState === 'launch-error') throw Object.assign(new Error('synthetic unavailable team'), {
      response: { status: 409, data: { detail: { code: 'room_launch_agents_unavailable', agent_ids: [12] } } },
    })
    const id = nextLaunchedRoomId++
    const launched = { id, user_id: 1, project_id: value.project_id, title: value.title.trim(), is_active: true,
      created_at: at(28), updated_at: at(28) }
    previewRooms.set(id, launched)
    launchedRoomMembers.set(id, [...value.agent_ids].sort((a, b) => a - b))
    launchedRoomSchedules.set(id, { max_rounds: value.max_rounds, max_tool_calls: value.max_tool_calls,
      max_executor_seconds: value.max_executor_seconds })
    return launched
  },
  clone: async (_id: number, value: { project_id: number; title: string }) => {
    if (previewState === 'clone-error') throw Object.assign(new Error('synthetic unavailable team'), {
      response: { status: 409, data: { detail: { code: 'room_clone_members_unavailable', agent_ids: [9] } } },
    })
    const clonedRoom = { id: 503, user_id: 1, project_id: value.project_id, title: value.title.trim(), is_active: true, created_at: at(25), updated_at: at(25) }
    previewRooms.set(clonedRoom.id, clonedRoom)
    return clonedRoom
  },
  delete: async (id: number) => {
    const current = previewRooms.get(id)
    if (current) previewRooms.set(id, { ...current, is_active: false, updated_at: at(26) })
  },
  cancel: async () => undefined, stop: async () => undefined, continue: async () => ({}),
  addMember: async () => ({}), removeMember: async () => undefined, pauseMember: async () => undefined, resumeMember: async () => undefined,
})
Object.assign(projectsApi, {
  get: async (id: number) => importedProject?.id === id ? importedProject : appliedStarterProject?.id === id ? appliedStarterProject : id === navigationProject.id ? navigationProject : primaryProject,
  list: async (status: 'active' | 'archived' | 'all' = 'active') => [...(layoutProjects.length ? layoutProjects : [primaryProject]), ...(previewState === 'switcher' ? [navigationProject] : []), ...(importedProject ? [importedProject] : [])]
    .filter((project) => status === 'all' || project.is_active === (status === 'active')),
  previewArchive: async () => ({ project_id: 40, plan_hash: archivePreviewVersion ? 'b'.repeat(64) : 'a'.repeat(64), counts: {
    active_rooms: [...previewRooms.values()].filter((room) => room.project_id === 40 && room.is_active).length,
    pending_triggers: 1, running_triggers: 1, running_runs: 1, waiting_runs: 0, waiting_approvals: 1,
    started_actions_uncertain: 1, open_actions_cancelled: 2, grants_revoked: 1,
  }, rooms: [...previewRooms.values()].filter((room) => room.project_id === 40 && room.is_active).map((room) => ({
    id: room.id, title: room.title, last_event_seq: events.length, active_work_count: room.id === 501 ? 3 : 0,
  })), rooms_truncated: false }),
  archive: async (_id: number, planHash: string) => {
    if (previewState === 'archive-stale' && archivePreviewVersion === 0) {
      archivePreviewVersion += 1
      throw Object.assign(new Error('synthetic stale archive'), { response: { status: 409, data: { detail: { code: 'project_archive_plan_stale' } } } })
    }
    primaryProject = { ...primaryProject, is_active: false, archived_at: at(28), updated_at: at(28) }
    for (const [id, room] of previewRooms) if (room.project_id === 40 && room.is_active) previewRooms.set(id, { ...room, is_active: false, updated_at: at(28) })
    return { project_id: 40, is_active: false, archived_at: at(28), plan_hash: planHash, counts: {
      active_rooms: 1, pending_triggers: 1, running_triggers: 1, running_runs: 1, waiting_runs: 0, waiting_approvals: 1,
      started_actions_uncertain: 1, open_actions_cancelled: 2, grants_revoked: 1,
    }, cleanup_unconfirmed: previewState === 'archive-cleanup' ? [501] : [] }
  },
  restore: async () => { const activeRooms = [...previewRooms.values()].filter((room) => room.project_id === 40 && room.is_active).length
    if (activeRooms) throw Object.assign(new Error('synthetic active children'), {
      response: { status: 409, data: { detail: { code: 'project_restore_active_children', active_rooms: activeRooms } } },
    })
    primaryProject = { ...primaryProject, is_active: true, archived_at: null, updated_at: at(29) }
    return { project_id: 40, is_active: true, closed_rooms: [...previewRooms.values()].filter((room) => room.project_id === 40 && !room.is_active).length } },
})
Object.assign(roomIndexApi, {
  list: async ({ status = 'active', project_id: projectId, q = '', cursor, limit = 50 }: { status?: 'active'|'closed'|'all'; project_id?: number; q?: string; cursor?: string; limit?: number }) => {
    const projectRows = [primaryProject, ...(previewState === 'switcher' ? [navigationProject] : []), ...(importedProject ? [importedProject] : [])]
    const normalized = q.trim().toLocaleLowerCase()
    const rows = [...previewRooms.values()].filter((room) => { const project = projectRows.find((item) => item.id === room.project_id) || primaryProject
      const effective = room.is_active && project.is_active
      return (!projectId || room.project_id === projectId) && (status === 'all' || effective === (status === 'active'))
        && room.title.toLocaleLowerCase().includes(normalized) })
      .map((room) => { const project = projectRows.find((item) => item.id === room.project_id) || primaryProject
        const effective = room.is_active && project.is_active
        return { room_id: room.id, room_title: room.title, project_id: room.project_id, project_name: project.name,
          room_active: room.is_active, project_active: project.is_active, effective_active: effective, read_only: !effective,
          pinned: pinnedRooms.has(room.id), pinned_at: pinnedRooms.has(room.id) ? at(30) : null, last_event_seq: room.id >= 503 ? 0 : events.length,
          last_activity_at: room.updated_at, unread_authored: room.id === 501 ? 3 : 0, waiting_approvals: room.id === 501 ? 1 : 0,
          active_failures: 0, attention_count: room.id === 501 ? 1 : 0, work_status: room.id === 501 ? 'waiting' : 'idle' } })
      .sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.last_activity_at.localeCompare(a.last_activity_at) || b.room_id - a.room_id)
    const offset = cursor ? Number(cursor) : 0
    const pageSize = rootApproval || parallelRoot || createRoot ? Math.max(1, Math.min(50, limit)) : 2
    return { items: rows.slice(offset, offset + pageSize), next_cursor: offset + pageSize < rows.length ? String(offset + pageSize) : null }
  },
  pin: async (id: number) => { pinnedRooms.add(id); return { room_id: id, pinned: true, pinned_at: at(30) } },
  unpin: async (id: number) => { pinnedRooms.delete(id) },
})
Object.assign(attentionApi, {
  list: async () => {
    if (previewState === 'attention-error') throw new Error('synthetic attention failure')
    if (previewState !== 'attention' && previewState !== 'attention-stale') return { items: [], next_cursor: null }
    return { items: [
      { id: `approval:${pendingAction.action_id}`, kind: 'approval', occurred_at: pendingAction.requested_at,
        room_id: 501, room_title: '출시 준비실', project_id: 40, project_name: '데스크톱 협업', agent_id: 9, agent_name: '검토',
        request_event_id: null, request_seq: null, action_id: pendingAction.action_id, failure_event_id: null, failure_seq: null,
        summary_code: 'approval_browser', href: `/chat/501?action=${pendingAction.action_id}` },
      { id: `failure:${failureEvent.event_id}`, kind: 'failure', occurred_at: failureEvent.created_at,
        room_id: 501, room_title: '출시 준비실', project_id: 40, project_name: '데스크톱 협업', agent_id: 12, agent_name: '조사',
        request_event_id: null, request_seq: null, action_id: null, failure_event_id: failureEvent.event_id, failure_seq: failureEvent.seq,
        summary_code: 'failure_connection', href: `/chat/501?seq=${failureEvent.seq}&failure=${failureEvent.event_id}` },
    ], next_cursor: null }
  },
})
Object.assign(agentWorkloadsApi, {
  list: async () => previewState !== 'workload' ? { items: [], next_cursor: null } : { items: [
    { agent_id: 8, agent_name: '기획', agent_active: true, overall_state: 'running',
      counts: { running: 1, waiting: 1, paused: 0, failed: 1, idle: 0, unknown: 0, total_rooms: 3 }, rooms_truncated: false,
      rooms: [
        { room_id: 501, room_title: '출시 준비실', project_id: 40, project_name: '데스크톱 협업', state: 'running',
          trigger_event_id: 'synthetic-event-1', trigger_seq: 1, failure_event_id: null, failure_seq: null, last_activity_at: at(27), href: '/chat/501?seq=1' },
        { room_id: 503, room_title: '문서 병렬 검토', project_id: 40, project_name: '데스크톱 협업', state: 'waiting',
          trigger_event_id: 'workload-request-503', trigger_seq: 1, failure_event_id: null, failure_seq: null, last_activity_at: at(26), href: '/chat/503?seq=1' },
        { room_id: 504, room_title: '설치 확인', project_id: 40, project_name: '데스크톱 협업', state: 'failed',
          trigger_event_id: 'workload-request-504', trigger_seq: 1, failure_event_id: 'workload-failure-504', failure_seq: 2,
          last_activity_at: at(25), href: '/chat/504?seq=2&failure=workload-failure-504' },
      ] },
    { agent_id: 11, agent_name: '대기', agent_active: true, overall_state: 'idle',
      counts: { running: 0, waiting: 0, paused: 0, failed: 0, idle: 1, unknown: 0, total_rooms: 1 }, rooms_truncated: false,
      rooms: [{ room_id: 501, room_title: '출시 준비실', project_id: 40, project_name: '데스크톱 협업', state: 'idle',
        trigger_event_id: null, trigger_seq: null, failure_event_id: null, failure_seq: null, last_activity_at: at(24), href: '/chat/501' }] },
  ], next_cursor: null },
})
Object.assign(contextPreviewsApi, {
  create: async (id: number, content: string) => {
    const contextMembers = [8, 9].map((agentId) => ({ room_id: id, agent_id: agentId, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' as const }))
    const mentions = buildRecipientGroups(agents, contextMembers).filter((group) => group.matches(content)).flatMap((group) => group.agentIds).sort((left, right) => left - right)
    const selected = mentions.length ? mentions : [8, 9]
    const projections = [
    { agent_id: 8, agent_name: '기획', model_id: 'qwen3.7-plus-2026-09', projection_status: 'ready', error_code: null,
      system_text: '출시 준비를 사용자의 관점에서 정리합니다.', messages: [
        { role: 'user', content: '설치 가이드의 마지막 검토를 부탁해.' },
        { role: 'assistant', content: '설치 가이드의 전체 구성을 검토했습니다.' },
        { role: 'user', content: 'tool 시스템 (room seq 5): 로컬 미리보기 화면을 공유했습니다. 이미지 내용은 제공되지 않습니다.' },
        { role: 'user', content },
      ], source_coverage: { committed_high_water: 6, included_event_seqs: [1, 2, 5], summarized_ranges: [{ kind: 'extractive_checkpoint', source_start_seq: 1, source_end_seq: 4 }], draft_included: true },
      truncations: [], non_text: [{ event_id: screenshotEvent.event_id, seq: screenshotEvent.seq, kind: 'screenshot_reference', supplied: false, image_mime: 'image/png', width: 1440, height: 1024 }],
      budget: { unit: 'characters', limit_chars: 12000, used_chars: 428, remaining_chars: 11572 } },
    { agent_id: 9, agent_name: '검토', model_id: 'claude-sonnet-2026-09', projection_status: 'ready', error_code: null,
      system_text: '문서의 표현과 누락된 조건을 검토합니다.', messages: [
        { role: 'user', content: '설치 가이드의 마지막 검토를 부탁해.' },
        { role: 'assistant', content: '지금 문서의 용어와 단계별 설명을 중심으로 검토하고 있어요.' },
        { role: 'user', content },
      ], source_coverage: { committed_high_water: 6, included_event_seqs: [1, 3], summarized_ranges: [{ kind: 'extractive_checkpoint', source_start_seq: 1, source_end_seq: 4 }], draft_included: true },
      truncations: [{ event_id: 'synthetic-event-2', seq: 2, original_chars: 140, included_chars: 82, omitted_chars: 58 }], non_text: [],
      budget: { unit: 'characters', limit_chars: 10000, used_chars: 391, remaining_chars: 9609 } },
    ]
    return { room_id: id, projected_seq: 7, selected_agent_ids: selected,
      fingerprint: (selected.length === 1 ? '8' : '4').repeat(64),
      agents: projections.filter((agent) => selected.includes(agent.agent_id)) }
  },
})
let previewBotDirectory:BotDirectory={version:1,revision:0,sections:previewState.startsWith('bot-directory-')?[{id:'11111111-1111-4111-8111-111111111111',name:'Project'}]:[],items:previewState==='bot-directory-all-hidden' ? agents.map(a=>({agent_id:a.id,pinned:false,hidden:true,section_id:null})) : previewState.startsWith('bot-directory-') ? [{agent_id:8,pinned:true,hidden:false,section_id:'11111111-1111-4111-8111-111111111111'},{agent_id:9,pinned:false,hidden:true,section_id:null},...(previewState==='bot-directory-home'?[{agent_id:10,pinned:false,hidden:true,section_id:null}]:[])] : []}
let previewBotDirectoryReceipt:BotDirectoryCommand|null=null
let previewMissingRequest:{key:string;body:string}|null=null
Object.assign(botDirectoryApi,{
 read:async()=>{if(previewState==='bot-directory-read-error')throw Error('Synthetic read failure');return structuredClone(previewBotDirectory)},
 save:async(body:BotDirectoryWrite,key:string)=>{if(previewState==='bot-directory-missing'){if(!previewMissingRequest){previewMissingRequest={key,body:JSON.stringify(body)};throw Error('Synthetic request not received')}if(previewMissingRequest.key!==key||previewMissingRequest.body!==JSON.stringify(body))throw Error('Synthetic request mismatch')}if(previewState==='bot-directory-conflict')throw {response:{status:409,data:{detail:{code:'bot_directory_changed'}}}};previewBotDirectory={version:1,revision:body.expected_revision+1,sections:structuredClone(body.sections),items:structuredClone(body.items)};previewBotDirectoryReceipt={...previewBotDirectory,command_key:key,expected_revision:body.expected_revision,replayed:false};if(previewState==='bot-directory-unknown')throw Error('Synthetic response lost');return structuredClone(previewBotDirectoryReceipt)},
 receipt:async(key:string)=>{if(!previewBotDirectoryReceipt||previewBotDirectoryReceipt.command_key!==key)throw {response:{status:404,data:{detail:{code:'bot_directory_command_not_found'}}}};return {...structuredClone(previewBotDirectoryReceipt),replayed:true}},
})
Object.assign(agentsApi, {
  avatars: async () => Object.fromEntries(agents.map(agent => [String(agent.id), agent.avatar || avatarFromSeed(agent.id)])),
  list: async () => [...agents, ...(previewState === 'first-success-resume' ? starterAgents : []), ...appliedStarterAgents, ...(importedAgent ? [importedAgent] : [])],
  create: async (input: import('../src/api/agents').AgentInput) => {
    const created = { ...agents[0], ...input, id: Math.max(...agents.map(a => a.id)) + 1, model: input.model_id, avatar: input.avatar || randomAvatar() }
    agents.push(created); return created
  },
  update: async (id: number, input: Partial<import('../src/api/agents').AgentInput>) => {
    const index = agents.findIndex(a => a.id === id)
    if (index < 0) throw Error('Synthetic agent missing')
    const value = { ...agents[index], ...input, model: input.model_id || agents[index].model, avatar: input.avatar || (agents[index].avatar || avatarFromSeed(id)) }
    agents[index] = value; return value
  }, delete: async () => undefined,
})
if (previewState.startsWith('bot-duplicate-')) {
 let duplicateReceipt:DuplicateReceipt|null=null
 let duplicateReceiptKey:string|null=null
 let duplicateMissing:{key:string;body:string}|null=null
 agentsApi.get=async id=>{const found=agents.find(a=>a.id===id);if(!found)throw Error('Synthetic Bot unavailable');return structuredClone(found)}
 Object.assign(botDuplicateApi,{
  preview:async(id:number,name:string)=>{if(previewState==='bot-duplicate-preview-error')throw Error('Synthetic preview failure');const source=agents.find(a=>a.id===id);if(!source)throw Error('Synthetic source missing');return {version:1,source_agent_id:id,name,plan_hash:'a'.repeat(64),profile:{description:source.description,system_prompt:source.system_prompt,connection_id:source.connection_id,model_id:source.model_id,temperature:source.temperature,max_tokens:source.max_tokens,avatar:source.avatar||avatarFromSeed(source.id)},skills:[{skill_id:'33333333-3333-4333-8333-333333333333',revision:1,binding_revision:1,name:'Research'}],routines:[{routine_id:'11111111-1111-4111-8111-111111111111',revision:1,name:'Weekly check',instruction:'Review the report',schedule:{kind:'weekly',weekdays:[0],hour:9,minute:0,timezone:'Asia/Seoul'},enabled:true,next_run_at:'2030-01-07T00:00:00Z',webhook_setup_required:false},{routine_id:'22222222-2222-4222-8222-222222222222',revision:1,name:'Incoming event',instruction:'Review the event',schedule:{kind:'webhook'},enabled:false,next_run_at:null,webhook_setup_required:true}],mcp_reconnect_required:true}},
  submit:async(id:number,body:DuplicateBody,key:string)=>{if(previewState==='bot-duplicate-stale')throw {response:{status:409,data:{detail:{code:'bot_clone_changed'}}}};if(previewState==='bot-duplicate-rejected')throw {response:{status:409,data:{detail:{code:'bot_clone_routine_limit'}}}};if(previewState==='bot-duplicate-missing'){if(!duplicateMissing){duplicateMissing={key,body:JSON.stringify(body)};throw Error('Synthetic request not received')}if(duplicateMissing.key!==key||duplicateMissing.body!==JSON.stringify(body))throw Error('Synthetic request mismatch')};const source=agents.find(a=>a.id===id);if(!source)throw Error('Synthetic source missing');const copy={...source,id:99,name:body.name,avatar:source.avatar||avatarFromSeed(source.id)};if(!agents.some(a=>a.id===99))agents.push(copy);personalRoomAgents.set(777,99);personalRooms.set(99,777);launchedRoomMembers.set(777,[99]);const base=previewRooms.get(501);if(base)previewRooms.set(777,{...base,id:777,title:`${body.name} · 개인 대화`});duplicateReceipt={version:1,source_agent_id:id,name:body.name,plan_hash:body.plan_hash,agent_id:99,room_id:777,project_id:40,href:'/chat/777',skill_count:1,routine_count:2,webhook_setup_required:true,mcp_reconnect_required:true,replayed:false};duplicateReceiptKey=key;if(previewState==='bot-duplicate-unknown')throw Error('Synthetic response lost');return structuredClone(duplicateReceipt)},
  receipt:async(id:number,key:string)=>{if(!duplicateReceipt||duplicateReceiptKey!==key||duplicateReceipt.source_agent_id!==id)throw {response:{status:404,data:{detail:{code:'bot_clone_command_not_found'}}}};return {...structuredClone(duplicateReceipt),replayed:true}},
 })
}
if (previewState.startsWith('bot-publish-')) {
 const origin='https://example.test',publisher='synthetic-publisher',id='11111111-1111-4111-8111-111111111111',sha='a'.repeat(64),source='b'.repeat(64),plan='c'.repeat(64)
 let configured=previewState!=='bot-publish-connect',pub=previewState==='bot-publish-update'||previewState==='bot-publish-revoke'||previewState==='bot-publish-conflict'?{origin,publisher_id:publisher,template_id:id,revision:1,revoked:false,snapshot_sha256:sha,url:`${origin}/t/${id}`}:null
 let saved:any=null,first=true,remoteCompleted:any=null
 const journal=new Map<string,any>()
 botTemplatesApi.exportPreview=async()=>({template:structuredClone(syntheticBotTemplate),template_sha256:source})
 Object.assign(templatePublisherApi,{
  config:async()=>({version:1,configured,revision:configured?1:0,origin:configured?origin:null,publisher_id:configured?publisher:null}),
  configure:async(_origin:string,_token:string,_revision:number)=>{configured=true;return {version:1,configured:true,revision:1,origin,publisher_id:publisher}},
  status:async(bot:number)=>{if(previewState==='bot-publish-error')throw Error('Synthetic read error');return {version:1,bot_id:bot,publication:pub,pending:saved&&['prepared','unknown','not_found'].includes(saved.state)?{command_key:saved.command_key,state:saved.state}:null}},
  preview:async(bot:number,operation:PublishOperation,template:BotTemplate|null)=>({version:1,bot_id:bot,operation,template:structuredClone(template),snapshot_sha256:operation==='revoke'?null:sha,source_sha256:source,origin,publisher_id:publisher,template_id:operation==='publish'?null:id,expected_revision:operation==='publish'?null:pub?.revision||1,plan_hash:plan}),
  command:async(bot:number,operation:PublishOperation,template:BotTemplate|null,plan_hash:string,key:string)=>{const old=journal.get(key);if(old&&(old.bot_id!==bot||old.operation!==operation||old.plan_hash!==plan_hash||JSON.stringify(old.template)!==JSON.stringify(template)))throw Error('Synthetic frozen request mismatch');if(old&&old.state!=='not_found'){if(old.state==='completed'||old.state==='conflict')return structuredClone(old);throw Error('Synthetic request unresolved')}if(!old&&saved&&['prepared','unknown','not_found'].includes(saved.state))throw Error('Synthetic request unresolved');const scope=old?.scope||{owner_id:agents.find(a=>a.id===bot)?.user_id||1,bot_id:bot,registry_id:1,origin,publisher_id:publisher,source_sha256:source,operation,snapshot_sha256:operation==='revoke'?null:sha,template_id:operation==='publish'?null:id,expected_revision:operation==='publish'?null:pub?.revision||1};saved={version:1,bot_id:bot,command_key:key,state:'completed',operation,template:structuredClone(template),plan_hash,scope,response:{template_id:id,revision:operation==='publish'?1:(scope.expected_revision||0)+1,revoked:operation==='revoke',snapshot_sha256:scope.snapshot_sha256,url:`${origin}/t/${id}`}};journal.set(key,saved);if(first&&previewState==='bot-publish-conflict'){first=false;saved.state='conflict';saved.response=null;return structuredClone(saved)}if(first&&previewState==='bot-publish-missing'){first=false;saved.state='unknown';saved.response=null;throw Error('Synthetic request missing')}if(first&&previewState==='bot-publish-unknown'){first=false;remoteCompleted=structuredClone(saved);saved.state='unknown';saved.response=null;throw Error('Synthetic remote completed; local confirmation lost')}pub={origin,publisher_id:publisher,template_id:id,revision:saved.response.revision,revoked:saved.response.revoked,snapshot_sha256:saved.response.snapshot_sha256,url:saved.response.url};return structuredClone(saved)},
  receipt:async(_bot:number,key:string)=>{const item=journal.get(key);if(!item||item.bot_id!==_bot)throw Error('Synthetic missing journal');if(previewState==='bot-publish-missing'&&item.state==='unknown')item.state='not_found';if(previewState==='bot-publish-unknown'&&item.state==='unknown'&&remoteCompleted?.command_key===key){Object.assign(item,structuredClone(remoteCompleted));pub={origin,publisher_id:publisher,template_id:id,revision:item.response.revision,revoked:item.response.revoked,snapshot_sha256:item.response.snapshot_sha256,url:item.response.url}}return structuredClone(item)},
  remote:async(bot:number)=>({version:1,bot_id:bot,template_id:id,revision:previewState==='bot-publish-conflict'?2:pub?.revision||1,revoked:pub?.revoked||false,snapshot_sha256:pub?.snapshot_sha256||sha,matches_local:previewState!=='bot-publish-conflict'}),
  adopt:async(_bot:number,body:{observed_revision:number})=>{if(pub)pub={...pub,revision:body.observed_revision};return {version:1}},
 })
}
if (previewState.startsWith('bot-template-')) {
 const stable=(value:unknown):unknown=>Array.isArray(value)?value.map(stable):value&&typeof value==='object'?Object.fromEntries(Object.entries(value).sort(([a],[b])=>a<b?-1:a>b?1:0).map(([key,item])=>[key,stable(item)])):value
 const digest=async(value:unknown)=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(stable(value))))),byte=>byte.toString(16).padStart(2,'0')).join('')
 let templateReceipt:TemplateReceipt|null=null,templateReceiptKey:string|null=null,linkReads=0
 let missingRequest:{key:string;body:string}|null=null
 agentsApi.get=async id=>{const found=agents.find(a=>a.id===id);if(!found)throw Error('Synthetic Bot unavailable');return structuredClone(found)}
 Object.assign(botTemplatesApi,{
  linkPreview:async(url:string)=>{const source=templatePreviewUrl(url);if(!source)throw Error('Synthetic invalid URL');if(previewState==='bot-template-link-unavailable')throw {response:{status:422,data:{detail:{code:'bot_template_link_unavailable'}}}};linkReads+=1;const template=structuredClone(syntheticBotTemplate);if(previewState==='bot-template-link-changed'&&linkReads>1)template.description='Changed synthetic source';return {source_url:source,template,template_sha256:await digest(template)}},
  exportPreview:async(_id:number)=>{if(previewState==='bot-template-export-error')throw Error('Synthetic export preview failure');return {template:structuredClone(syntheticBotTemplate),template_sha256:await digest(syntheticBotTemplate)}},
  importPreview:async(template:BotTemplate,selection:TemplateSelection)=>{if(previewState==='bot-template-read-error')throw Error('Synthetic preview failure');const templateHash=await digest(template);return {version:1,...selection,template_sha256:templateHash,plan_hash:await digest({templateHash,selection}),skills_count:template.skills.length,connection_enabled:true,routines:template.routines.map(r=>({name:r.name,schedule:r.schedule,enabled:r.enabled,next_run_at:r.enabled&&r.schedule.kind!=='webhook'?'2030-01-07T00:00:00Z':null,webhook_setup_required:r.schedule.kind==='webhook',availability:r.enabled?(r.schedule.kind==='webhook'?'webhook_setup_required':'requires_runtime_checks'):'paused'})),app_bindings_copied:false}},
  import:async(template:BotTemplate,selection:TemplateSelection,planHash:string,key:string)=>{const templateHash=await digest(template);if(planHash!==await digest({templateHash,selection}))throw Error('Synthetic plan mismatch');if(previewState==='bot-template-stale')throw {response:{status:409,data:{detail:{code:'bot_template_changed'}}}};if(previewState==='bot-template-rejected')throw {response:{status:409,data:{detail:{code:'bot_template_skill_library_limit'}}}};if(previewState==='bot-template-missing'||previewState==='bot-template-link-changed'){const body=JSON.stringify({template,selection,planHash});if(!missingRequest){missingRequest={key,body};throw Error('Synthetic request not received')}if(missingRequest.key!==key||missingRequest.body!==body)throw Error('Synthetic request mismatch')};const copy={...agents[0],id:99,name:selection.name,description:template.description,system_prompt:template.system_prompt,connection_id:selection.connection_id,model_id:selection.model_id,model:selection.model_id,avatar:template.avatar};if(!agents.some(a=>a.id===99))agents.push(copy);personalRoomAgents.set(777,99);personalRooms.set(99,777);launchedRoomMembers.set(777,[99]);const base=previewRooms.get(501);if(base)previewRooms.set(777,{...base,id:777,title:`${selection.name} · 개인 대화`});templateReceipt={version:1,...selection,template_sha256:templateHash,plan_hash:planHash,agent_id:99,room_id:777,project_id:40,href:'/chat/777',skill_count:template.skills.length,routine_count:template.routines.length,webhook_setup_required:template.routines.some(r=>r.schedule.kind==='webhook'),app_bindings_copied:false,replayed:false};templateReceiptKey=key;if(previewState==='bot-template-unknown'||previewState==='bot-template-link-unknown')throw Error('Synthetic response lost');return structuredClone(templateReceipt)},
  receipt:async(key:string)=>{if(!templateReceipt||templateReceiptKey!==key)throw {response:{status:404,data:{detail:{code:'bot_template_command_not_found'}}}};return {...structuredClone(templateReceipt),replayed:true}},
 })
}
let readinessRecords: ReadinessTestRecord[] = [
  { id: 72, connection_id: 2, tested_at: at(28), duration_ms: 318, model_id: 'claude-sonnet', ok: true, code: 'connected', config_fingerprint: '2'.repeat(64), config_current: true, credential_observation: 'verified', verified_capabilities: { max_tokens: false, temperature: false, tools: true, vision: false } },
  { id: 71, connection_id: 1, tested_at: at(20), duration_ms: 641, model_id: 'qwen3.7-plus', ok: false, code: 'invalid_credential', config_fingerprint: '0'.repeat(64), config_current: false, credential_observation: 'rejected', verified_capabilities: null },
]
const readinessConnection = (id: number) => {
  const item = id === 1 ? connection : reviewConnection
  const currentFingerprint = String(id).repeat(64)
  const latest = readinessRecords.find((record) => record.connection_id === id) || null
  const bindings = id === 1 ? agents.filter((agent) => agent.connection_id === 1) : agents.filter((agent) => agent.connection_id === 2)
  return { connection_id: id, label: item.label, provider_kind: item.provider_kind, enabled: item.enabled, auth_required: item.auth_required,
    has_secret: item.has_secret, public_config: { base_url: item.base_url, default_model_id: item.default_model_id, declared_capabilities: item.capabilities.declared },
    config_fingerprint: currentFingerprint, declared_state: 'declared', credential_observation: latest?.config_current ? latest.credential_observation : 'unknown',
    binding_counts: { active_bound: bindings.filter((agent) => agent.is_active && agent.model_id).length, active_missing_model: bindings.filter((agent) => agent.is_active && !agent.model_id).length, inactive: bindings.filter((agent) => !agent.is_active).length, total: bindings.length },
    agent_bindings: bindings.map((agent) => ({ agent_id: agent.id, agent_name: agent.name, agent_active: agent.is_active, model_id: agent.model_id,
      status: agent.is_active ? agent.model_id ? 'bound' : 'model_missing' : 'inactive', observation: latest?.config_current && latest.model_id === agent.model_id ? latest.ok ? 'verified' : 'failed' : 'unknown', latest_test_id: latest?.config_current && latest.model_id === agent.model_id ? latest.id : null })),
    agent_bindings_truncated: false, latest_test: latest }
}
Object.assign(readinessApi, {
  get: async () => ({ connections: previewState === 'readiness' || previewState.startsWith('first-success') ? [readinessConnection(1), readinessConnection(2)] : [] }),
  history: async (id: number) => ({ connection_id: id, current_config_fingerprint: String(id).repeat(64), items: readinessRecords.filter((record) => record.connection_id === id) }),
})
Object.assign(connectionsApi, {
  list: async () => {
    if (previewState === 'error') throw new Error('synthetic list failure')
    return [connection, reviewConnection, ...(importedConnection ? [importedConnection] : [])]
  },
  create: async () => connection, update: async () => connection, delete: async () => undefined,
  testPreview: async (_id: number, modelId: string, profile: 'text'|'full') => ({ profile, model_id: modelId,
    max_request_count: profile === 'text' ? 1 : 7 }),
  test: async (id: number, modelId?: string, profile: 'text'|'full' = 'full') => {
    if (previewState !== 'readiness') return { ok: true, code: 'verified', message: '모델 검증이 완료되었습니다.' }
    const tested = id === 1 ? connection : reviewConnection
    const record: ReadinessTestRecord = { id: Math.max(...readinessRecords.map((item) => item.id)) + 1, connection_id: id, tested_at: at(31), duration_ms: 284,
      model_id: modelId || tested.default_model_id, ok: true, code: 'connected', config_fingerprint: String(id).repeat(64), config_current: true,
      credential_observation: 'verified', verified_capabilities: { max_tokens: profile === 'full', temperature: profile === 'full', tools: profile === 'full', vision: profile === 'full' && id === 1 },
      profile, actual_request_count: profile === 'text' ? 1 : id === 1 ? 6 : 6 }
    readinessRecords = [record, ...readinessRecords.filter((item) => item.connection_id !== id), ...readinessRecords.filter((item) => item.connection_id === id)].slice(0, 20)
    return { ok: true, code: 'connected', message: '', record }
  },
  unlock: async () => ({ state: 'ready' }), unlockStatus: async () => ({ state: 'ready' }),
})
let starterCommitted: { project_id: number; room_id: number; agents: { proposer: number; reviewer: number } } | null =
  previewState === 'first-success-resume' ? { project_id: 83, room_id: 583, agents: { proposer: 18, reviewer: 19 } } : null
const starterAllocated = (intent: StarterIntent): StarterPreview => ({
  plan_hash: '7'.repeat(64), project_title: `${intent.project_title} (미리보기)`, room_title: intent.room_title,
  agents: intent.agents.map((agent, index) => ({ ...agent, requested_name: agent.name, name: `${agent.name}${index ? '' : ' 2'}`,
    test_history_id: agent.connection_id === 1 ? 71 : 72, config_revision: 3, config_fingerprint: String(agent.connection_id).repeat(64) })) as StarterPreview['agents'],
  policy: { max_rounds: 2, max_tool_calls: 0, max_executor_seconds: 60 },
})
Object.assign(starterApi, {
  lastResult: async () => starterCommitted,
  preview: async (intent: StarterIntent) => starterAllocated(intent),
  apply: async (_intent: StarterIntent & { plan_hash: string }) => {
    const fixture = createdStarterFixture(_intent, starterAllocated(_intent), at(32))
    starterCommitted = fixture.result
    appliedStarterAgents = fixture.agents
    appliedStarterProject = fixture.project
    previewRooms.set(fixture.room.id, fixture.room)
    previewDrafts.set(fixture.room.id, emptyPreviewDraft())
    return { ...starterCommitted, replayed: false }
  },
})
const workloadRoomEvents = new Map<number, RoomEvent[]>([
  [503, [{ ...event(1, 'user', 1, '지민', 'message', { content: '문서 두 부분을 동시에 검토해 줘.' }),
    room_id: 503, event_id: 'workload-request-503', created_at: at(25) }]],
  [504, [
    { ...event(1, 'user', 1, '지민', 'message', { content: '설치 확인 결과를 알려 줘.' }), room_id: 504, event_id: 'workload-request-504', created_at: at(23) },
    { ...event(2, 'agent', 8, '기획', 'failure', { code: 'provider_unavailable', trigger_event_id: 'workload-request-504' }),
      room_id: 504, event_id: 'workload-failure-504', created_at: at(24) },
  ]],
])
Object.assign(eventsApi, { list: async (id: number, after: number) => (crossRoom && id === 701 ? crossTargetEvents : workloadRoomEvents.get(id) || (id >= 503 ? [] : events)).filter((item) => item.seq > after), send: async () => events[0] })
eventsApi.replies=async (room,root,cursor,limit=20)=>{
  if(previewState==='thread-error')throw Error('Synthetic reply read failure')
  const upper=cursor?.upperSeq ?? Math.max(root.seq,...events.filter(e=>e.room_id===room).map(e=>e.seq))
  const rows=events.filter(e=>e.room_id===room&&e.kind==='message'&&e.seq>(cursor?.afterSeq??0)&&e.seq<=upper&&e.payload.reply_to && (e.payload.reply_to as {event_id:string;seq:number}).event_id===root.event_id && (e.payload.reply_to as {event_id:string;seq:number}).seq===root.seq).slice(0,limit+1)
  const items=rows.slice(0,limit),more=rows.length>limit
  return {version:1,root,upper_seq:upper,items,has_more:more,next_after_seq:more?items[items.length-1].seq:null}
}

let syntheticThreadReads = 0
eventsApi.thread=async (room,root,cursor,limit=20)=>{
  const delayed = previewState === 'thread-late' && root.seq === 1 && syntheticThreadReads++ === 0
  if(previewState==='thread-error')throw Error('Synthetic thread read failure')
  const upper=cursor?.upperSeq ?? Math.max(root.seq,...events.filter(e=>e.room_id===room).map(e=>e.seq))
  const ancestors=new Map([[root.event_id,{seq:root.seq,depth:0}]])
  const descendants:ReplyThreadItem[]=[]
  let depthLimited=false
  for(const item of events.filter(e=>e.room_id===room&&e.kind==='message'&&e.seq>root.seq&&e.seq<=upper).sort((a,b)=>a.seq-b.seq)){
    const parent=item.payload.reply_to
    if(!validReply(parent))continue
    const ancestor=ancestors.get(parent.event_id)
    if(!ancestor||ancestor.seq!==parent.seq||parent.seq>=item.seq)continue
    if(ancestor.depth===64){depthLimited=true;continue}
    const depth=ancestor.depth+1
    ancestors.set(item.event_id,{seq:item.seq,depth})
    descendants.push({event:item,parent,depth})
  }
  const rows=descendants.filter(item=>item.event.seq>(cursor?.afterSeq??0)).slice(0,limit+1)
  const items=rows.slice(0,limit),more=rows.length>limit
  if(previewState==='thread-invalid-parent'&&items[0])items[0]={...items[0],parent:{event_id:'d4400000-0000-4000-8000-000000000099',seq:1}}
  const result = {version:2 as const,root,upper_seq:upper,items,has_more:more,next_after_seq:more?items[items.length-1].event.seq:null,depth_limited:depthLimited}
  if(delayed) {
    const oldItems = items.map(item=>({...item,event:{...item.event,payload:{...item.event.payload,content:'오래된 합성 응답이 표시되면 안 됩니다.'}}}))
    await new Promise(resolve=>setTimeout(resolve,2500))
    return {...result,items:oldItems}
  }
  return result
}

const emptyPreviewDraft = (): RoomDraft => ({ content: '', reply_to: null, version: 0, idempotency_key: null, updated_at: null })
const previewDrafts = new Map<number, RoomDraft>([[501, previewState.startsWith('persistent-reply-') && previewState !== 'persistent-reply-ready'
  ? {content:previewState==='persistent-reply-empty'?'':'저장된 답장 초안입니다.',reply_to:previewState==='persistent-reply-unavailable'?{event_id:'b33a0000-0000-4000-8000-000000000099',seq:99}:{event_id:'b33a0000-0000-4000-8000-000000000002',seq:2},version:1,idempotency_key:previewState==='persistent-reply-empty'?null:'b33a0000-0000-4000-8000-000000000005',updated_at:at(22)}
  : previewState.startsWith('quote-reply-')
  ? {content:previewState==='quote-reply-conflict'?'@검토 기존 의견을 확인해 주세요.':'기존 초안: 확인할 조건을 덧붙이겠습니다.',version:1,idempotency_key:'b33a0000-0000-4000-8000-000000000005',updated_at:at(22)}
  : previewState === 'draft' || previewState === 'context'
  ? { content: `${previewState === 'context' ? '@기획 @검토' : '@기획'} 출시 노트의 설치 단계를 다시 확인해 주세요.\n누락된 항목은 목록으로 정리해 주세요.`, version: 4,
      idempotency_key: crypto.randomUUID(), updated_at: at(22) }
  : emptyPreviewDraft()]])
Object.assign(draftsApi, {
  get: async (id: number) => previewDrafts.get(id) || emptyPreviewDraft(),
  put: async (id: number, content: string, expectedVersion: number, replyTo: RoomDraft['reply_to'] = null) => {
    let previewDraft = previewDrafts.get(id) || emptyPreviewDraft()
    if (expectedVersion !== previewDraft.version) throw Object.assign(new Error('synthetic draft conflict'), { response: { status: 409 } })
    if (content !== previewDraft.content || JSON.stringify(replyTo) !== JSON.stringify(previewDraft.reply_to ?? null)) previewDraft = { content, reply_to: replyTo, version: previewDraft.version + 1,
      idempotency_key: content ? ((previewState.startsWith('persistent-reply-')||previewState.startsWith('routine-chat-')) ? crypto.randomUUID() : '123e4567-e89b-42d3-a456-426614174005') : null, updated_at: content ? at(23) : null }
    previewDrafts.set(id, previewDraft)
    return previewDraft
  },
  clear: async (id: number, expectedVersion: number) => {
    const previewDraft = previewDrafts.get(id) || emptyPreviewDraft()
    if (expectedVersion !== previewDraft.version) throw Object.assign(new Error('synthetic draft conflict'), { response: { status: 409 } })
    previewDrafts.set(id, { content: '', reply_to: null, version: previewDraft.version + 1, idempotency_key: null, updated_at: null })
  },
})
let previewReadState: RoomReadState = previewState === 'timeline'
  ? { last_read_seq: 68, updated_at: at(22) }
  : { last_read_seq: 0, updated_at: null }
Object.assign(readStatesApi, {
  get: async () => previewReadState,
  put: async (_id: number, lastReadSeq: number) => {
    if (lastReadSeq > previewReadState.last_read_seq) previewReadState = { last_read_seq: lastReadSeq, updated_at: at(23) }
    return previewReadState
  },
})
const dashboardSummary: DashboardSummary = {
  setup: { next_action: previewState === 'setup' ? 'connection' : null },
  counts: {
    enabled_connections: previewState === 'setup' ? 0 : 2,
    ready_agents: previewState === 'setup' ? 0 : 5,
    ready_projects: previewState === 'setup' ? 0 : 2,
    active_rooms: previewState === 'empty' || previewState === 'setup' || previewState.startsWith('first-success') ? 0 : 3,
    unread_authored: previewState === 'workqueue' ? 7 : 0,
    waiting_approvals: previewState === 'workqueue' ? 2 : 0,
    active_failures: previewState === 'workqueue' ? 1 : 0,
  },
  rooms: ['workqueue', 'one-room', 'primary-ready'].includes(previewState) ? [
    { room_id: 501, room_title: previewState === 'primary-ready' ? 'ChatPersistGo1029659d4d68 · 긴 이름의 개인 대화' : '출시 준비실', project_id: 40, project_name: '데스크톱 협업', last_event_seq: 22,
      last_activity_at: at(52), last_authored_seq: 21, last_authored_at: at(51),
      last_authored_preview: previewState === 'primary-ready' ? 'GO_ATOMIC_ROLE_67725f05_검증용_긴_원문_'.repeat(16) : '설치 가이드 최종 검토가 승인 대기 중입니다.', unread_authored: 4, waiting_approvals: 2,
      active_failures: 0, work_status: 'waiting' },
    { room_id: 502, room_title: '동기화 오류 조사', project_id: 41, project_name: '코어 안정화', last_event_seq: 14,
      last_activity_at: at(47), last_authored_seq: 13, last_authored_at: at(46),
      last_authored_preview: '최근 실행을 마치지 못했습니다. 확인이 필요합니다.', unread_authored: 2, waiting_approvals: 0,
      active_failures: 1, work_status: 'paused' },
    { room_id: 503, room_title: '릴리스 노트', project_id: 40, project_name: '데스크톱 협업', last_event_seq: 8,
      last_activity_at: at(41), last_authored_seq: 8, last_authored_at: at(41),
      last_authored_preview: '변경 사항을 정리하고 있습니다.', unread_authored: 1, waiting_approvals: 0,
      active_failures: 0, work_status: 'running' },
  ] : [],
}
if (previewState==='bot-directory-home') {
 dashboardSummary.counts.active_rooms=4
 dashboardSummary.rooms=[
  {room_id:501,personal_agent_id:8,room_title:'Pinned Bot',project_id:40,project_name:'Personal',last_event_seq:1,last_activity_at:at(20),last_authored_seq:null,last_authored_at:null,last_authored_preview:null,unread_authored:0,waiting_approvals:0,active_failures:0,work_status:'idle'},
  {room_id:502,personal_agent_id:9,room_title:'Hidden Bot approval',project_id:40,project_name:'Personal',last_event_seq:2,last_activity_at:at(20),last_authored_seq:null,last_authored_at:null,last_authored_preview:null,unread_authored:0,waiting_approvals:1,active_failures:0,work_status:'waiting'},
  {room_id:503,personal_agent_id:null,room_title:'Group room',project_id:40,project_name:'Team',last_event_seq:3,last_activity_at:at(20),last_authored_seq:null,last_authored_at:null,last_authored_preview:null,unread_authored:0,waiting_approvals:0,active_failures:0,work_status:'idle'},
  {room_id:504,personal_agent_id:10,room_title:'Hidden Bot quiet',project_id:40,project_name:'Personal',last_event_seq:4,last_activity_at:at(20),last_authored_seq:null,last_authored_at:null,last_authored_preview:null,unread_authored:0,waiting_approvals:0,active_failures:0,work_status:'idle'},
 ]
}
if (['checkin-on', 'checkin-failed', 'checkin-cancelled'].includes(previewState)) {
  dashboardSummary.counts.active_rooms = 1
  dashboardSummary.rooms = [{ room_id: 501, room_title: '기획 · 개인 대화', project_id: 40, project_name: '데스크톱 협업', last_event_seq: previewState === 'checkin-on' ? 2 : 1, last_activity_at: at(20), last_authored_seq: previewState === 'checkin-on' ? 2 : null, last_authored_at: previewState === 'checkin-on' ? at(20) : null, last_authored_preview: previewState === 'checkin-on' ? '안녕하세요. 지금 함께 확인할 일이 있나요?' : null, unread_authored: 0, waiting_approvals: 0, active_failures: 0, work_status: 'idle' }]
}
Object.assign(dashboardApi, {
  get: async () => {
    if (previewScreen === 'dashboard' && previewState === 'error') throw new Error('synthetic dashboard failure')
    if (previewState === 'primary-owner-switch' && useAuthStore.getState().user?.id === 2) return { setup: { next_action: 'agent' as const },
      counts: { enabled_connections: 0, ready_agents: 0, ready_projects: 0, active_rooms: 0, unread_authored: 0, waiting_approvals: 0, active_failures: 0 }, rooms: [] }
    return previewState === 'one-room' ? { ...dashboardSummary, counts: { ...dashboardSummary.counts, active_rooms: 1, waiting_approvals: 2 }, rooms: dashboardSummary.rooms.slice(0, 1) } : dashboardSummary
  },
})
let previewSchedule: RoomSchedule = { max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60 }
Object.assign(schedulesApi, {
  get: async (id: number) => {
    if (previewState === 'policy-error') throw new Error('synthetic schedule failure')
    if (starterCommitted?.room_id === id) return { max_rounds: 2, max_tool_calls: 0, max_executor_seconds: 60 }
    if (id === 504 && templateRoomSchedule) return templateRoomSchedule
    if (launchedRoomSchedules.has(id)) return launchedRoomSchedules.get(id)!
    return previewSchedule
  },
  put: async (_id: number, value: RoomSchedule) => { previewSchedule = value; return previewSchedule },
})
Object.assign(roomTemplatesApi, {
  list: async (projectId: number) => previewTemplates.filter((item) => item.project_id === projectId),
  create: async (projectId: number, value: RoomTemplateWrite) => {
    if (previewState === 'template-error') throw Object.assign(new Error('synthetic unavailable team'), {
      response: { status: 409, data: { detail: { code: 'room_template_members_unavailable', agent_ids: [12] } } },
    })
    const created = { ...value, agent_ids: [...value.agent_ids].sort((a, b) => a - b), id: 72, user_id: 1, project_id: projectId,
      created_at: at(26), updated_at: at(26) }
    previewTemplates = [...previewTemplates, created]
    return created
  },
  update: async (_projectId: number, templateId: number, value: RoomTemplateWrite) => {
    const updated = { ...previewTemplates.find((item) => item.id === templateId)!, ...value,
      agent_ids: [...value.agent_ids].sort((a, b) => a - b), updated_at: at(26) }
    previewTemplates = previewTemplates.map((item) => item.id === templateId ? updated : item)
    return updated
  },
  delete: async (_projectId: number, templateId: number) => { previewTemplates = previewTemplates.filter((item) => item.id !== templateId) },
  createRoom: async (_projectId: number, templateId: number) => {
    const template = previewTemplates.find((item) => item.id === templateId)!
    if (previewState === 'template-error') throw Object.assign(new Error('synthetic unavailable team'), {
      response: { status: 409, data: { detail: { code: 'room_template_members_unavailable', agent_ids: [12] } } },
    })
    const room = { id: 504, user_id: 1, project_id: 40, title: template.room_title, is_active: true, created_at: at(27), updated_at: at(27) }
    previewRooms.set(room.id, room)
    templateRoomMembers = [...template.agent_ids]
    templateRoomSchedule = { max_rounds: template.max_rounds, max_tool_calls: template.max_tool_calls,
      max_executor_seconds: template.max_executor_seconds }
    return room
  },
})
Object.assign(workspaceBlueprintApi, {
  export: async () => previewBlueprint,
  preview: async () => {
    if (previewState === 'workspace-error') throw new Error('synthetic invalid workspace')
    return { version: 1, plan_hash: 'a'.repeat(64), counts: { connections: 1, agents: 1, projects: 1,
    project_assignments: 1, room_templates: 1, template_members: 1 },
  connections: [{ ref: 'connection-1', source_name: 'Alibaba Coding Plan', target_name: 'Alibaba Coding Plan (imported 2)' }],
  agents: [{ ref: 'agent-1', source_name: '가져온 기획', target_name: '가져온 기획' }],
  projects: [{ ref: 'project-1', source_name: '데스크톱 협업', target_name: '데스크톱 협업 (imported 2)' }],
  room_templates: [{ ref: 'template-1', project_ref: 'project-1', source_name: '릴리스 팀', target_name: '릴리스 팀' }],
    warnings: [{ code: 'connections_need_credentials', count: 1 }] }
  },
  apply: async (_blueprint: WorkspaceBlueprint, planHash: string) => {
    if (previewState === 'workspace-stale') throw Object.assign(new Error('synthetic stale plan'), {
      response: { status: 409, data: { detail: { code: 'workspace_plan_stale' } } },
    })
    importedConnection = { ...connection, id: 3, label: 'Alibaba Coding Plan (imported 2)', has_secret: false, capabilities: {
      declared: { max_tokens: true, temperature: true, tools: true, vision: true }, verified: { models: {} },
    } }
    importedAgent = { ...agents[0], id: 13, name: '가져온 기획', connection_id: 3 }
    importedProject = { id: 41, user_id: 1, name: '데스크톱 협업 (imported 2)', description: '가져온 작업 공간', is_active: true, agents: [importedAgent] }
    previewTemplates = [...previewTemplates, { id: 73, user_id: 1, project_id: 41, name: '릴리스 팀', room_title: '가져온 릴리스 점검', agent_ids: [13],
      max_rounds: 3, max_tool_calls: 12, max_executor_seconds: 180, created_at: at(28), updated_at: at(28) }]
    return { plan_hash: planHash, counts: { connections: 1, agents: 1, projects: 1, project_assignments: 1, room_templates: 1, template_members: 1 },
      connections: [{ ref: 'connection-1', id: 3, label: 'Alibaba Coding Plan (imported 2)' }], agents: [{ ref: 'agent-1', id: 13, name: importedAgent.name }],
      projects: [{ ref: 'project-1', id: 41, name: importedProject.name }],
      room_templates: [{ ref: 'template-1', project_ref: 'project-1', id: 73, name: '릴리스 팀' }], credentials_unset: 1 }
  },
})
Object.assign(summariesApi, {
  create: async (_id: number, value: HandoffNoteWrite, idempotencyKey: string) => {
    if (previewState === 'handoff-error') throw new Error('synthetic handoff failure')
    if (previewState === 'handoff-busy') throw Object.assign(new Error('synthetic busy range'), {
      response: { status: 409, data: { detail: { code: 'checkpoint_source_busy' } } },
    })
    return { ...event(7, 'system', null, '시스템', 'summary', { ...value, owner_checkpoint: true }), idempotency_key: idempotencyKey }
  },
})
Object.assign(scopeStopsApi, { latest: async () => null, read: async () => { throw new Error('Synthetic stop receipt absent') }, confirm: async () => { throw new Error('Synthetic stop receipt absent') } })
Object.assign(toolsApi, {
  rootActions: async (messageId: string, rootRef: import('../src/api/botMessages').BotMessageRef) => ({ version: 1 as const, message_id: messageId, root_ref: rootRef, actions: [] }),
  activity: async (id: number) => ['duplicate-failure', 'empty'].includes(previewState) ? [] : id >= 503 || previewState.startsWith('finalize') || previewState === 'attention-stale' || previewState === 'inspector-archived' ? []
    : previewState.startsWith('retry') ? inspectorRetryStarted ? [{ ...runningAction, agent_id: 9, trigger_event_id: inspectorRetryEvent }] : []
    : previewState === 'inspector' ? [{ ...pendingAction, trigger_event_id: inspectorTrigger }]
    : previewState === 'recovery' ? [pendingAction, runningAction, uncertainAction]
    : previewState === 'media' ? [completedAction] : previewState === 'quiet' || previewState === 'long' ? [] : [pendingAction, runningAction], grants: async () => [],
  broadMode: async () => ({ enabled: false, scope: null, expires_at: null }),
  setBroadMode: async () => ({ enabled: false, scope: null, expires_at: null }),
  lease: async () => ({ active: false, owner: null, another_profile_or_room: false }),
  preview: async () => ({ action_id: pendingAction.action_id, family: pendingAction.family, target: pendingAction.target,
    expected_effect: pendingAction.expected_effect, arguments: { url: pendingAction.target },
    scope_description: 'Browser navigation to this exact local origin for this room session.',
    scope_target: pendingAction.target, workflow_id: 'synthetic-preview', payload_digest: 'synthetic',
    review_id: 'ad1e0e6c-9eeb-4571-864b-f2592c44db38',
    approval_policy: { scopes: ['once', 'room_session'], unverified_context_required: true, start_within_seconds: 60 },
  }),
  decide: async () => ({}), revoke: async () => undefined,
  screenshot: async (_roomId: number, actionId: string) => {
    if (previewState === 'gallery' && actionId === galleryRemovedAction) {
      throw Object.assign(new Error('synthetic removed artifact'), { response: { status: 410 } })
    }
    if (previewState === 'gallery' && actionId === galleryUnavailableAction) throw new Error('synthetic unavailable artifact')
    return (await fetch(screenshotFixture)).blob()
  },
  deleteScreenshot: async (_roomId: number, actionId: string) => {
    galleryItems = galleryItems.map((item) => item.action_id === actionId ? { ...item, state: 'removed' } : item)
  },
})

let galleryItems: ScreenshotArtifactItem[] = [
  { room_id: roomId, event_seq: 5, action_id: screenshotAction, created_at: screenshotEvent.created_at, width: 1440, height: 1024, state: 'available' },
  { room_id: roomId, event_seq: 6, action_id: galleryRemovedAction, created_at: galleryEvents[5].created_at, width: 1280, height: 720, state: 'removed' },
  { room_id: roomId, event_seq: 7, action_id: galleryUnavailableAction, created_at: galleryEvents[6].created_at, width: 1024, height: 768, state: 'unavailable' },
]
Object.assign(artifactsApi, {
  list: async (_roomId: number, afterSeq: number) => {
    if (previewState === 'gallery-error') throw new Error('synthetic gallery list failure')
    if (previewState !== 'gallery') return { version: 1, room_id: roomId, after_seq: afterSeq, scanned_through_seq: afterSeq, next_after_seq: null, items: [] }
    const first = afterSeq === 0
    const items = galleryItems.filter((item) => first ? item.event_seq <= 6 : item.event_seq > afterSeq)
    return { version: 1, room_id: roomId, after_seq: afterSeq, scanned_through_seq: first ? 6 : 7,
      next_after_seq: first ? 6 : null, items }
  },
})
Object.assign(requestInspectorApi, {
  get: async (_roomId: number, triggerEventId: string) => {
    if (!previewState.startsWith('inspector') && !previewState.startsWith('retry') && !previewState.startsWith('finalize') && previewState !== 'skill-source-task') throw new Error('synthetic request not found')
    if (previewState.startsWith('finalize') && finalizeStarted && triggerEventId === finalizeEventId) {
      const chosen = agents.find((item) => item.id === finalizeAgentId)!
      return {
      version: 1, room_id: roomId, room_title: '출시 준비실', read_only: false,
      request: { event_id: finalizeEventId, seq: 5, created_at: events.find((item) => item.event_id === finalizeEventId)!.created_at,
        actor_name_snapshot: '지민', preview: '동료 검토를 반영한 최종 결과를 작성해 주세요.', href: `/chat/${roomId}?seq=5` },
      execution: { phase: 'completed', selected_agent_count: 1, current_round: 1,
        selected_agents: [{ agent_id: chosen.id, name_snapshot: chosen.name, live_state: 'idle' }],
        policy: { max_rounds: 1, max_tool_calls: 0, max_executor_seconds: 60 } },
      completion: { state: 'completed', outcome_status: 'succeeded', result_available: true, completed_rounds: 1, pending_agent_count: 0 },
      outcomes: [{ event_id: finalizeResultId, seq: 6, round: 1, agent_id: chosen.id, agent_name_snapshot: chosen.name, kind: 'message', phase: 'succeeded',
        summary_code: 'response', model_use: { connection_id: chosen.connection_id, model_id: chosen.model_id,
          provider_kind: chosen.connection_id === 2 ? 'anthropic' : 'compatible', watermark_seq: 5 }, href: `/chat/${roomId}?seq=6` }],
      outcome_count: 1, outcomes_truncated: false, actions: [], action_count: 0, actions_truncated: false,
      }
    }
    if (previewState.startsWith('retry') && inspectorRetryStarted && triggerEventId === inspectorRetryEvent) return {
      version: 1, room_id: roomId, room_title: '데스크톱 협업', read_only: false,
      request: { event_id: inspectorRetryEvent, seq: 5, created_at: events.find((item) => item.event_id === inspectorRetryEvent)!.created_at,
        actor_name_snapshot: '지민', preview: '출시 전 검사 결과를 정확히 정리해 줘.', href: `/chat/${roomId}?seq=5` },
      execution: { phase: 'running', selected_agent_count: 1, current_round: 1,
        selected_agents: [{ agent_id: 9, name_snapshot: '검토', live_state: 'running' }],
        policy: { max_rounds: 1, max_tool_calls: 5, max_executor_seconds: 45 } },
      completion: { state: 'running', outcome_status: 'pending', result_available: false, completed_rounds: 0, pending_agent_count: 1 },
      outcomes: [], outcome_count: 0, outcomes_truncated: false,
      actions: [], action_count: 0, actions_truncated: false,
    }
    if (triggerEventId === inspectorLaterTrigger) return { version: 1, room_id: roomId, room_title: '데스크톱 협업', read_only: previewState === 'inspector-archived',
      request: { event_id: inspectorLaterTrigger, seq: 5, created_at: inspectorEvents[4].created_at, actor_name_snapshot: '지민', preview: '후속 요청은 별도로 처리해 줘.', href: `/chat/${roomId}?seq=5` },
      execution: { phase: 'completed', selected_agent_count: 1, current_round: 1,
        selected_agents: [{ agent_id: 8, name_snapshot: '기획 (후속 요청 당시)', live_state: null }],
        policy: { max_rounds: 2, max_tool_calls: 8, max_executor_seconds: 60 } },
      completion: { state: 'completed', outcome_status: 'succeeded', result_available: true, completed_rounds: 1, pending_agent_count: 0 },
      outcomes: [{ event_id: inspectorEvents[5].event_id, seq: 6, round: 1, agent_id: 8, agent_name_snapshot: '기획 (후속 요청 당시)', kind: 'message', phase: 'succeeded',
        summary_code: 'response', model_use: { connection_id: 1, model_id: 'qwen3.7-plus-2026-09', provider_kind: 'compatible', watermark_seq: 5 }, href: `/chat/${roomId}?seq=6` }],
      outcome_count: 1, outcomes_truncated: false, actions: [], action_count: 0, actions_truncated: false }
    if (triggerEventId !== inspectorTrigger) throw new Error('synthetic request not found')
    const finalizeFixture = previewState.startsWith('finalize')
    const finalizeAuthority = finalizeFixtureAuthority(previewState === 'finalize-blocked')
    const retryFixture = previewState.startsWith('retry')
    const blockedRetry = previewState === 'retry-blocked'
    return { version: 1, room_id: roomId, room_title: '데스크톱 협업', read_only: previewState === 'inspector-archived',
      request: { event_id: inspectorTrigger, seq: 1, created_at: inspectorEvents[0].created_at, actor_name_snapshot: '지민',
        preview: finalizeFixture ? '첫 출시 계획을 검토하고 결론을 제안해 줘.' : '출시 전 검사 결과를 정확히 정리해 줘.', href: `/chat/${roomId}?seq=1` },
      execution: { phase: finalizeFixture || retryFixture ? 'completed' : 'interrupted', selected_agent_count: finalizeFixture ? 3 : 2, current_round: 1,
        selected_agents: [{ agent_id: 8, name_snapshot: '기획 (요청 당시)', live_state: null },
          { agent_id: 9, name_snapshot: '검토 (요청 당시)', live_state: finalizeFixture || retryFixture ? null : 'waiting' },
          ...(finalizeFixture ? [{ agent_id: 10, name_snapshot: '테스트 (요청 당시)', live_state: null }] : [])],
        policy: { max_rounds: 3, max_tool_calls: 5, max_executor_seconds: 45 } },
      completion: { state: finalizeFixture || retryFixture ? 'completed' : 'interrupted', outcome_status: 'partial', result_available: true,
        completed_rounds: 1, pending_agent_count: 0 },
      outcomes: [
        { event_id: inspectorOutcome, seq: 2, round: 1, agent_id: 8, agent_name_snapshot: '기획 (요청 당시)', kind: 'message', phase: 'succeeded',
          summary_code: 'response', model_use: { connection_id: 1, model_id: 'qwen3.7-plus-2026-08', provider_kind: 'compatible', watermark_seq: 1 }, href: `/chat/${roomId}?seq=2` },
        { event_id: inspectorFailure, seq: 4, round: 1, agent_id: finalizeFixture ? 10 : 9, agent_name_snapshot: finalizeFixture ? '테스트 (요청 당시)' : '검토 (요청 당시)', kind: 'failure', phase: 'failed',
          summary_code: 'failure_connection', model_use: null, href: `/chat/${roomId}?seq=4` },
        ...(finalizeFixture ? [{ event_id: finalizeSecondOutcome, seq: 3, round: 1, agent_id: 9, agent_name_snapshot: '검토 (요청 당시)', kind: 'message', phase: 'succeeded',
          summary_code: 'response', model_use: { connection_id: 2, model_id: 'claude-sonnet-2026-07', provider_kind: 'anthropic', watermark_seq: 1 }, href: `/chat/${roomId}?seq=3` }] : []),
      ].sort((left, right) => left.seq - right.seq), outcome_count: finalizeFixture ? 3 : 2, outcomes_truncated: false,
      actions: finalizeFixture ? finalizeAuthority.hasWaitingAction ? [
        { action_id: pendingAction.action_id, agent_id: 10, state: 'waiting', phase: 'approval', summary_code: 'action_browser', requested_at: at(16),
          started_at: null, completed_at: null, result_event_id: null, result_seq: null, href: `/chat/${roomId}?seq=1&action=${pendingAction.action_id}` },
      ] : [] : retryFixture ? blockedRetry ? [
        { action_id: uncertainAction.action_id, agent_id: 9, state: 'uncertain', phase: 'uncertain', summary_code: 'action_files', requested_at: at(16),
          started_at: at(16), completed_at: null, result_event_id: null, result_seq: null, href: `/chat/${roomId}?seq=1` },
      ] : [] : [
        { action_id: screenshotAction, agent_id: 8, state: 'completed', phase: 'completed', summary_code: 'action_screen', requested_at: at(15),
          started_at: at(15), completed_at: at(16), result_event_id: inspectorToolResult, result_seq: 3, href: `/chat/${roomId}?seq=3` },
        { action_id: pendingAction.action_id, agent_id: 9, state: 'waiting', phase: 'approval', summary_code: 'action_browser', requested_at: at(16),
          started_at: null, completed_at: null, result_event_id: null, result_seq: null, href: `/chat/${roomId}?seq=1&action=${pendingAction.action_id}` },
      ], action_count: finalizeFixture ? finalizeAuthority.hasWaitingAction ? 1 : 0 : retryFixture ? blockedRetry ? 1 : 0 : 2, actions_truncated: false }
  },
})
let retryAttempt = 0
const toolReviewRetryReceipts = new Map<string, import('../src/api/requestRetries').RequestRetryResult>()
Object.assign(requestRetriesApi, {
  preflight: async (_roomId: number, failureEventId: string) => {
    if (previewState.startsWith('tool-review-retry-') && failureEventId === 'f01a0000-0000-4000-8000-000000000003') {
      const reason = previewState === 'tool-review-retry-effects' ? 'completed_effect' : previewState === 'tool-review-retry-context' ? 'context_unavailable' : null
      return {version:1,room_id:roomId,failure_event_id:failureEventId,failure_seq:2,source_request:{event_id:'f01a0000-0000-4000-8000-000000000001',seq:1,preview:'브라우저에서 화면을 확인해 줘.',href:`/chat/${roomId}?seq=1`},agent:{agent_id:8,name_snapshot:'기획',current_name:'기획'},current_model:{connection_id:1,model_id:'qwen3.7-plus',provider_kind:'compatible'},limits:{max_rounds:1,max_tool_calls:5,max_executor_seconds:45},eligible:!reason,blocked_reason:reason,plan_hash:reason?null:'c'.repeat(64)}
    }
    if (!previewState.startsWith('retry') || failureEventId !== inspectorFailure) throw new Error('synthetic retry not found')
    if (previewState === 'retry-error') throw new Error('synthetic retry preflight failure')
    const blocked = previewState === 'retry-blocked'
    return { version: 1, room_id: roomId, failure_event_id: inspectorFailure, failure_seq: 4,
      source_request: { event_id: inspectorTrigger, seq: 1, preview: '출시 전 검사 결과를 정확히 정리해 줘.', href: `/chat/${roomId}?seq=1` },
      agent: { agent_id: 9, name_snapshot: '검토 (요청 당시)', current_name: '검토' },
      current_model: { connection_id: 2, model_id: 'claude-sonnet', provider_kind: 'anthropic' },
      limits: { max_rounds: 1, max_tool_calls: 5, max_executor_seconds: 45 },
      eligible: !blocked, blocked_reason: blocked ? 'action_uncertain' : null, plan_hash: blocked ? null : 'c'.repeat(64) }
  },
  start: async (_roomId: number, failureEventId: string, planHash: string, key: string) => {
    if (previewState.startsWith('tool-review-retry-') && failureEventId === 'f01a0000-0000-4000-8000-000000000003') {
      if (previewState === 'tool-review-retry-stale') throw {response:{status:409,data:{detail:{code:'request_retry_plan_stale'}}}}
      if (planHash !== 'c'.repeat(64)) throw Error('Synthetic changed plan')
      const previous=toolReviewRetryReceipts.get(key)
      if(previous)return {...previous,replayed:true}
      const result={version:1 as const,room_id:roomId,source_failure_event_id:failureEventId,source_trigger_event_id:'f01a0000-0000-4000-8000-000000000001',source_request_event_id:'f01a0000-0000-4000-8000-000000000001',retry_event_id:'f01a0000-0000-4000-8000-000000000004',retry_seq:3,agent_id:8,replayed:false,href:`/chat/${roomId}?seq=3`}
      toolReviewRetryReceipts.set(key,result)
      events=[...events,{...event(3,'user',1,'지민','message',{content:'브라우저에서 화면을 확인해 줘.',source_failure_event_id:failureEventId,selected_agent_ids:[8],max_rounds:1,max_tool_calls:5,max_executor_seconds:45}),event_id:result.retry_event_id}]
      if(previewState==='tool-review-retry-unknown')throw Error('Synthetic lost acceptance')
      return result
    }
    retryAttempt += 1
    if (previewState === 'retry-delayed' && retryAttempt === 1) throw new Error('synthetic lost retry response')
    if (failureEventId !== inspectorFailure || planHash !== 'c'.repeat(64)) throw new Error('synthetic stale retry')
    inspectorRetryStarted = true
    if (!events.some((item) => item.event_id === inspectorRetryEvent)) events = [...events, {
      ...event(5, 'user', 1, '지민', 'message', { content: '출시 전 검사 결과를 정확히 정리해 줘.', source_failure_event_id: inspectorFailure,
        selected_agent_ids: [9], max_rounds: 1, max_tool_calls: 5, max_executor_seconds: 45 }), event_id: inspectorRetryEvent,
    }]
    return { version: 1, room_id: roomId, source_failure_event_id: inspectorFailure, source_trigger_event_id: inspectorTrigger,
      source_request_event_id: inspectorTrigger, retry_event_id: inspectorRetryEvent, retry_seq: 5, agent_id: 9,
      replayed: retryAttempt > 1, href: `/chat/${roomId}?seq=5` }
  },
})
let finalizeAttempt = 0
Object.assign(requestFinalizeApi, {
  preview: async (_roomId: number, sourceRequestId: string, agentId: number) => {
    if (!previewState.startsWith('finalize') || sourceRequestId !== inspectorTrigger) throw new Error('synthetic final result source not found')
    const authority = finalizeFixtureAuthority(previewState === 'finalize-blocked')
    const chosen = agents.find((item) => item.id === agentId)!
    return { version: 1, room_id: roomId,
      source_request: { event_id: inspectorTrigger, seq: 1, preview: '첫 출시 계획을 검토하고 결론을 제안해 줘.', href: `/chat/${roomId}?seq=1` },
      source_completion: { state: 'completed', outcome_status: 'partial', result_available: true },
      outcomes: [
        { event_id: inspectorOutcome, seq: 2, round: 1, agent_id: 8, agent_name_snapshot: '기획 (요청 당시)', kind: 'message', phase: 'succeeded', summary_code: 'response',
          model_use: { connection_id: 1, model_id: 'qwen3.7-plus-2026-08', provider_kind: 'compatible', watermark_seq: 1 }, href: `/chat/${roomId}?seq=2` },
        { event_id: finalizeSecondOutcome, seq: 3, round: 1, agent_id: 9, agent_name_snapshot: '검토 (요청 당시)', kind: 'message', phase: 'succeeded', summary_code: 'response',
          model_use: { connection_id: 2, model_id: 'claude-sonnet-2026-07', provider_kind: 'anthropic', watermark_seq: 1 }, href: `/chat/${roomId}?seq=3` },
      ], outcome_count: 3, outcomes_truncated: false,
      excluded_agents: [{ agent_id: 10, name_snapshot: '테스트 (요청 당시)', reason: 'failed' }],
      agent: { agent_id: agentId, name_snapshot: `${chosen.name} (요청 당시)`, current_name: chosen.name },
      current_model: { connection_id: chosen.connection_id, model_id: chosen.model_id, provider_kind: chosen.connection_id === 2 ? 'anthropic' : 'compatible' },
      limits: { max_rounds: 1, max_tool_calls: 0, max_executor_seconds: 60 },
      source_coverage: { source_request_event_id: inspectorTrigger,
        source_outcome_event_ids: [inspectorOutcome, finalizeSecondOutcome, inspectorFailure],
        included_message_event_ids: [inspectorOutcome, finalizeSecondOutcome], omitted_event_ids: [inspectorFailure], truncations: [], used_chars: 188, limit_chars: 32000 },
      eligible: authority.eligible, blocked_reason: authority.blockedReason, plan_hash: authority.planHash }
  },
  execute: async (_roomId: number, sourceRequestId: string, body: { agent_id: number; plan_hash: string }) => {
    finalizeAttempt += 1
    if (previewState === 'finalize-delayed' && finalizeAttempt === 1) throw new Error('synthetic lost final result response')
    if (sourceRequestId !== inspectorTrigger || body.plan_hash !== 'd'.repeat(64)) throw new Error('synthetic stale final result plan')
    const chosen = agents.find((item) => item.id === body.agent_id)!
    finalizeStarted = true
    finalizeAgentId = body.agent_id
    if (!events.some((item) => item.event_id === finalizeEventId)) events = [...events,
      { ...event(5, 'user', 1, '지민', 'message', { content: '동료 검토를 반영한 최종 결과를 작성해 주세요.', finalize: { version: 1,
        source_request_event_id: inspectorTrigger, source_outcome_event_ids: [inspectorOutcome, finalizeSecondOutcome, inspectorFailure], agent_id: body.agent_id } }), event_id: finalizeEventId },
      { ...event(6, 'agent', body.agent_id, chosen.name, 'message', { content: '최종 권고는 소수 사용자 공개부터 시작하고, 오류율 기준을 넘으면 즉시 중단하는 것입니다.',
        trigger_event_id: finalizeEventId, round: 1, model_use: { connection_id: chosen.connection_id,
          model_id: chosen.model_id, provider_kind: chosen.connection_id === 2 ? 'anthropic' : 'compatible', watermark_seq: 5 } }), event_id: finalizeResultId }]
    return { version: 1, room_id: roomId, source_request_event_id: inspectorTrigger,
      source_outcome_event_ids: [inspectorOutcome, finalizeSecondOutcome, inspectorFailure], finalize_event_id: finalizeEventId,
      finalize_seq: 5, agent_id: body.agent_id, replayed: finalizeAttempt > 1, href: `/chat/${roomId}?seq=5` }
  },
})

installSupportingFixtures(previewState)
if(previewState.startsWith('bot-template-')) {
 connectionsApi.models=async(id:number)=>({observed_at:at(20),stale:false,truncated:false,models:[{id:id===1?'qwen3.7-plus':'claude-sonnet',label:id===1?'qwen3.7-plus':'claude-sonnet',source:'configured'}]})
 connectionsApi.revisions=async()=>({version:1,config_revision:1,auth_grant_revision:1})
}


if(previewState.startsWith('bot-library-')||previewState.startsWith('page-revision-')||previewState.startsWith('page-workspace-')||previewState==='page-created') {
 const ids=[1,2,3].map(n=>`af041000-0000-4000-8000-${String(n+800).padStart(12,'0')}`),sourceId='af041000-0000-4000-8000-000000000810'
 const screenshotLibrary=previewState==='bot-library-screenshot'
 const source=screenshotLibrary?{...screenshotEvent,actor_id:null,actor_name_snapshot:'',event_id:sourceId}: {...event(1,'agent',8,'기획','message',{content:'검토한 내용을 정리했습니다.'}),event_id:sourceId};events=screenshotLibrary?[...baseEvents.map(item=>({...item,event_id:`af041000-0000-4000-8000-${String(item.seq+810).padStart(12,'0')}`})),source]:[source]
 if(screenshotLibrary)toolsApi.activity=async()=>[]
 if(previewState.startsWith('bot-library-search-'))libraryUploadsApi.list=async bot=>({bot_id:bot,items:[],next_cursor:null})
 const sourceSeq=source.seq
 let renamedTitle='늦은 페이지 — 검색 대상',renamedVersion=1,renameAttempt=0,renameReceipt:import('../src/api/roomResults').PageRenameReceipt|null=null
 const rows:BotLibraryItem[]=ids.map((id,i)=>({result_id:id,room_id:501,version:i===0?2:1,kind:(['text','link','file'] as const)[i],title:['출시 준비 요약','검토 자료 링크','화면 기록'][i],author_agent_id:8,author_name_snapshot:'기획',state:'draft',state_revision:1,created_at:at(1),updated_at:at(3),linked_opinion_count:0,source_href:`/chat/501?seq=${sourceSeq}&event=${sourceId}`,detail_href:`/conversations/501/results/${id}/versions/${i===0?2:1}`}))
 agentsApi.get=async id=>{const a=agents.find(x=>x.id===id);if(!a)throw {response:{status:404}};return a}
 botLibraryApi.list=async(id,after,query='')=>{if(previewState==='bot-library-search-loading'&&query)return await new Promise<never>(()=>{});if(previewState==='bot-library-error'||previewState==='bot-library-search-error'&&!!query)throw Error('Synthetic read failure');if(previewState==='bot-library-revoked'||id!==8)throw {response:{status:404}};if(previewState.startsWith('bot-library-search-')){if(previewState==='bot-library-search-stale'&&after)throw {response:{status:422,data:{detail:{code:'result_library_search_cursor_invalid'}}}};const all:BotLibraryItem[]=Array.from({length:55},(_,i)=>({...rows[i%rows.length],result_id:`bf041000-0000-4000-8000-${String(i+1).padStart(12,'0')}`,title:i===54?renamedTitle:`합성 결과 ${String(i+1).padStart(2,'0')}`,kind:i===54?'page':rows[i%rows.length].kind,...(i===54?{version:renamedVersion,detail_href:`/conversations/501/results/bf041000-0000-4000-8000-000000000055/versions/${renamedVersion}`}:{})}));const asciiLower=(v:string)=>v.replace(/[A-Z]/g,c=>c.toLowerCase());const filtered=all.filter(row=>asciiLower(row.title).includes(asciiLower(query))&&(!after||row.result_id>after.split('~')[0]));const items=filtered.slice(0,50);return {agent_id:id,items,next_cursor:filtered.length>50?`${items[items.length-1].result_id}${query?'~'+'a'.repeat(64):''}`:null}}return {agent_id:id,items:previewState==='bot-library-empty'?[]:structuredClone(screenshotLibrary?rows.filter(row=>row.kind==='file'):rows),next_cursor:null}}
 if(previewState.startsWith('bot-library-search-rename')) {
  roomResultsApi.renamePage=async(room,id,version,title)=>{if(previewState==='bot-library-search-rename-missing'&&renameAttempt++===0)throw Error('Synthetic lost request');if(previewState==='bot-library-search-rename-conflict')throw {response:{status:409,data:{detail:{code:'result_page_version_changed'}}}};const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(title));const digest=Array.from(new Uint8Array(bytes),x=>x.toString(16).padStart(2,'0')).join('');renamedTitle=title;renamedVersion=version+1;renameReceipt={version:1,room_id:room,result_id:id,base_version:version,page_version:version+1,title_sha256:digest,event_id:'af041000-0000-4000-8000-000000000831',seq:2,replayed:false};if(previewState==='bot-library-search-rename-unknown')throw Error('Synthetic lost response');return renameReceipt}
  roomResultsApi.renameReceipt=async()=>{if(!renameReceipt)throw {response:{status:404,data:{detail:{code:'result_rename_receipt_not_found'}}}};return {...renameReceipt,replayed:true}}
 }
 roomResultsApi.list=async room=>({room_id:room,items:structuredClone(screenshotLibrary?rows.filter(row=>row.kind==='file'):rows),next_cursor:null})
 roomResultsApi.detail=async(room,id,version)=>{const row=rows.find(x=>x.result_id===id);if(!row||room!==501||version<1||version>row.version)throw Error('Synthetic result unavailable');return {...row,version,current_version:row.version,source:{event_id:sourceId,seq:sourceSeq,href:row.source_href},execution:{trigger_event_id:null,round:null,generation:null,model_use:null},content:row.kind==='file'?'':row.kind==='link'?'https://example.invalid/reference':'합성 결과 내용입니다.',file_ref:row.kind==='file'?{adapter:'screenshot',action_id:screenshotLibrary?screenshotAction:'af041000-0000-4000-8000-000000000811',event_seq:sourceSeq,image_width:screenshotLibrary?1440:null,image_height:screenshotLibrary?1024:null}:null,file_status:row.kind==='file'?(screenshotLibrary?'unchecked':'unavailable'):null,evidence:[],evidence_status:'none',review_support:row.kind==='file'?'unsupported':'source_reply_only',link_report:row.kind==='link'?'unchecked':null,state_owner_id:null,state_changed_at:null,link_report_owner_id:null,link_reported_at:null} as ResultDetail}
 roomResultsApi.versions=async(room,id)=>{const row=rows.find(x=>x.result_id===id);if(!row)throw Error('Synthetic result unavailable');return {room_id:room,result_id:id,current_version:row.version,items:Array.from({length:row.version},(_,i)=>({...row,version:row.version-i})),next_before_version:null}}
 roomResultsApi.reviews=async()=>({items:[],next_cursor:null})
 if(previewState.startsWith('page-workspace-')||previewState==='page-created') {
  const doc=JSON.stringify({format:'gamja.page.v1',title:'합성 작업 현황',datasets:[{id:'tasks',columns:[{key:'name',label:'항목',type:'text'},{key:'amount',label:'금액',type:'number'},{key:'status',label:'상태',type:'text'}],rows:[{name:'Alpha',amount:12,status:'open'},{name:'Beta',amount:9,status:'done'},{name:'Gamma',amount:3,status:'open'}]}],views:[{id:'main',dataset:'tasks',filters:['status_filter']}],blocks:[{type:'heading',level:1,text:'작업 현황'},{type:'control',id:'status_filter',view:'main',field:'status',mode:'select',label:'상태'},{type:'columns',items:[{type:'metric',view:'main',aggregate:'count',label:'작업 수'},{type:'metric',view:'main',aggregate:'sum',field:'amount',label:'금액 합계'}]},{type:'table',view:'main',columns:['name','amount','status'],title:'작업 목록'}]})
  rows[0]={...rows[0],kind:'page',title:'합성 작업 현황'}
  if(previewState==='page-created')events=[
   {...event(1,'user',1,'지민','message',{content:'합성 작업 현황 페이지를 만들어 줘.'}),event_id:'af041000-0000-4000-8000-000000000819'},
   {...event(2,'tool',null,'도구','tool_result',{family:'write_page',status:'completed',trigger_event_id:'af041000-0000-4000-8000-000000000819',page_result:{version:1,room_id:501,result_id:ids[0],page_version:1,base_version:null,title:'합성 작업 현황',document_sha256:'a'.repeat(64),agent_id:8}}),event_id:sourceId},
   {...event(3,'user',1,'지민','message',{content:'합성 페이지를 업데이트해 줘.',page_target:{result_id:ids[0],version:1}}),event_id:'af041000-0000-4000-8000-000000000826'},
   {...event(4,'tool',null,'도구','tool_result',{family:'write_page',status:'completed',trigger_event_id:'af041000-0000-4000-8000-000000000826',page_result:{version:1,room_id:501,result_id:ids[0],page_version:2,base_version:1,title:'합성 작업 현황',document_sha256:'b'.repeat(64),agent_id:8}}),event_id:'af041000-0000-4000-8000-000000000827'}]

  const previousDetail=roomResultsApi.detail
  roomResultsApi.detail=async(room,id,version)=>({...await previousDetail(room,id,version),content:doc,...(previewState==='page-created'?{source:{event_id:version===1?sourceId:'af041000-0000-4000-8000-000000000827',seq:version===1?2:4,href:`/chat/501?seq=${version===1?2:4}`}}:{})})
  roomResultsApi.pageCurrent=async(room,id)=>roomResultsApi.detail(room,id,2)
  roomResultsApi.pageConversation=async(room,id)=>{if(previewState==='page-workspace-error')throw Error('Synthetic history error');return {version:1,room_id:room,result_id:id,upper_seq:1,items:[],root_states:[],next_after_seq:null}}
  let saved:RoomEvent|null=null
  eventsApi.sendPage=async(_room,content,target,key)=>{saved={...event(2,'user',1,'지민','message',{content,output_kind:'auto',reply_to:null,page_target:target}),event_id:'af041000-0000-4000-8000-000000000822',idempotency_key:key};if(previewState==='page-workspace-unknown')throw Error('Synthetic response lost');return saved}
  eventsApi.pageReceipt=async()=>{if(!saved)throw {response:{status:404}};return saved}
  toolsApi.activity=async()=>[]
  if(previewState==='page-workspace-failure'||previewState==='page-workspace-failure-generic') {
   const root='af041000-0000-4000-8000-000000000850',actionId='af041000-0000-4000-8000-000000000851'
   rows[0]={...rows[0],version:1,detail_href:`/conversations/501/results/${ids[0]}/versions/1`}
   const request={...event(2,'user',1,'지민','message',{content:'합성 페이지를 업데이트해 줘.',page_target:{result_id:ids[0],version:1}}),event_id:root}
   const tool={...event(3,'tool',null,'도구','tool_result',{action_id:actionId,trigger_event_id:root,state:'completed',family:'mcp_tool_call'}),event_id:'af041000-0000-4000-8000-000000000852'}
   const failure={...event(4,'agent',8,'기획','failure',{trigger_event_id:root,code:previewState==='page-workspace-failure-generic'?'page_document_invalid':'provider_invalid_response',message:'Private synthetic provider text must not be shown.'}),event_id:'af041000-0000-4000-8000-000000000853'}
   events=[source,request,tool,failure]
   toolsApi.activity=async()=>[{action_id:actionId,room_id:501,agent_id:8,trigger_event_id:root,family:'mcp_tool_call',target:'Synthetic MCP',expected_effect:'Read synthetic result',state:'completed',scope:'once',grant_id:1,requested_at:at(14),started_at:at(15),completed_at:at(16)}]
   roomResultsApi.pageConversation=async(room,id)=>({version:1,room_id:room,result_id:id,upper_seq:4,items:[request,failure],root_states:[{event_id:root,state:'completed'}],next_after_seq:null})
   requestInspectorApi.get=async(room,id)=>({version:1,room_id:room,room_title:'합성 방',read_only:false,request:{event_id:id,seq:2,created_at:at(15),actor_name_snapshot:'지민',preview:'합성 요청',href:'/chat/501?seq=2'},execution:{phase:'completed',selected_agents:[],selected_agent_count:1,policy:null,current_round:1},completion:{state:'completed',outcome_status:'failed',result_available:true,completed_rounds:1,pending_agent_count:0},outcomes:[],outcome_count:0,outcomes_truncated:false,actions:[],action_count:0,actions_truncated:false})
  }
  if(previewState==='page-workspace-finished') {
   const root='af041000-0000-4000-8000-000000000828',answer='af041000-0000-4000-8000-000000000829'
   const request={...event(2,'user',1,'지민','message',{content:'페이지를 정리해 줘.',page_target:{result_id:ids[0],version:2}}),event_id:root}
   const reply={...event(3,'agent',8,'기획','message',{content:'페이지를 정리했습니다.',trigger_event_id:root}),event_id:answer}
   events=[source,request,reply]
   roomResultsApi.pageConversation=async(room,id)=>({version:1,room_id:room,result_id:id,upper_seq:3,items:[request,reply],root_states:[{event_id:root,state:'running'}],next_after_seq:null})
   requestInspectorApi.get=async(room,id)=>({version:1,room_id:room,room_title:'합성 방',read_only:false,request:{event_id:id,seq:2,created_at:at(15),actor_name_snapshot:'지민',preview:'페이지를 정리해 줘.',href:'/chat/501?seq=2'},execution:{phase:'completed',selected_agents:[],selected_agent_count:1,policy:null,current_round:1},completion:{state:'completed',outcome_status:'succeeded',result_available:true,completed_rounds:1,pending_agent_count:0},outcomes:[],outcome_count:0,outcomes_truncated:false,actions:[],action_count:0,actions_truncated:false})
  }
  if(previewState==='page-workspace-approval') {
   const root='af041000-0000-4000-8000-000000000823',actionId='af041000-0000-4000-8000-000000000824',reviewId='af041000-0000-4000-8000-000000000825',target='https://example.invalid/document',scopeTarget='https://example.invalid',digest='e'.repeat(64)
   const request={...event(2,'user',1,'지민','message',{content:'페이지에 반영하기 전에 이 자료를 확인해 줘.',page_target:{result_id:ids[0],version:2}}),event_id:root}
   const action={action_id:actionId,room_id:501,agent_id:8,trigger_event_id:root,family:'browser_navigate',target,expected_effect:'Navigate browser',state:'waiting',scope:null,grant_id:null,requested_at:at(18),started_at:null,completed_at:null,review_id:reviewId}
   events=[source,request];toolsApi.activity=async()=>[action];toolsApi.grants=async()=>[]
   roomResultsApi.pageConversation=async(room,id)=>({version:1,room_id:room,result_id:id,upper_seq:2,items:[request],root_states:[{event_id:root,state:'running'}],next_after_seq:null})
   toolsApi.preview=async()=>({action_id:actionId,family:action.family,target,scope_target:scopeTarget,expected_effect:action.expected_effect,arguments:{url:target},scope_description:'Synthetic reviewed origin',workflow_id:'synthetic-workflow',payload_digest:digest,review_id:reviewId,approval_policy:{scopes:['once'],unverified_context_required:false,start_within_seconds:null},review:{version:1,captured_at:at(18),action:{action_id:actionId,room_id:501,agent_id:8,trigger_event_id:root,family:action.family,target,scope_target:scopeTarget,expected_effect:action.expected_effect,payload_digest:digest},authority:{owner_id:1,room_id:501,agent_id:8,proposer:{owner_id:1,room_id:501,agent_id:8,connection_id:1,provider_id:'synthetic',model_id:'synthetic-model'}},targets:[{role:'browser_origin',status:'verified',origin:scopeTarget}],external_context:{account:'unsupported',target_version:'unsupported'}}})
  }
 }
 if(previewState.startsWith('page-revision-')) {
  const rootId='af041000-0000-4000-8000-000000000820',answerId='af041000-0000-4000-8000-000000000821'
  let receipt:import('../src/api/roomResults').RevisionStatus|null=null, saved=false, startAttempts=0,lastKey=''
  const ref=(id:string,seq:number,actor:'user'|'agent',actorId:number)=>({room_id:501,event_id:id,seq,actor_type:actor,actor_id:actorId,kind:'message' as const,href:`/chat/501?seq=${seq}&event=${id}`})
  const rootRef=ref(rootId,2,'user',1),answerRef=ref(answerId,3,'agent',8)
  const priorDetail=roomResultsApi.detail
  roomResultsApi.detail=async(room,id,version)=>{if(version===3&&saved){const previous=await priorDetail(room,id,2);return {...previous,version:3,current_version:3,content:'수정된 합성 보고서입니다.',source:{event_id:answerId,seq:3,href:answerRef.href}}}const result=await priorDetail(room,id,version);return saved?{...result,current_version:3}:result}
  roomResultsApi.revisionActive=async(room,id)=>({room_id:room,result_id:id,base_version:saved?3:2,revision:null})
  roomResultsApi.revisionStart=async(room,id,version,instruction,key)=>{if(receipt&&key===lastKey){startAttempts++;return structuredClone(receipt)}lastKey=key;if(room!==501||id!==ids[0]||version!==2)throw {response:{status:409}};events=[source,{...event(2,'user',1,'지민','message',{content:instruction,page_revision:{version:1,result_id:id,base_version:version,agent_id:8,instruction}}),event_id:rootId},{...event(3,'agent',8,'기획','message',{content:'수정된 합성 보고서입니다.',trigger_event_id:rootId}),event_id:answerId}];receipt={room_id:room,result_id:id,expected_current_version:version,author_agent_id:8,instruction,root_ref:rootRef,state:'pending'};if(['page-revision-unknown','page-revision-missing'].includes(previewState)&&++startAttempts===1)throw Error('Synthetic response lost');return structuredClone(receipt)}
  roomResultsApi.revisionReceipt=async()=>{if(previewState==='page-revision-missing'&&startAttempts===1||!receipt)throw {response:{status:404}};return structuredClone(receipt)}
  roomResultsApi.revisionStatus=async()=>{if(!receipt)throw Error('Synthetic missing');return {...receipt,state:previewState==='page-revision-conflict'?'conflict':previewState==='page-revision-failed'?'failed':'ready',source_event_id:answerId,source_ref:answerRef}}
  roomResultsApi.revisionFinalize=async()=>{if(!receipt)throw Error('Synthetic missing');saved=true;return {...receipt,state:'completed',result_ref:{room_id:501,result_id:ids[0],version:3,href:`/chat/501?result=${ids[0]}&version=3`,source_event_id:answerId,source_ref:answerRef}}}
 }
}

const routineId='b0000000-0000-4000-8000-000000000001', routineRunId='b0000000-0000-4000-8000-000000000002', routineRootId='b0000000-0000-4000-8000-000000000003', routineResultId='b0000000-0000-4000-8000-000000000004'
let routineFixture: Routine = {version:1,routine_id:routineId,revision:1,agent_id:8,room_id:501,name:'아침 계획 확인',instruction:'오늘 할 일을 간단히 확인해 줘.',enabled:!['routine-paused','routine-chat-paused'].includes(previewState),schedule:{kind:'weekly',weekdays:[0,1,2,3,4],hour:8,minute:0,timezone:'Asia/Seoul'},next_run_at:['routine-paused','routine-chat-paused'].includes(previewState)?null:'2026-11-01T23:00:00Z',availability:['routine-paused','routine-chat-paused'].includes(previewState)?'paused':'available'}
if(previewState.startsWith('routine-webhook-'))routineFixture={...routineFixture,name:'웹훅 예시',schedule:{kind:'webhook'},next_run_at:null,webhook:{configured:previewState==='routine-webhook-configured',generation:previewState==='routine-webhook-configured'?1:0}}
let routineRows: Routine[] = previewState==='routine-empty' || previewState==='routine-new' || previewState==='routine-webhook-empty' ? [] : [routineFixture]
const routineRun: RoutineRun = {run_id:routineRunId,state:previewState==='routine-failed'?'failed':['routine-running','routine-approval'].includes(previewState)?'running':'completed',scheduled_at:at(10),started_at:at(10),completed_at:['routine-running','routine-approval'].includes(previewState)?null:at(11),root_ref:{room_id:501,event_id:routineRootId,seq:1,actor_type:'system',actor_id:null,kind:'activity'},result_ref:['routine-failed','routine-running','routine-approval'].includes(previewState)?null:{room_id:501,event_id:routineResultId,seq:2,actor_type:'agent',actor_id:8,kind:'message'},failure_code:previewState==='routine-failed'?'provider_invalid_response':null,origin:'scheduled'}
let routineCommand: unknown = null
if(previewState==='routine-chat-group'){routineFixture={...routineFixture,room_id:503};routineRows=routineRows.map(row=>({...row,room_id:503}))}
let routineHistory: RoutineRun[] = ['routine-running','routine-failed','routine-normal','routine-approval'].includes(previewState) ? [routineRun] : []
let webhookLost=previewState==='routine-webhook-unknown'
Object.assign(routinesApi, {
 issueWebhook:async(routine:Routine)=>{if(previewState==='routine-webhook-rejected')throw {response:{status:409,data:{detail:{code:'routine_webhook_generation_conflict'}}}};const generation=(routine.webhook?.generation||0)+1;routineFixture={...routineFixture,webhook:{configured:true,generation}};routineRows=[routineFixture];if(webhookLost){webhookLost=false;throw Error('Synthetic lost response')}return {version:1 as const,routine_id:routine.routine_id,revision:routine.revision,generation,key:'gw_'+ 'a'.repeat(43),url_path:`/api/v1/routine-webhooks/${routine.routine_id}`}},
 revokeWebhook:async(routine:Routine)=>{routineFixture={...routineFixture,webhook:{configured:false,generation:(routine.webhook?.generation||0)+1}};routineRows=[routineFixture]},
 webhookEvent:async()=>({run_id:routineRunId,state:'running' as const}),
 list: async (id:number) => id===8 && previewState.startsWith('routine-') ? structuredClone(routineRows) : [],
 read: async (_id:number,id:string) => {const row=routineRows.find(r=>r.routine_id===id);if(!row)throw new Error('missing');return structuredClone(row)},
 create: async (_id:number,body:RoutineInput) => {routineFixture={...routineFixture,...body,revision:1,next_run_at:body.schedule.kind==='webhook'?null:routineFixture.next_run_at,webhook:body.schedule.kind==='webhook'?{configured:false,generation:0}:null,availability:body.enabled?'available':'paused'};routineRows=[routineFixture];routineCommand={version:1,operation:'create',agent_id:8,routine:structuredClone(routineFixture),run:null};if(previewState==='routine-unknown')throw new Error('synthetic response lost');return routineCommand},
 update: async (_id:number,_routine:string,revision:number,body:RoutineInput) => {if(revision!==routineFixture.revision || previewState==='routine-conflict')throw {response:{status:409,data:{detail:{code:'routine_revision_conflict'}}}};routineFixture={...routineFixture,...body,revision:revision+1,next_run_at:body.schedule.kind==='webhook'?null:routineFixture.next_run_at,webhook:body.schedule.kind==='webhook'?{configured:false,generation:routineFixture.webhook?.generation||0}:null,availability:body.enabled?'available':'paused'};routineRows=[routineFixture];return structuredClone(routineFixture)},
 runs: async () => structuredClone(routineHistory),
 test: async (routine:Routine) => { const run:RoutineRun={...routineRun,state:'running',origin:'test',completed_at:null,result_ref:null,failure_code:null}; const root={...events[0],payload:{category:'owner_routine',status:'started',content:routine.instruction,routine:{version:1,run_id:routineRunId,routine_id:routine.routine_id,config_revision:routine.revision,origin:'test',scheduled_at:run.scheduled_at,owner_id:1,agent_id:8}}}; events=[root];routineHistory=[run, ...routineHistory].slice(0,20);routineCommand={version:1,operation:'test',agent_id:8,routine:null,run};if(previewState==='routine-unknown')throw new Error('synthetic response lost');return structuredClone(routineCommand)},
 command: async () => {if(!routineCommand)throw new Error('synthetic missing command');return structuredClone(routineCommand)},
})
if(previewState.startsWith('routine-')) {
 const root:RoomEvent={...event(1,'system',null,'시스템','activity',{category:'owner_routine',status:'started',content:routineFixture.instruction,routine:{version:1,run_id:routineRunId,routine_id:routineId,config_revision:1,origin:'scheduled',scheduled_at:at(10),owner_id:1,agent_id:8}}),event_id:routineRootId}
 const reply:RoomEvent={...event(2,'agent',8,'기획','message',{content:'오늘 할 일을 함께 확인해 볼게요.'}),event_id:routineResultId}
 events=[root,...(routineRun.result_ref?[reply]:[])]
 const proof={version:1 as const,owner_id:1,agent_id:8,room_id:501,routine_id:routineId,run_id:routineRunId,config_revision:1,generation:1,root_event_id:routineRootId,origin:'scheduled' as const,scheduled_at:at(10)}
 const action={...pendingAction,agent_id:8,trigger_event_id:routineRootId,root_ref:routineRun.root_ref,routine_binding:proof,scope_status:'active' as const,revocation_state:null,review_id:'b0000000-0000-4000-8000-000000000005'}
 let actionWaiting=true
 toolsApi.activity=async id => previewState==='routine-approval' && id===501 && actionWaiting ? [action] : []
 if(previewState==='routine-approval') toolsApi.decide=async (_room,id,decision) => {if(id!==action.action_id || !actionWaiting)throw Error('synthetic stale action');actionWaiting=false;return {decision}}
 if(previewState==='routine-approval') toolsApi.preview=async () => ({action_id:action.action_id,family:action.family,target:action.target,scope_target:action.target,scope_description:'Synthetic review',expected_effect:action.expected_effect,workflow_id:null,arguments:{},payload_digest:'e'.repeat(64),review_id:action.review_id,routine_binding:proof,root_ref:action.root_ref,scope_status:'active',revocation_state:null,approval_policy:{scopes:['once'],unverified_context_required:false,start_within_seconds:60},review:{version:1,captured_at:at(10),action:{action_id:action.action_id,room_id:501,agent_id:8,trigger_event_id:routineRootId,family:action.family,target:action.target,scope_target:action.target,expected_effect:action.expected_effect,payload_digest:'e'.repeat(64),execution_binding:null},authority:{owner_id:1,room_id:501,agent_id:8,proposer:{owner_id:1,room_id:501,agent_id:8,connection_id:1,provider_id:'synthetic',model_id:'synthetic'}},targets:[],external_context:{account:'unsupported',target_version:'unsupported'}}})
}


if (previewState.startsWith('routine-receipt-')) {
 const id=(n:number)=>`b1000000-0000-4000-8000-${String(n).padStart(12,'0')}`
 const queued=['routine-receipt-queued','routine-receipt-run-failed','routine-receipt-run-completed','routine-receipt-run-mismatch'].includes(previewState), root:RoomEvent={...event(1,'user',1,'지민','message',{content:queued?'이 루틴을 시험해 줘.':'평일 아침에 계획을 확인하는 루틴을 만들어 줘.'}),event_id:id(1)}
 const meta={version:1,receipt_id:'e'.repeat(64),operation:queued?'test':'create',state:queued?'queued':'saved',agent_id:8,root_request_event_id:root.event_id,routine_id:routineId,revision:1,routine:structuredClone(routineFixture)}
 const result:RoomEvent={...event(2,'system',null,'시스템','activity',{category:'routine_persistence',status:meta.state,trigger_event_id:root.event_id,routine_persistence:meta}),event_id:id(2)}
 events=[root,result];const ref=(row:RoomEvent)=>({room_id:row.room_id,event_id:row.event_id,seq:row.seq,actor_type:row.actor_type,actor_id:row.actor_id,kind:row.kind})
 routinesApi.persistence=async()=>{if(previewState==='routine-receipt-unknown')throw Error('synthetic unavailable');return {...structuredClone(meta),...(previewState==='routine-receipt-mismatch'?{revision:2}:{}),root_ref:ref(root),result_ref:ref(result),replayed:true,...(queued?{test:{state:'pending',run:null,failure_code:null}}:{})}}
 if(previewState.startsWith('routine-receipt-run-')) {
  const runId=id(5),scheduled=at(10),started:RoomEvent={...event(3,'system',null,'시스템','activity',{category:'owner_routine',status:'started',content:routineFixture.instruction,routine:{version:1,run_id:runId,routine_id:routineId,config_revision:1,owner_id:1,agent_id:8,origin:'test',scheduled_at:scheduled}}),event_id:id(3)}
  const failed=previewState!=='routine-receipt-run-completed',terminal:RoomEvent={...event(4,'agent',8,'기획',failed?'failure':'message',failed?{code:'routine_failed',message:'Routine execution failed.',trigger_event_id:started.event_id,generation:1,round:1,routine:{version:1,run_id:previewState==='routine-receipt-run-mismatch'?id(99):runId}}:{content:'시험 답변입니다.',trigger_event_id:started.event_id,routine:{version:1,run_id:runId}}),event_id:id(4)}
  const run:RoutineRun={run_id:runId,state:failed?'failed':'completed',scheduled_at:scheduled,started_at:scheduled,completed_at:at(11),root_ref:ref(started),result_ref:ref(terminal),failure_code:failed?'unsupported_option':null,origin:'test'}
  let reads=0;events=[root,result,started,terminal];routinesApi.runs=async()=>[structuredClone(run)]
  routinesApi.persistence=async()=>({...structuredClone(meta),root_ref:ref(root),result_ref:ref(result),replayed:true,test:++reads===1?{state:'pending',run:null,failure_code:null}:{state:'started',run:structuredClone(run),failure_code:null}})
 }
}

if (previewState === 'primary-empty') agentsApi.list = async () => []
if (crossRoom) {
  conversationsApi.listMembers = async (id: number) => [{ room_id: id, agent_id: id === 701 ? 9 : 8, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' }]
  toolsApi.activity = async () => []
}
if (previewState.startsWith('teammates-')) {
  conversationsApi.listMembers = async (id: number) => agents.slice(0, previewState === 'teammates-alone' ? 1 : 2).map(agent => ({
    room_id: id, agent_id: agent.id, is_active: true, joined_at: at(0), paused: false,
    run_state: teammateState === 'running' && previewState !== 'teammates-alone' && agent.id === 9 ? 'running' : 'idle',
  }))
  toolsApi.activity = async () => []
}
if (['duplicate-failure', 'empty'].includes(previewState) && previewScreen === 'chat') {
  conversationsApi.listMembers = async (id: number) => agents.slice(0, previewState === 'empty' ? 0 : 2).map(agent => ({
    room_id: id, agent_id: agent.id, is_active: true, joined_at: at(0), paused: false, run_state: 'idle',
  }))
}
if (rootApproval) {
  const scopedId = (value: number) => `af034000-0000-4000-8000-${String(value).padStart(12, '0')}`
  const imageCapture = previewState === 'root-approval-image-desktop' || previewState === 'root-approval-image-browser'
  const processCase = previewState === 'root-approval-process' || previewState === 'root-approval-missing-proof'
  const twoRoots = previewState === 'root-approval-two-roots', external = previewState === 'root-approval-external' || previewState === 'root-approval-stale'
  let stoppedA = false, staleReview = false
  const rootB = { ...event(1, 'user', 1, '지민', 'message', { content: '두 번째 요청도 검토에게 부탁해 주세요.' }), room_id: 502, event_id: scopedId(50) }
  const binding = (agent: number, room: number, second = false, nested = false): ExecutionBinding => ({
    version: 1, root_scope_id: scopedId(second ? 91 : 81), scope_id: scopedId(second ? 93 : nested ? 84 : 83), owner_id: 1,
    root_room_id: second ? 502 : 501, root_request_event_id: second ? rootB.event_id : crossRoot, execution_room_id: room, agent_id: agent,
    run_generation: processCase ? 1 : second ? 5 : 3, job_id: second ? scopedId(60) : nested ? scopedId(61) : crossId, phase: 'response', context_mode: 'delegated', deadline_at: processCase ? '2099-10-29T01:30:00.143527+00:00' : '2099-10-29T01:30:00Z',
    lineage: (second ? [91, 92, 93] : nested ? [81, 82, 83, 84] : [81, 82, 83]).map(id => ({ scope_id: scopedId(id), epoch: 0 })),
  })
  const action = (agent: number, room: number, number: number, second = false, nested = false): ToolActivity => ({
    action_id: scopedId(number), room_id: room, agent_id: agent, trigger_event_id: second ? rootB.event_id : crossRoot,
    family: processCase ? 'start_process' : external && !nested ? 'keyboard_text' : second ? 'read_file' : 'write_file',
    target: processCase ? '/bin/sleep [invocation sha256:1936d18df9226bce]' : external && !nested ? 'desktop:keyboard [text sha256:abcdef1234567890]' : second ? '/synthetic/second-request.txt' : nested ? '/synthetic/research-note.md' : '/synthetic/release-note.md',
    expected_effect: processCase ? 'Run process with local user authority; 1 arguments; owned background lease up to 180 seconds after start' : external && !nested ? 'Type 5 characters' : second ? 'Read file' : 'Write 120 bytes', state: 'waiting', scope: null, grant_id: null,
    requested_at: at(18), started_at: null, completed_at: null, review_id: scopedId(number + 100), execution_binding: binding(agent, room, second, nested),
    root_ref: crossRef(second ? rootB : crossRootEvent), scope_status: 'active', revocation_state: null,
  })
  const a = action(9, 701, 1), c = action(12, 702, 2, false, true), b = action(9, 701, 3, true)
  if (imageCapture) { a.family = previewState === 'root-approval-image-desktop' ? 'screenshot' : 'browser_screenshot'; a.target = a.family === 'screenshot' ? 'desktop:screen' : 'https://example.invalid/preview'; a.expected_effect = a.family === 'screenshot' ? 'Capture screenshot' : 'Capture browser page' }
  if (twoRoots) { a.state = 'completed'; a.started_at = at(19); a.completed_at = at(20) }
  const scopedRows = twoRoots ? [a, c, b] : [a]
  const snapshot = (row: ToolActivity): ActionPreview => {
    const proof = structuredClone(row.execution_binding!), opaque = row.family === 'keyboard_text', process = row.family === 'start_process'
    const scopeTarget = process ? '/bin/sleep' : row.target
    return { action_id: row.action_id, family: row.family, target: row.target, expected_effect: row.expected_effect,
      scope_target: scopeTarget, scope_description: 'One reviewed action by this execution Bot for the original request.', workflow_id: null,
      arguments: imageCapture ? (row.family === 'browser_screenshot' ? { url: row.target } : {}) : process ? { executable: '/bin/sleep', argv: ['173'], cwd: '/synthetic/consent-review', background_seconds: 180 } : opaque ? { text: 'Hello' } : row.family === 'read_file' ? { path: row.target } : { path: row.target, content: 'Synthetic reviewed content only.' },
      payload_digest: 'e'.repeat(64), review_id: row.review_id!, execution_binding: proof, root_ref: row.root_ref, scope_status: row.scope_status, revocation_state: row.revocation_state,
      approval_policy: { scopes: ['once'], unverified_context_required: opaque, start_within_seconds: opaque ? 60 : null },
      review: { version: 2, captured_at: at(22), action: { action_id: row.action_id, room_id: row.room_id, agent_id: row.agent_id,
        trigger_event_id: row.trigger_event_id, family: row.family, target: row.target, scope_target: scopeTarget, expected_effect: row.expected_effect, payload_digest: 'e'.repeat(64), execution_binding: proof },
        authority: { owner_id: 1, room_id: row.room_id, agent_id: row.agent_id, os_user_status: process ? 'verified' : 'unsupported', ...(process ? { euid: 502 } : {}),
          proposer: { owner_id: 1, room_id: row.room_id, agent_id: row.agent_id, connection_id: 1, provider_id: process || imageCapture ? 'opencode-go' : 'synthetic', model_id: process || imageCapture ? 'qwen3.7-plus' : 'synthetic-preview', config_revision: 1, auth_grant_revision: 1 } },
        targets: imageCapture ? [{ role: row.family === 'screenshot' ? 'desktop_channel' : 'browser_page', status: 'unsupported' }] : process ? [{ role: 'executable', path: '/bin/sleep', status: 'verified' }, { role: 'cwd', path: '/synthetic/consent-review', status: 'verified' }] : [{ role: opaque ? 'desktop_channel' : 'path', status: opaque ? 'unsupported' : 'verified', ...(opaque ? {} : { path: row.target }) }],
        external_context: { account: opaque ? 'unsupported' : 'not_applicable', target_version: opaque ? 'unsupported' : 'not_applicable' } },
    }
  }
  const messageCId = scopedId(61), messageBId = scopedId(60)
  const cMeta = { version: 1, message_id: messageCId, phase: 'request', sender_agent_id: 9, recipient_agent_id: 12, root_room_id: 501, root_request_event_id: crossRoot }
  const requestC = { ...event(2, 'agent', 9, '검토', 'message', { content: '조사에게 설치 지침의 근거 확인을 부탁합니다.', trigger_event_id: crossRoot, bot_message: cMeta }), room_id: 701, event_id: scopedId(70) }
  const deliveryC = { ...event(1, 'system', null, '시스템', 'activity', { category: 'bot_message', trigger_event_id: crossRoot, bot_message: { ...cMeta, phase: 'delivery' } }), room_id: 702, event_id: scopedId(71) }
  const bMeta = { version: 1, message_id: messageBId, sender_agent_id: 10, recipient_agent_id: 9, root_room_id: 502, root_request_event_id: rootB.event_id }
  const requestB = { ...event(2, 'agent', 10, '테스트', 'message', { content: '별도 요청의 문서를 확인해 주세요.', trigger_event_id: rootB.event_id, bot_message: { ...bMeta, phase: 'request' } }), room_id: 502, event_id: scopedId(72) }
  const deliveryB = { ...event(4, 'system', null, '시스템', 'activity', { category: 'bot_message', trigger_event_id: rootB.event_id, bot_message: { ...bMeta, phase: 'delivery' } }), room_id: 701, event_id: scopedId(73) }
  const answerA = { ...crossEvent('response', 701, 3), payload: { content: '초안은 정리했습니다. 조사의 근거 확인을 기다립니다.', trigger_event_id: crossRoot, bot_message: { version: 1, message_id: crossId, phase: 'response', sender_agent_id: 8, recipient_agent_id: 9, root_room_id: 501, root_request_event_id: crossRoot } } }
  const returnDeliveryA = crossEvent('return_delivery', 501, 3)
  if (twoRoots) { crossSourceEvents.push(returnDeliveryA); crossTargetEvents.push(requestC, answerA, deliveryB) }
  const messageC: BotMessage = { ...crossMessage, message_id: messageCId, parent_job_id: crossId, parent_kind: 'bot_message', depth: 2,
    sender_agent_id: 9, sender_name: '검토', recipient_agent_id: 12, recipient_name: '조사', content: String(requestC.payload.content),
    source_room: { room_id: 701, title: '검토 · 개인 대화' }, target_room: { room_id: 702, title: '조사 · 개인 대화' }, request_ref: crossRef(requestC), delivery_ref: crossRef(deliveryC), result_ref: null, return_delivery_ref: null, return_ref: null }
  const messageB: BotMessage = { ...crossMessage, message_id: messageBId, root_request_event_id: rootB.event_id,
    sender_agent_id: 10, sender_name: '테스트', content: String(requestB.payload.content), source_room: { room_id: 502, title: '테스트 · 개인 대화' },
    root_ref: crossRef(rootB), request_ref: crossRef(requestB), delivery_ref: crossRef(deliveryB), result_ref: null, return_delivery_ref: null, return_ref: null }
  const rootAMessage = (): BotMessage => ({ ...crossMessage, ...(twoRoots ? { state: 'completed' as const, reply_state: stoppedA ? 'cancelled' as const : 'pending' as const,
    result_ref: crossRef(answerA), return_delivery_ref: crossRef(returnDeliveryA) } : stoppedA ? { state: 'cancelled' as const, reply_state: 'cancelled' as const } : {}) })
  attentionApi.list = async () => ({ items: scopedRows.filter(row => row.state === 'waiting').map(row => ({
    id: `approval:${row.action_id}`, kind: 'approval' as const, occurred_at: row.requested_at, room_id: row.room_id,
    room_title: previewRooms.get(row.room_id)?.title || `대화 #${row.room_id}`, project_id: 40, project_name: '데스크톱 협업',
    agent_id: row.agent_id, agent_name: agents.find(agent => agent.id === row.agent_id)?.name || `Agent #${row.agent_id}`,
    request_event_id: null, request_seq: null, action_id: row.action_id, failure_event_id: null, failure_seq: null,
    summary_code: row.family === 'keyboard_text' ? 'approval_input' as const : 'approval_files' as const, href: `/chat/${row.room_id}?action=${row.action_id}`,
  })), next_cursor: null })
  toolsApi.rootActions = async (messageId, rootRef) => ({ version: 1, message_id: messageId, root_ref: rootRef,
    actions: scopedRows.filter(row => row.root_ref?.event_id === rootRef.event_id).map(row => structuredClone(row)) })
  toolsApi.activity = async id => scopedRows.filter(row => row.room_id === id).map(row => {
    const result = structuredClone(row)
    if (previewState === 'root-approval-missing-proof') { delete result.execution_binding; delete result.root_ref; delete result.scope_status; delete result.revocation_state }
    return result
  })
  toolsApi.preview = async (id, actionId) => {
    const row = scopedRows.find(row => row.room_id === id && row.action_id === actionId)
    if (!row || staleReview) throw Object.assign(new Error('Synthetic retained review stale'), { response: { status: 409 } })
    return snapshot(row)
  }
  toolsApi.decide = async (id, actionId, decision, scope, reviewId, acknowledge, sendImageToModel = false) => {
    const row = scopedRows.find(row => row.room_id === id && row.action_id === actionId)
    if (!row || row.scope_status !== 'active' || reviewId && reviewId !== row.review_id || decision === 'approve' && (scope !== 'once' || external && !acknowledge)) throw Object.assign(new Error('Synthetic decision rejected'), { response: { status: 409 } })
    if (previewState === 'root-approval-stale' && decision === 'approve') {
      staleReview = true; row.execution_binding = { ...row.execution_binding!, run_generation: 4, lineage: row.execution_binding!.lineage.map(item => ({ ...item, epoch: item.epoch + 1 })) }; row.review_id = scopedId(201)
      throw Object.assign(new Error('Synthetic root epoch changed'), { response: { status: 409 } })
    }
    if (sendImageToModel && (!imageCapture || decision !== 'approve')) throw new Error('Synthetic image consent rejected')
    row.send_image_to_model = decision === 'approve' && imageCapture ? sendImageToModel : false
    row.state = decision === 'approve' ? 'approved' : 'denied'; return { action_id: actionId, state: row.state, send_image_to_model: row.send_image_to_model }
  }
  let stopReceipt: ScopeStopReceipt | null = null
  conversationsApi.cancel = async (id, capture) => {
    if (id === 501) {
      stoppedA = true
      const targets = scopedRows.filter(item => item.root_ref?.room_id === 501)
      for (const row of targets) { if (row.state !== 'completed') row.state = 'cancelled'; row.scope_status = 'revoked'; row.revocation_state = 'unknown' }
      stopReceipt = { version: 1, stop_id: scopedId(300), room_id: 501, created_at: at(25), state: 'unknown',
        revocations: [{ scope_id: scopedId(81), epoch: 1 }], receipts: [{ scope_id: scopedId(81), epoch: 1, state: 'unknown' }] }
      capture?.(stopReceipt.stop_id)
    }
    // HTTP cancellation was accepted; owned-resource acknowledgment remains unknown.
  }
  scopeStopsApi.latest = async id => stopReceipt?.room_id === id ? structuredClone(stopReceipt) : null
  scopeStopsApi.read = async id => { if (!stopReceipt || stopReceipt.stop_id !== id) throw new Error('Synthetic stop receipt missing'); return structuredClone(stopReceipt) }
  scopeStopsApi.confirm = async frozen => {
    if (!stopReceipt || frozen.stop_id !== stopReceipt.stop_id || JSON.stringify(frozen.revocations) !== JSON.stringify(stopReceipt.revocations)) throw new Error('Synthetic frozen stop changed')
    stopReceipt = { ...stopReceipt, state: 'settled', receipts: stopReceipt.revocations.map(item => ({ ...item, state: 'settled' })) }
    for (const row of scopedRows.filter(item => item.root_ref?.room_id === 501)) row.revocation_state = 'settled'
    return structuredClone(stopReceipt)
  }
  botMessagesApi.list = async id => ({ version: 1, room_id: id, capabilities: { text_collaboration: true, delegated_os_execution: true },
    bots: agents.map(agent => ({ agent_id: agent.id, name: agent.name, description: agent.description, model_id: agent.model_id, avatar: agent.avatar })),
    messages: [rootAMessage(), ...(twoRoots ? [{ ...messageC, ...(stoppedA ? { state: 'cancelled' as const, reply_state: 'cancelled' as const } : {}) }, messageB] : [])]
      .filter(message => [message.root_ref.room_id, message.source_room.room_id, message.target_room.room_id].includes(id)), messages_truncated: false })
  const originalEventsList = eventsApi.list
  eventsApi.list = async (id, after, limit) => (twoRoots && id === 502 ? [rootB, requestB] : twoRoots && id === 702 ? [deliveryC] : await originalEventsList(id, after, limit)).filter(row => row.seq > after)
  if (twoRoots) {
    previewRooms.set(502, { id: 502, user_id: 1, project_id: 40, title: '테스트 · 개인 대화', is_active: true, created_at: at(0), updated_at: at(21) })
    previewRooms.set(702, { id: 702, user_id: 1, project_id: 40, title: '조사 · 개인 대화', is_active: true, created_at: at(0), updated_at: at(21) })
    personalRooms.set(10, 502); personalRoomAgents.set(502, 10); personalRooms.set(12, 702); personalRoomAgents.set(702, 12)
  }
  conversationsApi.listMembers = async id => [{ room_id: id, agent_id: id === 701 ? 9 : id === 702 ? 12 : id === 502 ? 10 : 8,
    is_active: true, joined_at: at(0), paused: false, run_state: id === 701 || id === 702 ? 'waiting' : 'idle' }]
}
if (parallelRoot) {
  const parallelId = (n: number) => `af035000-0000-4000-8000-${String(n).padStart(12, '0')}`
  const parallelBatchId = parallelId(1000), parallelRootId = parallelId(1)
  const people = [{ id: 9, name: '검토', room: 701, task: '설치 안내의 누락된 내용을 확인하고 보완할 부분을 알려 주세요.' },
    { id: 12, name: '조사', room: 702, task: '출시 문서의 근거를 확인하고 주의할 점을 정리해 주세요.' },
    { id: 10, name: '테스트', room: 703, task: '설치 순서를 검토하고 실행 전에 확인할 항목을 알려 주세요.' }]
  const baseStates: BotBatchMember['state'][] = previewState === 'parallel-pending' ? ['pending','pending','pending']
    : previewState === 'parallel-working' ? ['running','running','pending'] : ['parallel-first-answer','parallel-incoming-waiting'].includes(previewState) ? ['completed','running','running']
      : previewState === 'parallel-partial' ? ['completed','failed','completed'] : previewState === 'parallel-branch-stop' ? ['cancelled','completed','completed']
        : previewState === 'parallel-root-stop' ? ['completed','cancelled','cancelled'] : ['completed','completed','completed']
  const sourceRoot = { ...event(1, 'user', 1, '지민', 'message', { content: '@기획 출시 안내를 검토·조사·테스트에게 나누어 확인하고 답변을 모아 정리해 줘.' }), event_id: parallelRootId }
  const meta = (phase: string, member_index?: number) => ({ version: 1, batch_id: parallelBatchId, phase, ...(member_index === undefined ? {} : { member_index }), root_room_id: 501, root_request_event_id: parallelRootId })
  const memberMeta = (phase: 'request'|'delivery'|'response', index: number) => ({ version: 1, message_id: parallelId(200 + index), phase,
    sender_agent_id: 8, recipient_agent_id: people[index].id, root_room_id: 501, root_request_event_id: parallelRootId })
  const groupRequest = { ...event(2, 'agent', 8, '기획', 'message', { content: '세 동료에게 필요한 확인을 나누어 부탁했습니다.', trigger_event_id: parallelRootId, bot_batch: meta('request') }), event_id: parallelId(2) }
  const requests = people.map((person, index) => ({ ...event(3 + index, 'agent', 8, '기획', 'message', { content: person.task, trigger_event_id: parallelRootId, bot_message: memberMeta('request', index), bot_batch: meta('branch_request', index) }), event_id: parallelId(10 + index) }))
  const deliveries = people.map((person, index) => ({ ...event(1, 'system', null, '시스템', 'activity', { category: 'bot_message', content: person.task, trigger_event_id: parallelRootId, bot_message: memberMeta('delivery', index), bot_batch: meta('branch_delivery', index) }), room_id: person.room, event_id: parallelId(20 + index) }))
  const answers = people.map((person, index) => ['completed','failed'].includes(baseStates[index]) ? ({
    ...event(2, 'agent', person.id, person.name, baseStates[index] === 'failed' ? 'failure' : 'message', {
      ...(baseStates[index] === 'failed' ? { code: 'provider_unavailable', message: '조사 작업의 모델 응답을 받지 못했습니다.' }
        : { content: index === 0 ? '설치 전 백업 위치와 직전 버전으로 돌아가는 방법을 추가해 주세요.' : index === 1 ? '근거 문서와 배포 시점을 구분해서 안내하면 좋겠습니다.' : '설치 순서는 명확합니다. 실행 전 연결 상태와 저장 공간을 확인해 주세요.' }),
      trigger_event_id: parallelRootId, bot_message: memberMeta('response', index), bot_batch: meta('branch_response', index),
    }), room_id: person.room, event_id: parallelId(30 + index),
  }) : null)
  const joined = baseStates.every(state => state !== 'pending' && state !== 'running')
  const originalStopped = previewState === 'parallel-root-stop', alreadyReturned = ['parallel-completed','parallel-incoming','parallel-incoming-unavailable'].includes(previewState)
  let rootStopped = originalStopped
  const branchStops = new Set<number>(baseStates.flatMap((state, index) => state === 'cancelled' ? [index] : []))
  const back = joined && !originalStopped ? { ...event(6, 'system', null, '시스템', 'activity', { category: 'bot_batch', trigger_event_id: parallelRootId, bot_batch: meta('return_delivery') }), event_id: parallelId(60) } : null
  const final = alreadyReturned ? { ...event(7, 'agent', 8, '기획', 'message', { content: '세 동료의 확인을 모아 출시 안내를 정리했습니다. 설치 전 백업과 복구 방법을 안내하고, 문서 근거와 실행 전 확인 항목을 함께 담았습니다.', trigger_event_id: parallelRootId, bot_batch: meta('return') }), event_id: parallelId(70) } : null
  const sourceHistory = [sourceRoot, groupRequest, ...requests, ...(back ? [back] : []), ...(final ? [final] : [])]
  const recipientHistory = new Map(people.map((person, index) => [person.room, [deliveries[index], ...(answers[index] ? [answers[index]!] : [])]]))
  const snapshot = (): BotBatch => {
    const members: BotBatchMember[] = people.map((person, index) => {
      const prior = baseStates[index], state = rootStopped && !['completed','failed'].includes(prior) || branchStops.has(index) && !['completed','failed'].includes(prior) ? 'cancelled' : prior
      return { index, message_id: parallelId(200 + index), recipient_agent_id: person.id, recipient_name: person.name,
        target_room: { room_id: person.room, title: `${person.name} · 개인 대화` }, content: person.task, state,
        failure_code: state === 'failed' ? 'provider_unavailable' : state === 'cancelled' ? 'cancelled' : null,
        request_ref: crossRef(requests[index]), delivery_ref: crossRef(deliveries[index]), result_ref: answers[index] ? crossRef(answers[index]!) : null,
        resolved_result_ref: answers[index] ? crossRef(answers[index]!) : null }
    })
    const allTerminal = members.every(member => !['pending','running'].includes(member.state)), issue = members.some(member => member.state !== 'completed')
    return { version: 1, batch_id: parallelBatchId, root_ref: crossRef(sourceRoot), source_room: { room_id: 501, title: '기획 · 개인 대화' }, sender_agent_id: 8, sender_name: '기획', parent_job_id: null, parent_kind: null,
      state: rootStopped && !alreadyReturned ? 'cancelled' : allTerminal ? issue ? 'partial' : 'completed' : previewState === 'parallel-pending' ? 'pending' : 'running',
      reply_state: alreadyReturned ? 'delivered' : rootStopped ? 'cancelled' : allTerminal ? 'pending' : 'none', failure_code: rootStopped && !alreadyReturned ? 'cancelled' : issue && allTerminal ? 'partial_members' : null,
      request_ref: crossRef(groupRequest), return_delivery_ref: back ? crossRef(back) : null, return_ref: final ? crossRef(final) : null,
      source_scope_id: parallelId(201), created_at: at(15), updated_at: at(22), deadline_at: '2099-10-29T01:30:00Z', members }
  }
  events = sourceHistory
  const previousList = eventsApi.list
  eventsApi.list = async (id, after, limit = 100) => (id === 501 ? sourceHistory : recipientHistory.get(id) || await previousList(id, after, limit)).filter(row => row.seq > after).slice(0, limit)
  botBatchesApi.list = async id => { if (previewState === 'parallel-incoming-unavailable') throw new Error('Synthetic incoming batch status unavailable'); return { version: 1, room_id: id, batches: [501, ...people.map(person => person.room)].includes(id) ? [snapshot()] : [], batches_truncated: false } }
  botMessagesApi.list = async id => ({ version: 1, room_id: id, capabilities: { text_collaboration: true, delegated_os_execution: true }, bots: agents.map(agent => ({ agent_id: agent.id, name: agent.name, description: agent.description, model_id: agent.model_id, avatar: agent.avatar })), messages: [], messages_truncated: false })
  for (const person of people) {
    previewRooms.set(person.room, { id: person.room, user_id: 1, project_id: 40, title: `${person.name} · 개인 대화`, is_active: true, created_at: at(0), updated_at: at(21) })
    personalRooms.set(person.id, person.room); personalRoomAgents.set(person.room, person.id)
  }
  conversationsApi.listMembers = async id => {
    const member = snapshot().members.find(person => person.target_room.room_id === id)
    return [{ room_id: id, agent_id: member?.recipient_agent_id || 8, is_active: true, joined_at: at(0), paused: false,
      run_state: member?.state === 'running' ? 'running' : member?.state === 'cancelled' ? 'stopped' : 'idle',
      ...(previewState.startsWith('parallel-incoming') && id === 701 ? { work_state: { version: 1 as const, room_id: id, agent_id: 9,
        scope: 'none' as const, phase: 'idle' as const, peer_agent_ids: [] } } : {}) }]
  }
  toolsApi.activity = async () => []
  const stopReceipts = new Map<number, ScopeStopReceipt>()
  const receiptFor = (room: number, index?: number): ScopeStopReceipt => {
    const refs = index === undefined ? [200,201,202,203,204] : [202 + index]
    return { version: 1, stop_id: parallelId(900 + room), room_id: room, created_at: at(24), state: 'unknown',
      revocations: refs.map(n => ({ scope_id: parallelId(n), epoch: 1 })), receipts: refs.map(n => ({ scope_id: parallelId(n), epoch: 1, state: 'unknown' })) }
  }
  if (originalStopped) stopReceipts.set(501, receiptFor(501))
  else if (previewState === 'parallel-branch-stop') stopReceipts.set(701, receiptFor(701, 0))
  conversationsApi.cancel = async (room, capture) => {
    if (room === 501) rootStopped = true
    else { const index = people.findIndex(person => person.room === room); if (index >= 0) branchStops.add(index) }
    const index = people.findIndex(person => person.room === room), receipt = receiptFor(room, index < 0 ? undefined : index)
    stopReceipts.set(room, receipt); capture?.(receipt.stop_id)
  }
  scopeStopsApi.latest = async room => structuredClone(stopReceipts.get(room) || null)
  scopeStopsApi.read = async (id, room) => { const receipt = stopReceipts.get(room); if (!receipt || receipt.stop_id !== id) throw new Error('Synthetic stop receipt absent'); return structuredClone(receipt) }
  scopeStopsApi.confirm = async frozen => { const current = stopReceipts.get(frozen.room_id); if (!current || current.stop_id !== frozen.stop_id || JSON.stringify(current.revocations) !== JSON.stringify(frozen.revocations)) throw new Error('Synthetic frozen stop changed'); const settled: ScopeStopReceipt = { ...current, state: 'settled', receipts: current.revocations.map(ref => ({ ...ref, state: 'settled' })) }; stopReceipts.set(frozen.room_id, settled); return structuredClone(settled) }
}
if (createRoot) {
  const creationId = (n: number) => `af036000-0000-4000-8000-${String(n).padStart(12, '0')}`
  const opId = creationId(100), rootId = creationId(1), collaborating = previewState === 'bot-create-collaborating'
  const failed = previewState === 'bot-create-failed' || previewState === 'bot-create-source-changed'
  const definition = { name: '통계', description: '숫자의 근거와 비교 조건을 검토하는 Bot', role: '제공된 수치의 출처와 비교 기준을 확인하고, 계산 근거와 불확실성을 구분해 한국어로 설명합니다.' }
  const root = { ...event(1, 'user', 1, '지민', 'message', { content: '@기획 숫자를 검토하는 통계 Bot을 만들고, 출시 지표를 함께 검토해 줘.' }), event_id: rootId }
  const metadata = (phase: 'request'|'created') => ({ version: 1, creation_id: opId, phase, root_room_id: 501, root_request_event_id: rootId, source_agent_id: 8 })
  const request = { ...event(2, 'agent', 8, '기획', 'activity', { category: 'bot_creation', status: 'requested', trigger_event_id: rootId,
    content: definition, bot_creation: metadata('request') }), event_id: creationId(2) }
  const result = { ...event(3, 'system', null, '시스템', 'activity', { category: 'bot_creation', status: 'created', trigger_event_id: rootId,
    created_agent_id: 13, created_room_id: 713, bot_creation: metadata('created') }), event_id: creationId(3) }
  const createdAgent: Agent = { ...agents[0], id: 13, name: definition.name, description: definition.description, system_prompt: definition.role,
    avatar: avatarFromSeed(13), created_at: at(16), updated_at: null }
  const receipt: BotCreationReceipt = { version: 1, creation_id: opId, state: 'created', root_ref: crossRef(root), source_agent_id: 8, source_room_id: 501,
    created_agent: { id: createdAgent.id, name: createdAgent.name, description: createdAgent.description, system_prompt: createdAgent.system_prompt,
      connection_id: 1, model_id: 'qwen3.7-plus', temperature: 'auto', max_tokens: 0, avatar: createdAgent.avatar! },
    conversation: { room_id: 713, project_id: 40, href: '/chat/713' }, request_ref: crossRef(request), result_ref: crossRef(result),
    model_binding: { connection_id: 1, model_id: 'qwen3.7-plus', config_revision: 1, auth_grant_revision: 1 },
    readback: { agent_matches: previewState !== 'bot-create-unavailable', conversation_matches: true, available: previewState !== 'bot-create-unavailable' }, created_at: at(16), replayed: false }
  const failure = { ...event(2, 'agent', 8, '기획', 'failure', { trigger_event_id: rootId, generation: 1, round: 1,
    code: previewState === 'bot-create-source-changed' ? 'bot_create_source_changed' : 'bot_create_invalid_request',
    message: previewState === 'bot-create-source-changed' ? 'The original Bot model binding changed. No Bot was created.' : 'The Bot definition could not be saved.' }), event_id: creationId(80) }
  const replayNote = { ...event(4, 'agent', 8, '기획', 'message', { content: '앞서 만든 통계 Bot을 그대로 사용하겠습니다.', trigger_event_id: rootId, generation: 1, round: 1 }), event_id: creationId(90) }
  const messageId = creationId(200), botMeta = (phase: string) => ({ version: 1, message_id: messageId, phase, sender_agent_id: 8, recipient_agent_id: 13, root_room_id: 501, root_request_event_id: rootId })
  const task = '지표의 비교 기간과 표본 수가 결론을 뒷받침하는지 검토해 줘.'
  const handoff = { ...event(4, 'agent', 8, '기획', 'message', { content: task, trigger_event_id: rootId, bot_message: botMeta('request') }), event_id: creationId(40) }
  const delivery = { ...event(1, 'system', null, '시스템', 'activity', { category: 'bot_message', trigger_event_id: rootId, bot_message: botMeta('delivery') }), room_id: 713, event_id: creationId(41) }
  const answer = { ...event(2, 'agent', 13, '통계', 'message', { content: '비교 기간을 맞추고 표본 수를 함께 제시해야 합니다. 현재 정보만으로 증가 원인을 단정하기는 어렵습니다.', trigger_event_id: rootId, generation: 1, round: 1, bot_message: botMeta('response') }), room_id: 713, event_id: creationId(42) }
  const back = { ...event(5, 'system', null, '시스템', 'activity', { category: 'bot_message', trigger_event_id: rootId, bot_message: botMeta('return_delivery') }), event_id: creationId(43) }
  const summary = { ...event(6, 'agent', 8, '기획', 'message', { content: '통계 Bot의 검토를 반영했습니다. 지표는 같은 기간과 표본 수를 기준으로 비교하고, 원인은 추가 근거를 확인한 뒤 설명하겠습니다.', trigger_event_id: rootId, generation: 2, round: 1, bot_message: botMeta('return') }), event_id: creationId(44) }
  const sourceEvents = failed ? [root, failure] : [root, request, result, ...(collaborating ? [handoff, back, summary] : previewState === 'bot-create-replayed' ? [replayNote] : [])]
  const targetEvents = collaborating ? [delivery, answer] : []
  events = sourceEvents
  const previousEvents = eventsApi.list
  eventsApi.list = async (id, after, limit = 100) => (id === 501 ? sourceEvents : id === 713 ? targetEvents : await previousEvents(id, after, limit)).filter(row => row.seq > after).slice(0, limit)
  const readReceipt = () => structuredClone(receipt)
  botCreationsApi.list = async id => {
    if (id === 501 && previewState === 'bot-create-checking') return new Promise<import('../src/api/botCreations').RoomBotCreations>(() => {})
    if (id === 501 && previewState === 'bot-create-unknown') throw new Error('Synthetic creation read response lost')
    return { version: 1, room_id: id, creations: id === 501 && !failed ? [readReceipt()] : [], creations_truncated: false }
  }
  botCreationsApi.read = async (id, room, original) => { if (failed || id !== opId || room !== 501 || original !== rootId) throw new Error('Synthetic creation not found'); return readReceipt() }
  botMessagesApi.list = async id => ({ version: 1, room_id: id, capabilities: { text_collaboration: true, delegated_os_execution: true },
    bots: agents.map(agent => ({ agent_id: agent.id, name: agent.name, description: agent.description, model_id: agent.model_id, avatar: agent.avatar })),
    messages: collaborating && [501,713].includes(id) ? [{ version: 1, capabilities: { text_collaboration: true, delegated_os_execution: true },
      message_id: messageId, root_request_event_id: rootId, parent_job_id: null, parent_kind: null, depth: 1,
      sender_agent_id: 8, sender_name: '기획', recipient_agent_id: 13, recipient_name: '통계', content: task, state: 'completed', reply_state: 'delivered',
      source_room: { room_id: 501, title: '기획 · 개인 대화' }, target_room: { room_id: 713, title: '통계 · 개인 대화' },
      root_ref: crossRef(root), request_ref: crossRef(handoff), delivery_ref: crossRef(delivery), result_ref: crossRef(answer), return_delivery_ref: crossRef(back), return_ref: crossRef(summary),
      failure_code: null, deadline_at: '2099-10-29T01:30:00Z', created_at: at(17), updated_at: at(20) }] : [], messages_truncated: false })
  if (!failed) {
    agents.push(createdAgent)
    previewRooms.set(713, { id: 713, user_id: 1, project_id: 40, title: '통계 · 개인 대화', is_active: true, created_at: at(16), updated_at: at(20) })
    personalRooms.set(13, 713); personalRoomAgents.set(713, 13)
  }
  const earlierMembers = conversationsApi.listMembers
  conversationsApi.listMembers = async id => {
    if (id !== 501 && id !== 713) return earlierMembers(id)
    const agentId = id === 713 ? 13 : 8
    // The recipient has no invented local Owner request. Its delegated result
    // remains in canonical history; the room-local work projection is idle.
    const work: WorkState = id === 713
      ? { version: 1, room_id: id, agent_id: agentId, scope: 'none', phase: 'idle', peer_agent_ids: [] }
      : { version: 1, room_id: id, agent_id: agentId, scope: failed || collaborating || previewState === 'bot-create-replayed' ? 'previous' : 'current',
        phase: failed ? 'failed' : collaborating || previewState === 'bot-create-replayed' ? 'completed' : 'thinking',
        trigger_event_id: rootId, generation: collaborating ? 2 : 1, round: 1,
        request: { event_id: rootId, seq: 1 }, updated_at: at(20), peer_agent_ids: [],
        result: failed ? { event_id: failure.event_id, seq: failure.seq } : collaborating ? { event_id: summary.event_id, seq: summary.seq }
          : previewState === 'bot-create-replayed' ? { event_id: replayNote.event_id, seq: replayNote.seq } : null,
        event: failed ? { event_id: failure.event_id, seq: failure.seq } : null }
    return [{ room_id: id, agent_id: agentId, is_active: true, joined_at: at(0), paused: false,
      run_state: work.phase === 'thinking' ? 'running' : 'idle', work_state: work }]
  }
  toolsApi.activity = async () => []
}
if (previewState === 'previous-cancelled') {
  const id = 'af040000-0000-4000-8000-000000000001'
  events = [{ ...event(1, 'user', 1, '지민', 'message', { content: '이전 작업을 중지했어요. 다음 요청을 준비합니다.' }), event_id: id }]
  toolsApi.activity = async () => []
  conversationsApi.listMembers = async room => [{ room_id: room, agent_id: 8, is_active: true, joined_at: at(0), paused: false, run_state: 'idle', work_state: { version: 1, room_id: room, agent_id: 8, scope: 'previous', phase: 'cancelled', trigger_event_id: id, request: { event_id: id, seq: 1 }, peer_agent_ids: [] } }]
}
if (previewState.startsWith('skill-receipt-')) {
  const ready = ['skill-receipt-ready','skill-receipt-binding-changed','skill-receipt-binding-mismatch'].includes(previewState)
  const id = (n: number) => `af042000-0000-4000-8000-${String(n).padStart(12,'0')}`
  const sourceRoot: RoomEvent = {...event(1,'user',1,'지민','message',{content:'회의 내용을 정리해 줘.'}),event_id:id(1)}
  const sourceResult: RoomEvent = {...event(2,'agent',8,'기획','message',{content:'결정 사항과 다음 행동을 정리했어요.',trigger_event_id:sourceRoot.event_id}),event_id:id(2)}
  const root: RoomEvent = {...event(3,'user',1,'지민','message',{content:ready ? '방금 한 정리 방식을 Skill로 저장하고 앞으로 사용해 줘.' : '방금 한 정리 방식을 Skill 초안으로 저장해 줘.'}),event_id:id(3)}
  const source = {room_id:501,root_event_id:sourceRoot.event_id,root_seq:1,result_event_id:sourceResult.event_id,result_seq:2,agent_id:8}
  const meta = {version:1 as const,receipt_id:'c'.repeat(64),operation:ready ? 'create_ready' as const : 'create_draft' as const,state:'saved' as const,agent_id:8,root_request_event_id:root.event_id,skill_id:skillId(1),revision:1,name:'회의 정리',description:'결정과 다음 행동 정리',content:'회의 내용을 읽고 결정 사항과 다음 행동을 정리합니다.',active:ready,source,creator_binding:ready ? {agent_id:8,skill_id:skillId(1),revision:1,enabled:true as const} : null}
  const result: RoomEvent = {...event(4,'system',null,'시스템','activity',{category:'skill_persistence',status:'saved',trigger_event_id:root.event_id,skill_persistence:meta}),event_id:id(4)}
  const snapshot: Skill = {version:1,skill_id:meta.skill_id,revision:1,name:meta.name,description:meta.description,content:meta.content,active:ready,source,created_at:at(16),updated_at:at(16)}
  const current: Skill = previewState === 'skill-receipt-history' ? {...snapshot,revision:2,name:'회의 정리 · 수정됨',updated_at:at(20)} : snapshot
  skillRows = [current]
  if (ready) skillBindings.set(meta.skill_id, previewState === 'skill-receipt-binding-changed' ? {revision:2,enabled:false} : previewState === 'skill-receipt-binding-mismatch' ? {revision:1,enabled:false} : {revision:1,enabled:true})
  events = [sourceRoot,sourceResult,root,result,{...event(5,'agent',8,'기획','message',{content:ready ? '앞서 사용한 정리 방식을 앞으로 참고할게요.' : '앞서 사용한 정리 방식의 초안을 저장했어요.',trigger_event_id:root.event_id}),event_id:id(5)}]
  toolsApi.activity = async () => []
  conversationsApi.listMembers = async id => [{room_id:id,agent_id:8,is_active:true,joined_at:at(0),paused:false,run_state:'idle',work_state:{version:1,room_id:id,agent_id:8,scope:'none',phase:'idle',peer_agent_ids:[]}}]
  const receipt = {...meta,root_ref:crossRef(root),result_ref:crossRef(result),replayed:true}
  skillsApi.persistence = async () => { if (previewState === 'skill-receipt-unknown') throw new Error('Synthetic read unavailable'); return structuredClone(previewState === 'skill-receipt-mismatch' ? {...receipt,content:'다른 내용'} : receipt) }
  skillsApi.versions = async id => ({version:1,skill_id:id,versions:[current,snapshot],next_before_revision:null})
}
if (previewState === 'skill-source-task') {
  events = inspectorEvents.slice(0,2)
  toolsApi.activity = async () => []
  conversationsApi.listMembers = async id => [{room_id:id,agent_id:8,is_active:true,joined_at:at(0),paused:false,run_state:'idle'}]
  const priorInspector = requestInspectorApi.get
  requestInspectorApi.get = async (room, trigger) => { const data = await priorInspector(room,trigger); return {...data,read_only:false,completion:{...data.completion,state:'completed',outcome_status:'succeeded',result_available:true,pending_agent_count:0},outcomes:data.outcomes.filter(x=>x.event_id===inspectorOutcome),outcome_count:1,outcomes_truncated:false,actions:[],action_count:0,actions_truncated:false} }
}
if (previewState.startsWith('memory-sharing-chat')) {
  toolsApi.activity = async () => []
  conversationsApi.listMembers = async id => [8,9].map(agent => ({ room_id:id,agent_id:agent,is_active:true,joined_at:at(0),paused:false,run_state:'idle' as const,work_state:{version:1 as const,room_id:id,agent_id:agent,scope:'none' as const,phase:'idle' as const,peer_agent_ids:[]} }))
}
if (previewState.startsWith('chat-persistence-') || previewState.startsWith('checkin-')) {
  toolsApi.activity = async () => []
  conversationsApi.listMembers = async id => [{ room_id: id, agent_id: 8, is_active: true, joined_at: at(0), paused: false, run_state: 'idle',
    work_state: { version: 1, room_id: id, agent_id: 8, scope: 'none', phase: 'idle', peer_agent_ids: [] } }]
}
if(previewState.startsWith('memory-import-')) {
  toolsApi.activity = async () => []
  conversationsApi.listMembers = async id => [{room_id:id,agent_id:8,is_active:true,joined_at:at(0),paused:false,run_state:'idle'}]
}
if (previewState.startsWith('memory-import-message-')) {
  const content = previewState === 'memory-import-message-long'
    ? '한국어로 핵심을 먼저 알려 주세요. '.repeat(30)
    : '간결한 한국어를 선호합니다.'
  const prefix = previewState === 'memory-import-message-invalid' ? '알 수 없는 형식\n' : importSourcePrefix
  events = [event(1, 'user', 1, '지민', 'message', { content: prefix + content + importSourceSuffix, memory_import: { version: 1, agent_id: 8, source_label: '예시 AI', content_sha256: 'a'.repeat(64) } })]
}
if (previewState === 'message-reference') {
  events = [
    ...Array.from({ length: 12 }, (_, i) => event(i + 1, 'user', 1, '지민', 'message', { content: `앞선 대화 ${i + 1}` })),
    { ...event(13, 'user', 1, '지민', 'message', { content: '참조한 원래 메시지입니다.' }), event_id: '929d363e-c8b1-466c-a4fc-f7e24a3adb8c' },
  ]
  toolsApi.activity = async () => []
  conversationsApi.listMembers = async id => [{ room_id: id, agent_id: 8, is_active: true, joined_at: at(0), paused: false, run_state: 'idle' }]
}
useAuthStore.getState().setAuth('synthetic-preview-token', { id: 1, email: 'preview@example.invalid', username: '지민' })
if (previewState === 'tool-review-retry-accepted') {
  const identity=`1:${useAuthStore.getState().epoch}`, generation=directoryViewGeneration(), failure='f01a0000-0000-4000-8000-000000000003', source='f01a0000-0000-4000-8000-000000000001', retried='f01a0000-0000-4000-8000-000000000004'
  const read=beginRetryRead(identity,generation,roomId,failure)
  if(read){finishRetryRead(identity,generation,read,{version:1,room_id:roomId,failure_event_id:failure,failure_seq:2,source_request:{event_id:source,seq:1,preview:'브라우저에서 화면을 확인해 줘.',href:`/chat/${roomId}?seq=1`},agent:{agent_id:8,name_snapshot:'기획',current_name:'기획'},current_model:{connection_id:1,model_id:'qwen3.7-plus',provider_kind:'compatible'},limits:{max_rounds:1,max_tool_calls:5,max_executor_seconds:45},eligible:true,blocked_reason:null,plan_hash:'c'.repeat(64)})
    const command=beginRetryCommand(identity,generation,roomId,failure,read.id)
    if(command)finishRetryCommand(identity,generation,command,{phase:'accepted',result:{version:1,room_id:roomId,source_failure_event_id:failure,source_trigger_event_id:source,source_request_event_id:source,retry_event_id:retried,retry_seq:3,agent_id:8,replayed:false,href:`/chat/${roomId}?seq=3`}})
  }
  events=[...events,{...event(3,'user',1,'지민','message',{content:'브라우저에서 화면을 확인해 줘.'}),event_id:retried},event(4,'agent',8,'기획','message',{content:'요청을 확인하고 결과를 정리했습니다.'})]
}
if(previewState==='memory-import-normal')updateImportWorkspace(`1:${useAuthStore.getState().epoch}`,()=>({source:'예시 AI',content:'한국어로 간결하게 답변하고, 다음 행동을 먼저 알려 주세요.',operation:null}))
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
// Synthetic draft adapter: missing fixtures cannot reach a real Core.
mcpDraftApi.read=async (_room, action) => { const preview=await toolsApi.preview(501,action); return {version:1,action_id:action,review_id:preview.review_id,revision:0,fields:[],values:{},edits:{},available:false,pending:false} }
mcpDraftApi.save=async()=>{throw Error('fixture unavailable')};mcpDraftApi.apply=async()=>{throw Error('fixture unavailable')};mcpDraftApi.command=async()=>{throw Error('fixture unavailable')}

if(['mcp-approval','mcp-delegated-valid','mcp-delegated-missing'].includes(previewState)||previewState.startsWith('mcp-draft-')) {
 const rootId='b2000000-0000-4000-8000-000000000011',actionId='b2000000-0000-4000-8000-000000000012';let reviewId='b2000000-0000-4000-8000-000000000013'
 const root:RoomEvent={...event(1,'user',1,'지민','message',{content:previewState.startsWith('mcp-draft-')?'팀에 보낼 안내 메일 초안을 준비해 줘. 보내기 전에 검토할게.':'문서에서 제목을 확인해 줘.'}),event_id:rootId};events=[root]
 const binding={server_id:mcpId,config_revision:1,binding_revision:1,tool_id:'a'.repeat(64),tool_name:previewState.startsWith('mcp-draft-')?'send_message':'read_document',schema_sha256:'b'.repeat(64),owner_id:1,agent_id:8,root_event_id:rootId,generation:1,transport_sha256:'c'.repeat(64),input_sha256:'d'.repeat(64),transport_type:'http' as const}
 const delegated=previewState.startsWith('mcp-delegated-'), teammate=delegated?{handoff_id:'b2000000-0000-4000-8000-000000000014',phase:'response' as const}:null
 const mcpBinding={...binding,...(teammate?{teammate_binding:teammate}:{})}
 const proof:ExecutionBinding={version:1,root_scope_id:'b2000000-0000-4000-8000-000000000015',scope_id:'b2000000-0000-4000-8000-000000000015',owner_id:1,root_room_id:501,root_request_event_id:rootId,execution_room_id:501,agent_id:8,run_generation:1,job_id:null,phase:null,context_mode:'user_room',deadline_at:'2099-10-29T00:00:00Z',lineage:[{scope_id:'b2000000-0000-4000-8000-000000000015',epoch:0}]}
 const scope=delegated?{execution_binding:proof,root_ref:{room_id:501,event_id:rootId,seq:1,actor_type:'user' as const,actor_id:1,kind:'message' as const},scope_status:'active' as const,revocation_state:null}:{}
 const target=previewState.startsWith('mcp-draft-')?'메일 도구 | {"type":"http","url":"https://example.invalid/mcp"} | send_message':'문서 도구 | {"type":"http","url":"https://example.invalid/mcp"} | read_document',action={...pendingAction,...scope,action_id:actionId,agent_id:8,trigger_event_id:rootId,family:'mcp_tool_call',target,expected_effect:'앱 도구를 한 번 실행합니다.',review_id:reviewId,mcp_binding:mcpBinding,scope:null}
 let waiting=true;toolsApi.activity=async id=>id===501&&waiting?[structuredClone(action)]:[]
 if(previewState.startsWith('mcp-draft-')) {
  sessionStorage.removeItem(`mcp-draft-command:1:501:${actionId}`)
  let draft:ActionDraft={version:1,action_id:actionId,review_id:reviewId,revision:0,available:true,pending:false,fields:previewState==='mcp-draft-empty'?[]:[{path:'/to',label:'받는 사람',role:'recipient',type:'string_array',max:120,max_items:8},{path:'/subject',label:'제목',role:'subject',type:'string',max:160},{path:'/body',label:'본문',role:'body',type:'string',max:2000}],values:{'/to':['team@example.invalid'],'/subject':'합성 제목','/body':'합성 메시지'},edits:{}}
  if(previewState==='mcp-draft-empty')draft={...draft,values:{}}
  if(previewState==='mcp-draft-applied')draft={...draft,pending:true,edits:{'/subject':'수정된 제목'}}
  const receipts=new Map<string,DraftReceipt>()
  mcpDraftApi.read=async()=>{if(previewState==='mcp-draft-stale')throw {response:{status:409}};return structuredClone(draft)}
  mcpDraftApi.save=async (_room,_action,revision,review,edits,key)=>{if(previewState==='mcp-draft-saving')return new Promise(()=>{});if(previewState==='mcp-draft-conflict')throw {response:{status:409}};draft={...draft,revision:revision+1,review_id:review,edits:structuredClone(edits),pending:Object.keys(edits).length>0};const receipt:DraftReceipt={version:1,operation:'save',state:'saved',command_key:key,action_id:actionId,review_id:review,revision:draft.revision};receipts.set(key,receipt);if(previewState==='mcp-draft-unknown')throw Error('synthetic lost response');return receipt}
  mcpDraftApi.apply=async (_room,_action,revision,_review,key)=>{reviewId='b2000000-0000-4000-8000-000000000019';action.review_id=reviewId;draft={...draft,review_id:reviewId,revision:revision+1,values:{...draft.values,...draft.edits},pending:false};const receipt:DraftReceipt={version:1,operation:'apply',state:'applied',command_key:key,action_id:actionId,review_id:reviewId,revision:draft.revision};receipts.set(key,receipt);return receipt}
  mcpDraftApi.command=async (_room,_action,key)=>{const receipt=receipts.get(key);if(!receipt)throw {response:{status:404}};return receipt}
 }
 toolsApi.preview=async()=>({action_id:actionId,review_id:reviewId,family:action.family,target,scope_target:target,expected_effect:action.expected_effect,scope_description:'synthetic',workflow_id:null,arguments:{document:'검토할 문서',password:'[redacted]'},payload_digest:'e'.repeat(64),...scope,mcp_binding:previewState==='mcp-delegated-missing'?binding:mcpBinding,approval_policy:{scopes:['once'],unverified_context_required:true,start_within_seconds:60},review:{version:delegated?2:1,captured_at:at(10),action:{action_id:actionId,room_id:501,agent_id:8,trigger_event_id:rootId,family:action.family,target,scope_target:target,expected_effect:action.expected_effect,payload_digest:'e'.repeat(64),...(delegated?{execution_binding:proof}:{}),mcp_binding:mcpBinding},authority:{owner_id:1,room_id:501,agent_id:8,proposer:{owner_id:1,room_id:501,agent_id:8,connection_id:1,provider_id:'synthetic',model_id:'synthetic'}},targets:[{role:'mcp_server',status:'configured'}],external_context:{account:'unsupported',target_version:'unsupported'}}})
 toolsApi.decide=async()=>{waiting=false;return {}}
}
if(previewScreen==='chat'&&previewState.startsWith('mcp-routine-')) {
 const rootId='b2100000-0000-4000-8000-000000000011',actionId='b2100000-0000-4000-8000-000000000012',reviewId='b2100000-0000-4000-8000-000000000013'
 const routine={version:1 as const,owner_id:1,agent_id:8,room_id:501,routine_id:'b2100000-0000-4000-8000-000000000014',run_id:'b2100000-0000-4000-8000-000000000015',config_revision:1,generation:1,root_event_id:rootId,origin:'test' as const,scheduled_at:at(10)}
 const info={version:1,owner_id:1,agent_id:8,routine_id:routine.routine_id,run_id:routine.run_id,config_revision:1,origin:previewState==='mcp-routine-origin-changed'?'scheduled':'test',scheduled_at:routine.scheduled_at}
 events=[{...event(1,'system',null,'시스템','activity',{category:'owner_routine',status:'started',content:'문서 제목 확인',routine:info}),event_id:rootId}]
 const rootRef={room_id:501,event_id:rootId,seq:1,actor_type:'system' as const,actor_id:null,kind:'activity' as const}
 const scope={routine_binding:routine,root_ref:rootRef,scope_status:'active' as const,revocation_state:null}
 const mcpBinding={server_id:mcpId,config_revision:1,binding_revision:1,tool_id:'a'.repeat(64),tool_name:'read_document',schema_sha256:'b'.repeat(64),owner_id:1,agent_id:8,root_event_id:rootId,generation:1,transport_sha256:'c'.repeat(64),input_sha256:'d'.repeat(64),transport_type:'http' as const}
 const target='문서 도구 | {\"type\":\"http\",\"url\":\"https://example.invalid/mcp\"} | read_document',action={...pendingAction,...scope,action_id:actionId,agent_id:8,trigger_event_id:rootId,family:'mcp_tool_call',target,expected_effect:'앱 도구를 한 번 실행합니다.',review_id:reviewId,mcp_binding:mcpBinding,scope:null}
 let waiting=true;toolsApi.activity=async id=>id===501&&waiting?[action]:[];toolsApi.grants=async()=>[]
 conversationsApi.listMembers=async id=>[{room_id:id,agent_id:8,is_active:true,joined_at:at(0),paused:false,run_state:'waiting'}]
 toolsApi.preview=async()=>({action_id:actionId,review_id:reviewId,family:action.family,target,scope_target:target,expected_effect:action.expected_effect,scope_description:'synthetic',workflow_id:null,arguments:{document:'합성 문서'},payload_digest:'e'.repeat(64),...scope,routine_binding:previewState==='mcp-routine-proof-changed'?{...routine,generation:2}:routine,mcp_binding:mcpBinding,approval_policy:{scopes:['once'],unverified_context_required:true,start_within_seconds:60},review:{version:1,captured_at:at(10),action:{action_id:actionId,room_id:501,agent_id:8,trigger_event_id:rootId,family:action.family,target,scope_target:target,expected_effect:action.expected_effect,payload_digest:'e'.repeat(64),execution_binding:null,mcp_binding:mcpBinding},authority:{owner_id:1,room_id:501,agent_id:8,proposer:{owner_id:1,room_id:501,agent_id:8,connection_id:1,provider_id:'synthetic',model_id:'synthetic'}},targets:[{role:'mcp_server',status:'configured'}],external_context:{account:'unsupported',target_version:'unsupported'}}})
 toolsApi.decide=async()=>{waiting=false;return {}}
}
if (previewScreen === 'browser-demonstration') {
 const id='a'.repeat(32), draft={name:'시연 초안',description:'브라우저 시연',content:'단계를 검토하고 수정하세요.'}
 const recovery=previewState.startsWith('demo-recovery'), conflict=previewState==='demo-recovery-conflict'
 let current:Demonstration={version:1,demo_id:id,agent_id:8,profile_id:'b'.repeat(32),profile_revision:1,review_revision:1,review:{goal:'화면 확인',steps:[],rules:'',fields:null},state:'recording',expires_in_seconds:600,goal:'화면 확인',steps:['Click a page control','Enter a value (not recorded)'],draft:null,source_trust:'untrusted_hints'}
 browserDemonstrationsApi.list=async()=>({items:recovery?[{...current,state:'stopped',expires_in_seconds:0,review:{goal:'저장된 목표',steps:['Click a button'],rules:'실패 시 중지',fields:draft},steps:['Click a button'],draft}]:[],next_after_demo_id:null,unavailable_count:0})
 browserDemonstrationsApi.updateReview=async(_room,_id,revision,review)=>{if(conflict&&revision===1){current={...current,state:'stopped',expires_in_seconds:0,steps:['Click a button'],draft,review_revision:2,review:{goal:'저장된 목표',steps:['Click a button'],rules:'서버에서 수정한 규칙',fields:{...draft,content:'서버에서 수정한 초안'}}};throw {response:{status:409,data:{detail:{code:'browser_demonstration_review_revision_conflict'}}}}}current={...current,review,review_revision:revision+1};return current}
 browserDemonstrationsApi.start=async(_room,body)=>{current={...current,goal:body.goal,steps:[body.include_target_labels?'Click a button: Continue':'Click a button','Enter a value (not recorded)'],state:previewState==='demo-interrupted'?'interrupted':'recording'};return current}
 demonstrationDraftApi.preflight=async(_room,_id,body)=>({agent_id:body.agent_id,review_revision:body.review_revision,connection_id:2,connection_name:'Synthetic Go',provider_id:'opencode-go',model_id:'synthetic-model',preflight_token:'a'.repeat(64),max_requests:1,tools:false})
 demonstrationDraftApi.submit=async(room,_id,body)=>({room_id:room,agent_id:body.agent_id,event_id:'af041000-0000-4000-8000-000000000801',status:'submitted',replayed:false})
 demonstrationDraftApi.stop=async(room)=>({room_id:room,agent_id:8,event_id:'af041000-0000-4000-8000-000000000801',status:'cancelled',review_revision:current.review_revision,fields:null,failure_code:null})
 demonstrationDraftApi.read=async(room)=>({room_id:room,agent_id:8,event_id:'af041000-0000-4000-8000-000000000801',status:'completed',review_revision:current.review_revision,fields:{name:'합성 초안',description:'검토 필요',content:'직접 검토한 초안 내용'},failure_code:null})
 browserDemonstrationsApi.command=async()=>current
 browserDemonstrationsApi.read=async()=>recovery&&current.state==='recording'?{...current,state:'stopped',expires_in_seconds:0,review:{goal:'저장된 목표',steps:['Click a button'],rules:'실패 시 중지',fields:draft},steps:['Click a button'],draft}:current
 browserDemonstrationsApi.stop=async()=>{current={...current,state:'stopped',expires_in_seconds:0,draft,review:{goal:current.goal,steps:current.steps,rules:'',fields:null}};return current}
 browserDemonstrationsApi.save=async(_room,_id,body)=>{if(previewState==='demo-unknown')throw Error('Synthetic unknown save result');return {version:1,skill_id:'af041000-0000-4000-8000-000000000801',revision:1,...body,active:false,source:{kind:'browser_demonstration',demo_id:id,room_id:501,agent_id:8,profile_id:'b'.repeat(32),profile_revision:1,trust:'untrusted_hints'},created_at:at(1),updated_at:at(1)}}
 skillsApi.command=async()=>{throw Error('Synthetic command unavailable')}
}
// These stories represent a settled tool result, without the default live actions or runs.
if (previewScreen === 'chat' && (['tool-review-changed','tool-review-success','tool-review-failure'].includes(previewState) || previewState.startsWith('tool-review-retry-'))) {
  toolsApi.activity = async () => []
  toolsApi.grants = async () => []
  conversationsApi.listMembers = async id => [{room_id:id,agent_id:8,is_active:true,joined_at:at(0),paused:false,run_state:'idle',work_state:{version:1,room_id:id,agent_id:8,scope:'none',phase:'idle',peer_agent_ids:[]}}]
}
if (previewScreen === 'chat' && ['grant-scope-once','grant-scope-session'].includes(previewState)) {
  const root='a55a0000-0000-4000-8000-000000000001', actionId='a55a0000-0000-4000-8000-000000000002', reviewId='a55a0000-0000-4000-8000-000000000003'
  const target='https://example.invalid/document', scopeTarget='https://example.invalid', digest='e'.repeat(64)
  const action={action_id:actionId,room_id:roomId,agent_id:8,trigger_event_id:root,family:'browser_navigate',target,expected_effect:'Navigate browser',state:'waiting',scope:null,grant_id:null,requested_at:at(18),started_at:null,completed_at:null,review_id:reviewId}
  events=[{...event(1,'user',1,'지민','message',{content:'이 문서 페이지를 확인해 줘.'}),event_id:root}]
  conversationsApi.listMembers=async id=>[{room_id:id,agent_id:8,is_active:true,joined_at:at(0),paused:false,run_state:'waiting'}]
  toolsApi.activity=async()=>[action]
  toolsApi.grants=async()=>[]
  toolsApi.preview=async()=>({action_id:actionId,family:action.family,target,scope_target:scopeTarget,expected_effect:action.expected_effect,arguments:{url:target},scope_description:'Synthetic reviewed origin for this Bot and room session.',workflow_id:'synthetic-workflow',payload_digest:digest,review_id:reviewId,approval_policy:{scopes:previewState==='grant-scope-once'?['once']:['once','room_session'],unverified_context_required:false,start_within_seconds:null},review:{version:1,captured_at:at(18),action:{action_id:actionId,room_id:roomId,agent_id:8,trigger_event_id:root,family:action.family,target,scope_target:scopeTarget,expected_effect:action.expected_effect,payload_digest:digest},authority:{owner_id:1,room_id:roomId,agent_id:8,proposer:{owner_id:1,room_id:roomId,agent_id:8,connection_id:1,provider_id:'synthetic',model_id:'synthetic-model'}},targets:[{role:'browser_origin',status:'verified',origin:scopeTarget}],external_context:{account:'unsupported',target_version:'unsupported'}}})
}
if(previewScreen==='chat'&&previewState.startsWith('persistent-reply-')) {
  conversationsApi.listMembers=async id=>[8,9].map(agent=>({room_id:id,agent_id:agent,is_active:true,joined_at:at(0),paused:false,run_state:'idle' as const,work_state:{version:1 as const,room_id:id,agent_id:agent,scope:'none' as const,phase:'idle' as const,peer_agent_ids:[]}}))
  toolsApi.activity=async()=>[]
  let lost = previewState === 'persistent-reply-unknown'
  eventsApi.send=async(room,content,key,outputKind,replyTo)=>{
    const existing=events.find(row=>row.idempotency_key===key); if(existing)return existing
    const result={...event(3,'user',1,'지민','message',{content,output_kind:outputKind,...(replyTo?{reply_to:replyTo}:{})}),room_id:room,event_id:'b33a0000-0000-4000-8000-000000000003',idempotency_key:key}
    events=[...events,result]; if(lost){lost=false;throw Error('Synthetic response lost')} return result
  }
}
if(previewScreen==='chat'&&previewState.startsWith('quote-reply-')) {
  conversationsApi.listMembers=async id=>[8,9].map(agent=>({room_id:id,agent_id:agent,is_active:true,joined_at:at(0),paused:false,run_state:'idle' as const,work_state:{version:1 as const,room_id:id,agent_id:agent,scope:'none' as const,phase:'idle' as const,peer_agent_ids:[]}}))
  toolsApi.activity=async()=>[]
  eventsApi.send=async(room,content,key)=>{const result={...event(3,'user',1,'지민','message',{content}),room_id:room,event_id:'b33a0000-0000-4000-8000-000000000003',idempotency_key:key};events=[...events.filter(row=>row.event_id!==result.event_id),result];return result}
}
if(previewScreen==='chat'&&(previewState.startsWith('reaction-')||previewState.startsWith('bot-reaction-'))) {
  const receipts=new Map<string,{target:string;eventId:string;seq:number}>();let conflict=previewState==='reaction-conflict',lost=previewState==='reaction-unknown'
  const snapshot=(room:number,target:string):ReactionValue=>{const last=events.filter(row=>row.kind==='activity'&&row.actor_type==='user'&&row.actor_id===1&&row.payload.category==='message_reaction'&&row.payload.target_event_id===target).at(-1);return {version:1,room_id:room,target_event_id:target,owner_id:1,current_seq:last?.seq||0,current_event_id:last?.event_id||null,emoji:(last?.payload.emoji??null) as ReactionEmoji|null,replayed:false}}
  const append=(target:string,emoji:ReactionEmoji|null,expected:number)=>{const seq=events.length+1,eventId=`e6ac0000-0000-4000-8000-${String(seq).padStart(12,'0')}`;const row={...event(seq,'user',1,'지민','activity',{category:'message_reaction',version:1,target_event_id:target,emoji,expected_seq:expected}),event_id:eventId};events=[...events,row];return row}
  messageReactionsApi.read=async(room,target)=>snapshot(room,target)
  messageReactionsApi.put=async(room,target,emoji,expected,key)=>{if(conflict){conflict=false;append(target,'👀',expected);throw {response:{status:409,data:{detail:{code:'reaction_stale'}}}}}const prior=receipts.get(key);if(prior){const value=snapshot(room,target);return {...value,applied_seq:prior.seq,applied_event_id:prior.eventId,replayed:true}}if(snapshot(room,target).current_seq!==expected)throw {response:{status:409,data:{detail:{code:'reaction_stale'}}}};const row=append(target,emoji,expected);receipts.set(key,{target,eventId:row.event_id,seq:row.seq});const value={...snapshot(room,target),applied_seq:row.seq,applied_event_id:row.event_id};if(lost){lost=false;throw Error('Synthetic response lost')}return value}
  messageReactionsApi.command=async(room,target,key)=>{const receipt=receipts.get(key);if(!receipt||receipt.target!==target)throw {response:{status:404}};return {...snapshot(room,target),applied_seq:receipt.seq,applied_event_id:receipt.eventId,replayed:true}}
  messageReactionsApi.applied=async(room,seq)=>events.find(row=>row.room_id===room&&row.seq===seq)||null
  conversationsApi.listMembers=async id=>(previewState.startsWith('bot-reaction-')?[8,9]:[8]).map(agentId=>({room_id:id,agent_id:agentId,is_active:true,joined_at:at(0),paused:false,run_state:'idle' as const,work_state:{version:1 as const,room_id:id,agent_id:agentId,scope:'none' as const,phase:'idle' as const,peer_agent_ids:[]}}))
  toolsApi.activity=async()=>[]
}
if(previewState==='page-owner-rename-source')events=[
 {...event(1,'user',1,'지민','message',{content:'합성 페이지를 만들어 줘.'}),event_id:'af041000-0000-4000-8000-000000000840'},
 {...event(2,'user',1,'지민','activity',{category:'page_owner_rename',version:1,result_id:'af041000-0000-4000-8000-000000000801',base_version:1,page_version:2,document_sha256:'a'.repeat(64)}),event_id:'af041000-0000-4000-8000-000000000841'}]
if (previewScreen === 'vault') {
 const card:Card={item_id:'e'.repeat(32),cardholder:'Example Person',expiry_month:5,expiry_year:2031,billing:{address_line1:'123 Sample Street',address_line2:'',city:'London',region:'',postal_code:'SW1A 1AA',country:'United Kingdom'},nickname:'Sample card',hint:'Synthetic only',last4:'1234',revision:1,created_at:'2026-11-01',updated_at:'2026-11-01',secret_status:{version:1,status:previewState==='vault-card-unavailable'?'unavailable':previewState==='vault-card-missing'?'missing':previewState==='vault-card-mismatch'?'metadata_mismatch':'present',revision:1,command_id:'f'.repeat(32),last4:previewState==='vault-card-unavailable' || previewState==='vault-card-missing' || previewState==='vault-card-mismatch'?null:'1234'}}
 let cardInvalidSeen=false, cardFillCalls=0
 let cardItems:Card[]=previewState==='vault-card-empty'?[]:[card];const cardReceipts=new Map<string,CardReceipt>();sessionStorage.removeItem('vault.cards.pending.1');sessionStorage.removeItem('vault.cards.native.1')
 cardApi.list=async()=>({version:1,items:cardItems,next_cursor:null});cardApi.read=async id=>{const found=cardItems.find(x=>x.item_id===id);if(!found)throw {response:{status:404}};return found}
 cardApi.create=async(value,key)=>{const r:CardReceipt={version:1,operation:'create',command_key:key,item_id:'d'.repeat(32),revision:1};cardItems=[...cardItems,{...card,...value,item_id:r.item_id,last4:null,secret_status:{version:1,status:'missing',revision:null,command_id:null,last4:null}}];cardReceipts.set(key,r);if(previewState==='vault-card-unknown')throw Error('unknown');return r}
 cardApi.update=async(id,value,rev,key)=>{if(previewState==='vault-card-conflict')throw {response:{status:409}};const r:CardReceipt={version:1,operation:'update',command_key:key,item_id:id,revision:rev+1};cardItems=cardItems.map(c=>c.item_id===id?{...c,...value,revision:r.revision}:c);cardReceipts.set(key,r);return r}
 cardApi.confirm=async(id,rev,_sr,_sc,key)=>{cardItems=cardItems.map(x=>x.item_id===id?{...x,last4:x.secret_status.last4,revision:rev+1}:x);return {version:1,operation:'confirm',command_key:key,item_id:id,revision:rev+1}};cardApi.remove=async(id,rev,key)=>{cardItems=cardItems.filter(x=>x.item_id!==id);return {version:1,operation:'delete',command_key:key,item_id:id,revision:rev+1}};cardApi.receipt=async key=>{const r=cardReceipts.get(key);if(!r)throw {response:{status:404}};return r};cardApi.nativeAvailable=()=>true
 cardApi.native=async args=>{if(args.operation==='open_and_fill'){const mode=previewState==='vault-card-fill-unknown'?'unknown':previewState==='vault-card-fill-unavailable'?'not_started':previewState==='vault-card-fill-cancelled'?'cancelled':previewState==='vault-card-fill-retry' && cardFillCalls++===0?'not_filled':'filled';return {version:1,status:mode,revision:args.expectedRevision,command_id:args.commandId,last4:null}}if(previewState==='vault-card-cancelled')throw 'vault_cancelled';if(previewState==='vault-card-not-started')return {version:1,status:'not_started',revision:args.expectedRevision,command_id:args.commandId,last4:null};if(previewState==='vault-card-invalid-input'){if(!cardInvalidSeen){cardInvalidSeen=true;return {version:1,status:'invalid_input',revision:args.expectedRevision,command_id:args.commandId,last4:null}}const revision=(args.expectedRevision ?? 0)+1;cardItems=cardItems.map(x=>x.item_id===args.itemId?{...x,secret_status:{version:1,status:'present',revision,command_id:args.commandId,last4:'1234'}}:x);return {version:1,status:'stored',revision,command_id:args.commandId,last4:'1234'}}if(previewState==='vault-card-auto'){const deleted=args.operation==='delete',revision=(args.expectedRevision ?? 0)+1;cardItems=cardItems.map(x=>x.item_id===args.itemId?{...x,secret_status:{version:1,status:deleted?'deleted':'present',revision,command_id:args.commandId,last4:deleted?null:'1234'}}:x);return {version:1,status:deleted?'deleted':'stored',revision,command_id:args.commandId,last4:deleted?null:'1234'}}return {version:1,status:'unavailable',revision:null,command_id:args.commandId,last4:null}}

 const contact:Contact={item_id:'b'.repeat(32),name:'Morgan Example',email:'morgan@example.invalid',phone:'+44 20 0000 0000',address_line1:'123 Long International Example Street',address_line2:'Suite 20',city:'London',region:'Greater London',postal_code:'SW1A 1AA',country:'United Kingdom',nickname:'Morgan',hint:'Synthetic example',revision:1,created_at:'2026-11-01',updated_at:'2026-11-01'}
 let contactItems:Contact[]=previewState==='vault-contacts-empty'?[]:[contact];const receipts=new Map<string,ContactReceipt>()
 sessionStorage.removeItem('vault.contacts.pending.1')
 contactApi.list=async()=>{if(previewState==='vault-contacts-error')throw Error('offline');return {version:1,items:contactItems,next_cursor:null}}
 contactApi.create=async(value,key)=>{const r:ContactReceipt={version:1,operation:'create',command_key:key,item_id:'d'.repeat(32),revision:1};contactItems=[...contactItems,{...contact,...value,item_id:r.item_id}];receipts.set(key,r);if(previewState==='vault-contacts-unknown')throw Error('unknown');return r}
 contactApi.update=async(id,value,rev,key)=>{if(previewState==='vault-contacts-conflict')throw {response:{status:409}};const r:ContactReceipt={version:1,operation:'update',command_key:key,item_id:id,revision:rev+1};contactItems=contactItems.map(c=>c.item_id===id?{...c,...value,revision:r.revision}:c);receipts.set(key,r);return r}
 contactApi.remove=async(id,rev,key)=>{const r:ContactReceipt={version:1,operation:'delete',command_key:key,item_id:id,revision:rev+1};contactItems=contactItems.filter(c=>c.item_id!==id);receipts.set(key,r);return r}
 contactApi.receipt=async key=>{const r=receipts.get(key);if(!r)throw {response:{status:404}};return r}

 sessionStorage.removeItem('vault.pending.1')
 sessionStorage.removeItem('vault.fill.pending.1')
 sessionStorage.removeItem('vault.window.1')
 sessionStorage.removeItem('vault.csv.pending.1')
 sessionStorage.removeItem('vault.fill.previous.1')
 const item:VaultItem={version:1,item_id:'a'.repeat(32),website:'https://example.invalid',username:'sample@example.invalid',nickname:'Sample',hint:'Example only',revision:1,is_active:true,created_at:'2026-11-01T00:00:00Z',updated_at:'2026-11-01T00:00:00Z',secret_status:{version:1,status:previewState==='vault-mismatch'?'metadata_mismatch':previewState==='vault-unavailable'?'unavailable':previewState==='vault-no-saved'?'missing':'present',revision:previewState==='vault-no-saved'?null:1,command_id:null}}
 let csvLastCommand:string|null=null
 const csvDraft='c'.repeat(32),csvBatch=previewState==='vault-csv-batch' || previewState==='vault-csv-batch-unknown'
 const csvReceipts=new Map<string,{version:1;operation:'import_reserve';command_key:string;state:'created'|'duplicate';item_id:string;revision:number;draft_id:string;row:number}>()
 const csvStates:Record<number,'pending'|'stored'|'unknown'|'rejected'>={2:'pending',3:'pending'}
 vaultApi.csvChoose=async()=>{if(previewState==='vault-csv-cancelled')throw 'cancelled';return {version:1,draft_id:csvDraft,total:csvBatch?2:1,invalid_count:csvBatch||previewState==='vault-csv-invalid'?1:0,expires_in_seconds:300}}
 vaultApi.csvPage=async args=>csvBatch?(args.offset===0?{version:1,rows:[{row:2,website:item.website,username:item.username,nickname:item.nickname,hint:item.hint,state:csvStates[2]}],errors:[],next_offset:1}:{version:1,rows:[{row:3,website:'https://second.invalid',username:'second@example.invalid',nickname:'Second',hint:'',state:csvStates[3]}],errors:[{row:4,reason:'invalid_website'}],next_offset:null}):({version:1,rows:[{row:2,website:item.website,username:item.username,nickname:item.nickname,hint:item.hint,state:'pending'}],errors:previewState==='vault-csv-invalid'?[{row:3,reason:'invalid_website'}]:[],next_offset:null})
 vaultApi.importReserve=async (_value,key,draftId,row)=>{if(previewState==='vault-csv-unknown')throw Error('unknown');const r={version:1 as const,operation:'import_reserve' as const,command_key:key,state:(previewState==='vault-csv-duplicate' || csvBatch && row===2?'duplicate':'created') as 'created'|'duplicate',item_id:csvBatch && row===3?'d'.repeat(32):item.item_id,revision:1,draft_id:draftId,row};csvReceipts.set(key,r);return r}
 vaultApi.importReceipt=async key=>{if(previewState==='vault-csv-unknown')throw {response:{status:404}};const r=csvReceipts.get(key);if(!r)throw {response:{status:404}};return r}
 vaultApi.importBind=async key=>({version:1,operation:'import_bind',command_key:key,native_ready:previewState!=='vault-csv-bind-unavailable'})
 vaultApi.csvSkip=async args=>{csvStates[args.row]='rejected';return {version:1,status:'rejected',revision:null,command_id:null}}
 vaultApi.csvStore=async args=>{csvLastCommand=args.commandId;const status=previewState==='vault-csv-rejected'?'rejected':previewState==='vault-csv-batch-unknown'?'unknown':'stored';csvStates[args.row]=status;return {version:1,status,revision:status==='stored'?1:null,command_id:args.commandId}}
 vaultApi.csvReconcile=async args=>({version:1,status:'unknown',revision:null,command_id:args.commandId})
 vaultApi.csvCancel=async()=>({version:1,status:'cancelled'})
 vaultApi.session=async()=>({version:1,owner_id:previewState==='vault-other-owner'?2:1,owner_epoch:'a'.repeat(64),identity_attested:true})
 vaultApi.list=async()=>({version:1,items:[item],next_cursor:null})
 vaultApi.read=async itemId=>csvLastCommand?{...item,item_id:itemId,...(csvBatch && itemId==='d'.repeat(32)?{website:'https://second.invalid',username:'second@example.invalid',nickname:'Second',hint:''}:{}),secret_status:{version:1,status:'present',revision:1,command_id:csvLastCommand}}:{...item,item_id:itemId}
 vaultApi.nativeAvailable=()=>true
 let fillCalls=0
 vaultApi.fill=async args=>({version:1,status:previewState==='vault-fill-cancelled'?'cancelled':previewState==='vault-fill-failed' || (previewState==='vault-fill-retry' && fillCalls++===0)?'not_filled':previewState==='vault-fill-unknown'?'unknown':previewState==='vault-fill-password-only'?'filled_password_only':'filled',revision:args.expectedRevision,command_id:args.commandId})
 vaultApi.closeBrowser=async args=>({version:1,status:'closed',revision:null,command_id:args.commandId})
 vaultApi.native=async args=>{if(previewState==='vault-cancelled')throw 'vault_cancelled';return {version:1,status:args.operation==='delete'?'deleted':args.operation==='replace'?'stored':'present',revision:2,command_id:args.commandId??null}}
 vaultApi.create=async (_value,key)=>{if(previewState==='vault-unknown')throw Error('unknown');if(previewState==='vault-conflict')throw {response:{status:409}};return {version:1,operation:'create',command_key:key,item_id:item.item_id,revision:1}}
 vaultApi.update=async (_id,_value,_rev,key)=>{if(previewState==='vault-unknown')throw Error('unknown');if(previewState==='vault-conflict')throw {response:{status:409}};return {version:1,operation:'update',command_key:key,item_id:item.item_id,revision:2}}
 vaultApi.receipt=async key=>{if(previewState==='vault-unknown')throw {response:{status:404}};return {version:1,operation:'create',command_key:key,item_id:item.item_id,revision:1}}
 vaultApi.delete=async (_id,_rev,key)=>({version:1,operation:'delete',command_key:key,item_id:item.item_id,revision:2})
}
// Only the two read-only-failure stories install these synthetic GET responses.
const priorBrowserAdapter=api.defaults.adapter
let browserFixtureAdapter:typeof api.defaults.adapter
if (previewScreen==='chat' && ['readonly-failure-parked','readonly-failure-unknown'].includes(previewState)) {
 const profile='1'.repeat(32),handoff='e'.repeat(32),unknown=previewState==='readonly-failure-unknown'
 sessionStorage.removeItem(`gamja.vaultProfileHandoff.unknown:1:501:${profile}:${handoff}`)
 toolsApi.activity=async room=>room===501?[{action_id:'af042000-0000-4000-8000-000000000002',room_id:501,agent_id:8,trigger_event_id:'af042000-0000-4000-8000-000000000001',family:'browser_inspect',target:'https://example.invalid/',expected_effect:'Inspect browser page',state:'completed',scope:'once',grant_id:1,requested_at:at(14),started_at:at(15),completed_at:at(16)}]:[]
 browserFixtureAdapter=async config=>{
  if(config.method!=='get')throw Error('Synthetic fixture blocks effects')
  const path=config.url||'';let data:unknown
  if(path==='/tools/rooms/501/browser-profiles')data={items:[{id:profile,name:'합성 Bot 프로필',account_label:'합성 계정',agent_ids:[8],origin_urls:['https://example.invalid'],revision:1,controller:'settling',agent_id:null,connected:false,last_used_at:null,expires_in_seconds:1200,home_room_id:501,shared_room_ids:[]}]}
  else if(path===`/tools/rooms/501/browser-profiles/${profile}/vault-handoff`)data={version:1,profile_id:profile,revision:1,handoff:{handoff_id:handoff,owner_id:1,owner_epoch:'a'.repeat(64),profile_id:profile,profile_revision:1,room_id:501,origin:'https://example.invalid',username:'demo@example.invalid',profile_label:'합성 Bot 프로필',account_label:'합성 계정',state:'prepared',prior_outcome_unknown:false},native_status:{version:1,status:unknown?'unknown':'active',handoff_id:handoff,profile_id:profile,profile_revision:1,fill_status:unknown?'unknown':'not_filled'},allowed_origins:[['https','example.invalid',443]],can_fill:false,can_return:false,can_recover:true}
  else if(path===`/tools/rooms/501/browser-profiles/${profile}/vault-handoff/custody`)data={version:1,status:unknown?'unknown':'parked',handoff_id:handoff,profile_id:profile,profile_revision:1,custody_id:'b'.repeat(32)}
  else throw Error(`[UI fixture missing] GET ${path}`)
  return {data,status:200,statusText:'OK',headers:new AxiosHeaders(),config}
 }
 api.defaults.adapter=browserFixtureAdapter
}
if (previewScreen === 'chat' && ['teammate-incomplete-failure', 'teammate-changed-failure'].includes(previewState)) {
 browserFixtureAdapter = async config => {
  if (config.method?.toLowerCase() === 'get' && config.url === '/conversations/501/shared-binding')
   return {data:{kind:'ordinary'},status:200,statusText:'OK',headers:new AxiosHeaders(),config}
  throw Error(`[UI fixture missing] ${config.method} ${config.url}`)
 }
 api.defaults.adapter = browserFixtureAdapter
}
if(previewScreen==='chat'&&previewState.startsWith('routine-chat-')){
 browserFixtureAdapter=async config=>{
  if(config.method?.toLowerCase()==='get'&&/^\/conversations\/\d+\/shared-binding$/.test(config.url||''))return {data:{kind:'ordinary'},status:200,statusText:'OK',headers:new AxiosHeaders(),config}
  if(config.method?.toLowerCase()==='post'&&/^\/conversations\/\d+\/routine-reference\/resolve$/.test(config.url||''))return {data:previewState==='routine-chat-rejected'?{version:1,state:'rejected',code:'routine_reference_room_changed'}:previewState==='routine-chat-mismatch'?{version:1,state:'accepted',event_id:'af041000-0000-4000-8000-000000000823',seq:99}:{version:1,state:'pending'},status:200,statusText:'OK',headers:new AxiosHeaders(),config}
  throw Error(`[UI fixture missing] ${config.method} ${config.url}`)
 }
 api.defaults.adapter=browserFixtureAdapter
 let routineSendCount=0
 eventsApi.send=async(room,content,key,output='auto',reply=null,routineRef=null)=>{if(['routine-chat-unknown','routine-chat-rejected','routine-chat-mismatch','routine-chat-delayed'].includes(previewState)&&routineSendCount++===0)throw Error('Synthetic response lost');return ({event_id:'af041000-0000-4000-8000-000000000823',device_id:null,room_id:room,seq:20,actor_type:'user',actor_id:1,actor_name_snapshot:'Synthetic owner',kind:'message',payload:{content,output_kind:output,reply_to:reply,...(routineRef?{routine_ref:routineRef}:{})},created_at:at(20),idempotency_key:key})}
}
const initialEntry = previewScreen === 'vault' ? '/vault' : previewScreen === 'memory-import' ? '/memory-import' : previewScreen === 'bot-library' ? '/agents/8/library' : previewScreen === 'apps' ? '/apps' : previewScreen === 'connections'
  ? `/connections${previewState === 'edit' ? '?edit=1' : previewState.startsWith('first-success') || previewState === 'chatgpt-image-option' ? '?first=1' : ''}`
  : previewScreen === 'skills' ? (previewState.startsWith('skill-source') ? `/skills?room=501&request=11111111-1111-4111-8111-111111111111&result=22222222-2222-4222-8222-222222222222&agent=8` : '/skills') : previewScreen === 'usage' ? '/usage' : previewScreen === 'devices' ? '/devices' : previewScreen === 'agents' || previewScreen === 'bot-memory' ? '/agents' : previewScreen === 'projects' ? '/projects' : previewScreen === 'dashboard' ? '/'
    : (previewState==='page-workspace-failure'||previewState==='page-workspace-failure-generic') ? '/chat/501?result=af041000-0000-4000-8000-000000000801&version=1'
    : previewState==='page-owner-rename-source' ? '/chat/501?seq=2&event=af041000-0000-4000-8000-000000000841'
    : (previewState.startsWith('page-revision-')||previewState.startsWith('page-workspace-')) ? '/chat/501?result=af041000-0000-4000-8000-000000000801&version=2'
    : `/chat/${previewState === 'bot-message-incoming' || previewState.startsWith('parallel-incoming') ? 701 : roomId}${previewState === 'deepseq' ? '?seq=2' : previewState === 'missingseq' ? '?seq=999' : ''}`

function TemplateLinkAgentsPreview(){
 const auth=useAuthStore(),[request,setRequest]=useState<{id:string;url:string;identity:string}|null>(()=>({id:'synthetic-link',url:'https://templates.example.org/t/11111111-1111-4111-8111-111111111111',identity:`${auth.user?.id||0}:${auth.epoch}`}))
 return <Agents templateRequest={request} onTemplateOpened={()=>setRequest(null)}/>
}
if(previewScreen==='chat'&&previewState==='teammate-page-created') {
 const resultId='af041000-0000-4000-8000-000000000861',sourceId='af041000-0000-4000-8000-000000000862'
 agents[0].name='Go 리더';agents[1].name='OAuth 작업자';agents[1].model_id='gpt-6-luna';agents[1].model='gpt-6-luna'
 const meta={version:1,handoff_id:teammateId,sender_agent_id:8,recipient_agent_id:9,depth:1}
 events=[
  {...event(1,'user',1,'지민','message',{content:'합성 표를 페이지로 만들어 주세요. OAuth 작업자에게 부탁해 주세요.'}),event_id:teammateRoot},
  {...event(2,'agent',8,'Go 리더','message',{content:'OAuth 작업자에게 합성 페이지 작성을 부탁합니다.',trigger_event_id:teammateRoot,teammate_handoff:{...meta,phase:'request'}}),event_id:teammateRequest},
  {...event(3,'system',null,'시스템','activity',{category:'teammate_handoff',status:'running',handoff_id:teammateId,root_request_event_id:teammateRoot,sender_agent_id:8,recipient_agent_id:9}),event_id:'af041000-0000-4000-8000-000000000863'},
  {...event(4,'tool',null,'도구','tool_result',{family:'write_page',status:'completed',trigger_event_id:teammateRoot,delegated_origin:{root_room_id:501,root_request_event_id:teammateRoot,job_kind:'teammate',job_id:teammateId,handoff_id:teammateId,phase:'response'},page_result:{version:1,room_id:501,result_id:resultId,page_version:1,base_version:null,title:'합성 위임 페이지',document_sha256:'a'.repeat(64),agent_id:9}}),event_id:sourceId},
  {...event(5,'agent',9,'OAuth 작업자','message',{content:'합성 페이지를 만들었습니다. SYNTHETIC-TEAMMATE-PAGE',trigger_event_id:teammateRoot,teammate_handoff:{...meta,phase:'response'}}),event_id:teammateResult},
  {...event(6,'agent',8,'Go 리더','message',{content:'OAuth 작업자가 만든 합성 페이지를 확인해 주세요.',trigger_event_id:teammateRoot,teammate_handoff:{...meta,phase:'return'}}),event_id:teammateReturn},
 ]
 const summary:ResultSummary={result_id:resultId,room_id:501,version:1,kind:'page',title:'합성 위임 페이지',author_agent_id:9,author_name_snapshot:'OAuth 작업자',state:'draft',state_revision:1,created_at:at(17),updated_at:at(17),linked_opinion_count:0}
 const doc=JSON.stringify({format:'gamja.page.v1',title:'합성 위임 페이지',datasets:[{id:'sample',columns:[{key:'item',label:'합성 항목',type:'text'},{key:'count',label:'수량',type:'number'}],rows:[{item:'SYNTHETIC-TEAMMATE-PAGE',count:2}]}],views:[{id:'main',dataset:'sample',filters:[]}],blocks:[{type:'heading',level:1,text:'합성 자료'},{type:'table',view:'main',columns:['item','count'],title:'합성 표'}]})
 const detail:ResultDetail={...summary,current_version:1,source:{event_id:sourceId,seq:4,href:`/chat/501?seq=4&event=${sourceId}`},execution:{trigger_event_id:teammateRoot,round:null,generation:null,model_use:null},content:doc,file_ref:null,file_status:null,evidence:[],evidence_status:'none',review_support:'source_reply_only',link_report:null,state_owner_id:null,state_changed_at:null,link_report_owner_id:null,link_reported_at:null}
 roomResultsApi.list=async room=>{if(room!==501)throw Error('Synthetic room mismatch');return {room_id:room,items:[structuredClone(summary)],next_cursor:null}}
 roomResultsApi.pageCurrent=async(room,id)=>{if(room!==501||id!==resultId)throw Error('Synthetic Page mismatch');return structuredClone(detail)}
 roomResultsApi.detail=async(room,id,version)=>{if(room!==501||id!==resultId||version!==1)throw Error('Synthetic Page version mismatch');return structuredClone(detail)}
 roomResultsApi.versions=async(room,id)=>{if(room!==501||id!==resultId)throw Error('Synthetic Page mismatch');return {room_id:room,result_id:id,current_version:1,items:[structuredClone(summary)],next_before_version:null}}
 roomResultsApi.pageConversation=async(room,id)=>{if(room!==501||id!==resultId)throw Error('Synthetic Page mismatch');return {version:1,room_id:room,result_id:id,upper_seq:6,items:[],root_states:[],next_after_seq:null}}
 roomResultsApi.reviews=async()=>({items:[],next_cursor:null})
 conversationsApi.listMembers=async room=>[8,9].map(agent=>({room_id:room,agent_id:agent,is_active:true,joined_at:at(0),paused:false,run_state:'idle' as const,work_state:{version:1 as const,room_id:room,agent_id:agent,scope:'none' as const,phase:'idle' as const,peer_agent_ids:[]}}))
 botsApi.metadata=async room=>({version:1 as const,room_id:room,personal_agent_id:null,can_change_members:true})
 eventsApi.list=async(room,after,limit=100)=>{if(room!==501)throw Error('Synthetic room mismatch');return events.filter(item=>item.seq>after).slice(0,limit)}
 teammatesApi.list=async room=>({version:1,room_id:room,teammates:agents.slice(0,2).map(agent=>({agent_id:agent.id,name:agent.name,description:agent.description,model_id:agent.model_id})),handoffs:[{...teammateHandoff,sender_name:'Go 리더',recipient_name:'OAuth 작업자',content:'OAuth 작업자에게 합성 페이지 작성을 부탁합니다.',state:'completed' as const,reply_state:'delivered' as const,request_seq:2,result_event_id:teammateResult,result_seq:5,return_event_id:teammateReturn,return_seq:6}],handoffs_truncated:false})
 toolsApi.activity=async()=>[];toolsApi.grants=async()=>[]
}
const element = (<StrictMode><I18nProvider><QueryClientProvider client={queryClient}>
  <MemoryRouter initialEntries={[initialEntry]}><AppHeaderFrame room={previewScreen === 'chat'} controls={<><AttentionInbox/><QuickRoomSwitcher /><KeyboardHelp identity={`${useAuthStore.getState().user?.id || 0}:${useAuthStore.getState().epoch}`}/><AppMenu /></>}>
      {previewScreen === 'browser-demonstration' ? <main className="page-frame page-frame--narrow page-content"><BrowserDemonstration roomId={501} profile={{id:'b'.repeat(32),name:'합성 브라우저',account_label:'',agent_ids:[8],origin_urls:['https://example.invalid'],revision:1,controller:previewState==='demo-permission'?'disconnected':'user',agent_id:null,connected:previewState!=='demo-permission',last_used_at:null,expires_in_seconds:600}} agents={[{id:8,name:'기획'}]} readOnly={false}/></main> : <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/usage" element={<Usage />} />
        <Route path="/skills" element={<Skills />} />
        <Route path="/apps" element={<McpServers />} />
        <Route path="/memory-import" element={<MemoryImport />} />
        <Route path="/vault" element={<Vault />} />
        <Route path="/devices" element={<Devices />} />
        <Route path="/connections" element={<Connections />} />
        <Route path="/agents/:agentId/library" element={<BotLibrary />} />
        <Route path="/agents" element={previewScreen === 'bot-memory' ? <BotMemoryPreview /> : previewState.startsWith('bot-template-link-')&&previewState!=='bot-template-link-invalid' ? <TemplateLinkAgentsPreview /> : <Agents />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/chat/:conversationId" element={<Chat />} />
      </Routes>}

  </AppHeaderFrame></MemoryRouter>
</QueryClientProvider></I18nProvider></StrictMode>)

let incomingSequence = 72
return { element, addIncomingMessage: () => {
  if (previewState !== 'timeline') return
  incomingSequence += 1
  previewEmitRoomEvent({ ...event(incomingSequence, 'agent', 8, '기획', 'message', {
    content: '새 검토 결과가 도착했습니다.',
  }), created_at: new Date(Date.UTC(2026, 9, 2, 10, 0) + (incomingSequence - 1) * 60_000).toISOString() })
}, dispose: () => {if(browserFixtureAdapter && api.defaults.adapter===browserFixtureAdapter)api.defaults.adapter=priorBrowserAdapter;queryClient.clear()} }
}

function BotMemoryPreview() {
  const [open, setOpen] = useState(true)
  return <><Agents />{open && <AgentMemoryDialog agentId={8} name="기획" onClose={() => setOpen(false)} />}</>
}
