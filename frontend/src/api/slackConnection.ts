import { invoke } from '@tauri-apps/api/core'
import api from '../lib/api'

export type SlackPending = {version:1;owner_id:number;owner_epoch:string;core_epoch:string;account_id:string;expected_revision:number;operation_id:string;target_state:'stored'|'deleted'}
export type SlackStatus = {version:1;owner_id:number;owner_epoch:string;core_epoch:string;state:'missing'|'stored'|'deleted'|'uncertain';account_id:string|null;revision:number;operation_id:string|null;pending:SlackPending|null}
export type SlackChange = {ownerId:number;ownerEpoch:string;coreEpoch:string;accountId:string;expectedRevision:number;operationId:string}
export type SlackAdapter = {status:(ownerId:number)=>Promise<SlackStatus>;store:(input:SlackChange & {appToken:string;botToken:string})=>Promise<SlackStatus>;remove:(input:SlackChange)=>Promise<SlackStatus>}
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const epoch=/^[0-9a-f]{64}$/
export function validSlackStatus(v:SlackStatus,owner:number):boolean {
 return !!v && v.version===1 && v.owner_id===owner && epoch.test(v.owner_epoch) && epoch.test(v.core_epoch) && ['missing','stored','deleted','uncertain'].includes(v.state) && Number.isSafeInteger(v.revision) && v.revision>=0 && (v.account_id===null || uuid.test(v.account_id)) && (v.operation_id===null || uuid.test(v.operation_id)) && (v.state==='uncertain' ? !!v.pending && v.pending.version===1 && v.pending.owner_id===owner && epoch.test(v.pending.owner_epoch) && epoch.test(v.pending.core_epoch) && uuid.test(v.pending.account_id) && uuid.test(v.pending.operation_id) && Number.isSafeInteger(v.pending.expected_revision) && v.pending.expected_revision>=0 && ['stored','deleted'].includes(v.pending.target_state) : v.pending===null && (v.state==='missing' ? v.revision===0 && v.account_id===null && v.operation_id===null : v.revision>0 && v.account_id!==null && v.operation_id!==null))
}
export const slackAdapter:SlackAdapter={
 status:ownerId=>invoke('slack_credential_status',{ownerId}),
 store:input=>invoke('slack_credential_store',input),
 remove:input=>invoke('slack_credential_delete',input),
}

// Slack channel configuration carries identifiers only. Credentials use the Native adapter above.
export type SlackTarget={agent_id:number;agent_name:string;project_id:number;project_name:string;connection_id:number;connection_name:string;model_id:string}
export type SlackConfig={version:1;revision:number;state:'unconfigured'|'pending'|'configured';operation_id?:string;account_id?:string;credential_revision?:number;enabled?:boolean;workspace_id?:string;app_id?:string;channel_id?:string;sender_ids?:string[];agent_id?:number;project_id?:number;connection_id?:number;model_id?:string}
export type SlackConfigWrite={operation_id:string;expected_revision:number;expected_owner_epoch:string;expected_core_epoch:string;account_id:string;credential_revision:number;enabled:boolean;workspace_id:string;app_id:string;channel_id:string;sender_ids:string[];agent_id:number;project_id:number;connection_id:number;model_id:string}
export type SlackEvent={event_id:string;workspace_id:string;channel_id:string;sender_id:string;thread_ts:string;state:string;room_id:number|null;root_event_id:string|null;result_event_id:string|null;reply_state:string|null}
export type SlackReplyPending={run_id:string;event_id:string;workspace_id:string;channel_id:string;thread_ts:string;sender_id:string;state:'unknown'|'blocked'|'failed_before_dispatch'|'failed'|'cancelled'|'interrupted';attempt_id:string|null;config_revision:number;result_text:string|null}
export type SlackReplyDecision='owner_confirmed_sent'|'owner_confirmed_not_sent'|'owner_skip_no_reply'|'owner_hold_unknown'
export type SlackResolutionWrite={operation_id:string;attempt_id:string|null;expected_state:SlackReplyPending['state'];decision:SlackReplyDecision;expected_config_revision:number;expected_run_revision:number;expected_owner_epoch:string;expected_core_epoch:string}
export type SlackResolutionReceipt={version:1;run_id:string;operation_id:string;decision:SlackReplyDecision}
export type SlackFlowAdapter={targets:()=>Promise<SlackTarget[]>;config:()=>Promise<SlackConfig>;transport:()=>Promise<string>;events:()=>Promise<SlackEvent[]>;unresolved:(cursor?:string)=>Promise<{items:SlackReplyPending[];next_cursor:string|null}>;resolve:(id:string,input:SlackResolutionWrite)=>Promise<SlackResolutionReceipt>;resolution:(id:string,operation:string)=>Promise<SlackResolutionReceipt>;save:(input:SlackConfigWrite)=>Promise<SlackConfig>;reconcile:()=>Promise<SlackConfig>;command:(operationId:string)=>Promise<{operation_id:string;revision:number;state:'pending'|'configured'}>}

