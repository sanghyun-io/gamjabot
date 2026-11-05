import './networkGuard'
import '../src/index.css'
import { useRef, useState } from 'react'
import type { Meta, StoryObj } from '@storybook/react'
import { SharedBotApps, type SharedAppsAdapter, type SharedAppItem } from '../src/components/SharedBotApps'
import type { SharedAppsCommand, SharedAppsPointer, SharedAppsReceipt, SharedAppsScope } from '../src/api/sharedBotApps'
const account='11111111-1111-4111-8111-111111111111',team='22222222-2222-4222-8222-222222222222',pub='33333333-3333-4333-8333-333333333333',generation='44444444-4444-4444-8444-444444444444',server='55555555-5555-4555-8555-555555555555',link='66666666-6666-4666-8666-666666666666',request='77777777-7777-4777-8777-777777777777',root='88888888-8888-4888-8888-888888888888',action='99999999-9999-4999-8999-999999999999'
const scope:SharedAppsScope={owner_id:42,owner_epoch:1,profile_id:'synthetic',key_fingerprint:'synthetic',room_id:88,agent_id:9,issuer:'https://example.invalid',account_id:account,team_id:team,publication_id:pub,session_generation:generation}
const base:SharedAppItem={server_id:server,server_name:'합성 앱',server_revision:1,enabled:true,credential_configured:true,discovery_ready:true,provider_account_verified:false,assigned_to_current_bot:false,link:null}
type Scenario='unavailable'|'unlinked'|'pending'|'preference'|'unknown'|'changed'|'effect-unknown'|'all-ready'|'all-unknown'|'all-conflict'|'all-inflight'|'repair-active'|'repair-ready'|'repair-unknown'|'repair-stale'|'repair-discovered'|'repair-again'|'repair-unavailable'|'many-disabled'|'many-pending'
function fixture(scenario:Scenario):SharedAppsAdapter {
 const original='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
 let pendingPointers:SharedAppsPointer[]=scenario.startsWith('repair-')&&scenario!=='repair-discovered'?[{owner_id:scope.owner_id,issuer:scope.issuer,profile_id:scope.profile_id,key_fingerprint:scope.key_fingerprint,account_id:scope.account_id,team_id:scope.team_id,publication_id:scope.publication_id,operation_id:original,operation:'clear_all',expected_snapshot_digest:'a'.repeat(64)}]:[],last:SharedAppsCommand|null=null,lastClear:string|null=null,lastRecovery:string|null=null,recoverCount=0
 let item:SharedAppItem={...base}
 if(['pending','many-pending','preference','changed','effect-unknown','all-ready','all-unknown','all-conflict','all-inflight','repair-active','repair-ready','repair-unknown','repair-stale','repair-discovered','repair-again','repair-unavailable'].includes(scenario))item={...base,assigned_to_current_bot:true,link:{link_id:link,server_id:server,server_name:base.server_name,server_revision:1,link_revision:1,preferences_revision:1,state:scenario==='changed'?'uncertain':'active',provider_account_verified:false},preferences:(scenario==='preference'||scenario==='effect-unknown'||scenario.startsWith('all-')||scenario.startsWith('repair-'))?[{scope:'bot',state:'active',revision:1}]:[],pending:(scenario==='pending'||scenario==='many-pending')?[{request_id:request,root_event_id:root,action_id:action,room_id:88,agent_id:9,server_id:server,server_name:base.server_name,server_revision:1,link_id:link,link_revision:1,session_generation:generation,state:'pending',tool_name:'synthetic_read'}]:[]}
 const receipt=(op:SharedAppsCommand):SharedAppsReceipt=>({operation_id:op.operation_id,state:'committed',result:{operation:op.operation,server_id:op.server_id,...(op.request_id?{request:{request_id:op.request_id}}:{})}})
 return {store:{read:()=>[...pendingPointers],add:p=>{pendingPointers=[...pendingPointers,p]},remove:p=>{pendingPointers=pendingPointers.filter(x=>x.operation_id!==p.operation_id)},resolveRecovery:(old,newOp)=>{pendingPointers=pendingPointers.filter(x=>x.operation_id!==newOp.operation_id).map(x=>x.operation_id===old.operation_id||x.original_operation_id===old.operation_id?{...x,resolved_by_operation_id:newOp.operation_id}:x)}},confirm:()=>true,
  list:async()=>({items:scenario==='many-disabled'||scenario==='many-pending'?[item,...Array.from({length:30},(_,i)=>({...base,server_id:`disabled-${i}`,server_name:`합성 미연결 앱 ${i+1}`,enabled:false}))]:[item],shared_personal_apps_available:scenario!=='unavailable',owner_shared_apps_available:false}),
  command:async(_scope,op)=>{const needs=op.operation==='unlink'||op.operation==='clear'||op.operation==='decide'&&(op.choice==='bot'||op.choice==='all');if(needs&&op.expected_preference_revision!==item.link?.preferences_revision)throw Error('synthetic stale preference revision');if(!needs&&op.expected_preference_revision!==undefined)throw Error('synthetic unexpected preference revision');last=op;if(op.operation==='link')item={...base,assigned_to_current_bot:true,link:{link_id:link,server_id:server,server_name:base.server_name,server_revision:1,link_revision:1,preferences_revision:1,state:'active',provider_account_verified:false},preferences:[],pending:[]};if(op.operation==='decide'){const persistent=op.choice==='bot'||op.choice==='all';item={...item,pending:[],preferences:persistent?[...(item.preferences||[]),{scope:op.choice as 'bot'|'all',state:'active',revision:1}]:item.preferences,link:item.link&&persistent?{...item.link,preferences_revision:item.link.preferences_revision+1}:item.link}};if(op.operation==='clear')item={...item,preferences:[],link:item.link?{...item.link,preferences_revision:item.link.preferences_revision+1}:null};if(op.operation==='unlink')item={...base};if(scenario==='unknown')throw Error('synthetic response lost');const r=receipt(op);if(scenario==='effect-unknown'&&r.result)r.result.inflight_effects='may_be_uncertain';return r},
  clearAllPreview:async()=>({state:scenario==='repair-discovered'?'uncertain':'ready',snapshot_digest:'a'.repeat(64),recovery_operation_id:scenario==='repair-discovered'?original:null,links:[{link_id:link,server_id:server,server_name:'합성 앱',link_revision:1,preferences_revision:1,preference_count:1,pending_count:0,inflight_count:scenario==='all-inflight'?1:0}]}),
  clearAll:async(_scope,operationId,digest)=>{if(digest!=='a'.repeat(64))throw Error('synthetic bad digest');if(scenario==='all-conflict')throw {response:{status:409,data:{detail:{code:'shared_connector_snapshot_changed'}}}};lastClear=operationId;item={...item,preferences:[]};if(scenario==='all-unknown')throw Error('synthetic response lost');return {operation_id:operationId,state:'committed',result:{operation:'clear_all',cleared_connections:1,cleared_preferences:1,...(scenario==='all-inflight'?{inflight_effects:'may_be_uncertain'}:{})}}},
  recoveryPreview:async(_scope,originalId)=>({state:scenario==='repair-active'?'active':scenario==='repair-unavailable'?'unavailable':'recoverable',reason:scenario==='repair-active'?'handler_live':scenario==='repair-unavailable'?'fence_unavailable':'orphan_fenced',original_operation_id:originalId,old_status:'unknown',snapshot_digest:'b'.repeat(64),links:[{link_id:link,server_id:server,server_name:'합성 앱',link_revision:1,preferences_revision:1,preference_count:1,pending_count:0,inflight_count:1}]}),
  recover:async(_scope,operationId,originalId,digest)=>{if(digest!=='b'.repeat(64)||originalId!==original||operationId===original)throw Error('synthetic invalid scope');recoverCount++;if(scenario==='repair-stale')throw {response:{status:409,data:{detail:{code:'shared_connector_snapshot_changed'}}}};lastRecovery=operationId;if(scenario==='repair-unknown'||scenario==='repair-again'&&recoverCount===1)throw Error('synthetic response lost');item={...item,preferences:[],link:item.link?{...item.link,preferences_revision:item.link.preferences_revision+1}:null};return {operation_id:operationId,state:'committed',result:{operation:'clear_all_recovery',original_operation_id:original,old_effect_unknown:true,cleared_connections:1,cleared_preferences:1,inflight_effects:'may_be_uncertain'}}},
  receipt:async(_scope,p)=>{if(p.operation==='clear_all_recovery'&&lastRecovery===p.operation_id&&!(scenario==='repair-again'&&recoverCount===1))item={...item,preferences:[],link:item.link?{...item.link,preferences_revision:item.link.preferences_revision+1}:null};return p.operation==='clear_all_recovery'&&lastRecovery===p.operation_id?(scenario==='repair-again'&&recoverCount===1?{operation_id:p.operation_id,state:'uncertain'}:{operation_id:p.operation_id,state:'committed',result:{operation:'clear_all_recovery',original_operation_id:original,old_effect_unknown:true,cleared_connections:1,cleared_preferences:1,inflight_effects:'may_be_uncertain'}}):p.operation==='clear_all'&&p.operation_id===original?{operation_id:p.operation_id,state:scenario==='repair-active'?'pending':'uncertain'}:p.operation==='clear_all'&&lastClear===p.operation_id?{operation_id:p.operation_id,state:'committed',result:{operation:'clear_all',cleared_connections:1,cleared_preferences:1}}:last&&last.operation_id===p.operation_id?receipt(last):{operation_id:p.operation_id,state:'uncertain'}},
 }
}
function Example({scenario}:{scenario:Scenario}){const adapter=useRef(fixture(scenario)).current,[mount,setMount]=useState(0);return <div className="page-frame page-content stack"><button type="button" className="button button--secondary" onClick={()=>setMount(x=>x+1)}>합성 화면 다시 열기</button><SharedBotApps key={mount} scope={scope} adapter={adapter} ko onManage={()=>{}}/></div>}
const meta:Meta<typeof Example>={title:'Shared Bot/Personal apps',component:Example}
export default meta
type Story=StoryObj<typeof Example>
export const Unavailable:Story={args:{scenario:'unavailable'}}
export const Unlinked:Story={args:{scenario:'unlinked'}}
export const AccountUseRequest:Story={args:{scenario:'pending'}}
export const SavedPreference:Story={args:{scenario:'preference'}}
export const UnknownAndReadback:Story={args:{scenario:'unknown'}}
export const ChangedOrUncertain:Story={args:{scenario:'changed'}}
function AccountSwitchExample(){
 const next={...scope,account_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',session_generation:'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'}
 const [selected,setSelected]=useState(scope),resolveOld=useRef<(()=>void)|null>(null)
 const adapter=useRef<SharedAppsAdapter>({store:{read:()=>[],add:()=>{},remove:()=>{},resolveRecovery:()=>{}},confirm:()=>true,
  list:async current=>{if(current.account_id===scope.account_id)return new Promise(resolve=>{resolveOld.current=()=>resolve({items:[{...base,server_name:'이전 계정 합성 앱'}],shared_personal_apps_available:true,owner_shared_apps_available:false})});return {items:[{...base,server_name:'새 계정 합성 앱'}],shared_personal_apps_available:true,owner_shared_apps_available:false}},
  command:async()=>{throw Error('synthetic unavailable')},receipt:async()=>{throw Error('synthetic unavailable')},clearAllPreview:async()=>{throw Error('synthetic unavailable')},clearAll:async()=>{throw Error('synthetic unavailable')},recoveryPreview:async()=>{throw Error('synthetic unavailable')},recover:async()=>{throw Error('synthetic unavailable')}}).current
 return <div className="page-frame page-content stack"><div className="page-actions"><button type="button" className="button button--secondary" onClick={()=>setSelected(next)}>합성 계정 변경</button><button type="button" className="button button--secondary" onClick={()=>resolveOld.current?.()}>이전 합성 응답 완료</button></div><SharedBotApps key={selected.account_id} scope={selected} adapter={adapter} ko onManage={()=>{}}/></div>
}
export const AccountSwitchLateResponse:Story={render:()=> <AccountSwitchExample/>}
export const AlreadyStartedActionUnknown:Story={args:{scenario:'effect-unknown'}}
export const ClearAll:Story={args:{scenario:'all-ready'}}
export const ClearAllUnknown:Story={args:{scenario:'all-unknown'}}
export const ClearAllChanged:Story={args:{scenario:'all-conflict'}}
export const ClearAllInflightUnknown:Story={args:{scenario:'all-inflight'}}
export const RecoveryActive:Story={args:{scenario:'repair-active'}}
export const RecoveryReady:Story={args:{scenario:'repair-ready'}}
export const RecoveryUnknown:Story={args:{scenario:'repair-unknown'}}
export const RecoveryChanged:Story={args:{scenario:'repair-stale'}}
export const RecoveryDiscovered:Story={args:{scenario:'repair-discovered'}}
export const RecoveryAnotherAttempt:Story={args:{scenario:'repair-again'}}
export const RecoveryUnavailable:Story={args:{scenario:'repair-unavailable'}}
function RecoveryDifferentAccountExample(){
 const adapter=useRef(fixture('repair-ready')).current
 const other={...scope,account_id:'cccccccc-cccc-4ccc-8ccc-cccccccccccc',session_generation:'dddddddd-dddd-4ddd-8ddd-dddddddddddd'}
 const [selected,setSelected]=useState(scope)
 return <div className="page-frame page-content stack"><button type="button" className="button button--secondary" onClick={()=>setSelected(other)}>합성 계정 변경</button><SharedBotApps key={selected.account_id} scope={selected} adapter={adapter} ko onManage={()=>{}}/></div>
}
export const RecoveryDifferentAccount:Story={render:()=> <RecoveryDifferentAccountExample/>}

function HierarchyExample({scenario}:{scenario:'many-disabled'|'many-pending'}) {const adapter=useRef(fixture(scenario)).current;return <div className="page-frame page-content stack"><SharedBotApps scope={scope} adapter={adapter} ko onManage={()=>{}}/><article className="surface"><strong>합성 대화 결과</strong><p>대화가 개인 앱 설정 아래에서 보입니다.</p></article></div>}
export const ConversationManyApps:Story={render:()=> <HierarchyExample scenario="many-disabled"/>}
export const ConversationPendingRequest:Story={render:()=> <HierarchyExample scenario="many-pending"/>}