const validConfig=(v:SlackConfig):SlackConfig=>{if(!v||v.version!==1||!Number.isSafeInteger(v.revision)||v.revision<0||!['unconfigured','pending','configured'].includes(v.state)||v.state==='unconfigured'&&v.revision!==0||v.state!=='unconfigured'&&(!v.operation_id||!uuid.test(v.operation_id)||!v.account_id||!uuid.test(v.account_id)||!Number.isSafeInteger(v.credential_revision)||!v.credential_revision||!Array.isArray(v.sender_ids)))throw Error('Invalid Slack configuration');return v}
export const slackFlowAdapter:SlackFlowAdapter={
 targets:async()=>{const {data}=await api.get<{version:number;items:SlackTarget[]}>('/slack/targets');if(data.version!==1||!Array.isArray(data.items)||data.items.some(v=>!v||![v.agent_id,v.project_id,v.connection_id].every(n=>Number.isSafeInteger(n)&&n>0)||![v.agent_name,v.project_name,v.connection_name,v.model_id].every(n=>typeof n==='string'&&n.length>0)))throw Error('Invalid targets');return data.items},
 config:async()=>validConfig((await api.get<SlackConfig>('/slack/config')).data),
 transport:async()=>{const {data}=await api.get<{version:number;state:string}>('/slack/transport');if(data.version!==1||!['disabled','connecting','connected','unavailable','disconnected','unconfirmed','permission_required','identity_mismatch'].includes(data.state))throw Error('Invalid transport');return data.state},
 events:async()=>{const {data}=await api.get<{version:number;items:SlackEvent[]}>('/slack/events');if(data.version!==1||!Array.isArray(data.items)||data.items.length>50||data.items.some(v=>!v||![v.event_id,v.workspace_id,v.channel_id,v.sender_id,v.thread_ts,v.state].every(x=>typeof x==='string'&&x.length>0)||(v.reply_state!==null&&typeof v.reply_state!=='string')||(v.room_id!==null&&(!Number.isSafeInteger(v.room_id)||v.room_id<1))))throw Error('Invalid events');return data.items},
 unresolved:async(cursor)=>{const {data}=await api.get<{version:number;items:SlackReplyPending[];next_cursor:string|null}>('/slack/replies/unresolved',{params:cursor?{cursor}:undefined});if(data.version!==1||!Array.isArray(data.items)||data.items.length>50||(data.next_cursor!==null&&typeof data.next_cursor!=='string')||data.items.some(v=>!uuid.test(v.run_id)||!['unknown','blocked','failed_before_dispatch','failed','cancelled','interrupted'].includes(v.state)||(v.attempt_id!==null&&!uuid.test(v.attempt_id))||!Number.isSafeInteger(v.config_revision)))throw Error('Invalid unresolved replies');return data},
 resolve:async(id,input)=>{const {data}=await api.post<SlackResolutionReceipt>(`/slack/replies/${id}/resolution`,input);if(data.version!==1||data.run_id!==id||data.operation_id!==input.operation_id||data.decision!==input.decision)throw Error('Invalid receipt');return data},
 resolution:async(id,operation)=>{const {data}=await api.get<SlackResolutionReceipt>(`/slack/replies/${id}/resolution`,{params:{operation_id:operation}});if(data.version!==1||data.run_id!==id||data.operation_id!==operation)throw Error('Invalid receipt');return data},
 save:async input=>validConfig((await api.put<SlackConfig>('/slack/config',input)).data),
 reconcile:async()=>validConfig((await api.post<SlackConfig>('/slack/config/reconcile')).data),
 command:async operationId=>{const {data}=await api.get<{version:number;operation_id:string;revision:number;state:'pending'|'configured'}>(`/slack/commands/${operationId}`);if(data.version!==1||data.operation_id!==operationId||!Number.isSafeInteger(data.revision)||data.revision<1||!['pending','configured'].includes(data.state))throw Error('Invalid receipt');return data},
}
