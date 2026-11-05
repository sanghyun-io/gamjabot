import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useMemo, useState } from 'react'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { BotNotificationSettingsView, BotNotificationSettingsDisclosure, type BotNotificationAdapter } from '../src/components/BotNotificationSettings'
import type { NotificationSetting } from '../src/api/botNotifications'

type Scenario='ready'|'denied'|'unknown'|'permission-unknown'|'refresh-conflict'|'refresh-failure'|'missing-receipt'|'disclosure'
function Surface({scenario}:{scenario:Scenario}){
 const [ready,setReady]=useState(false),[identity,setIdentity]=useState('')
 useLayoutEffect(()=>{useAuthStore.getState().setAuth('synthetic-token',{id:1,email:'preview@example.invalid',username:'Preview'});setIdentity(`synthetic-${scenario}-${crypto.randomUUID()}`);setReady(true)},[scenario])
 const adapter=useMemo<BotNotificationAdapter>(()=>{let setting:NotificationSetting={agent_id:7,revision:1,completion:true,failure:false,approval:true};let savedOperation='';let reads=0,writes=0;return {status:async()=>({owner_id:1,owner_epoch:'a'.repeat(64)}),settings:async()=>{reads++;if(reads>1&&scenario==='refresh-failure')throw Error('synthetic refresh failure');if(reads===2&&scenario==='refresh-conflict')setting={...setting,revision:2,completion:false};return [setting]},write:async(_id,input)=>{writes++;if(scenario==='missing-receipt'&&writes===1)throw Error('synthetic lost before save');setting={agent_id:7,revision:input.expected_revision+1,completion:input.completion,failure:input.failure,approval:input.approval};savedOperation=input.operation_id;if(scenario==='unknown')throw Error('synthetic lost response');return setting},resolveCommand:async(agent,input)=>{if(savedOperation!==input.operation_id||agent!==7||setting.revision!==input.expected_revision+1)return 'pending';return 'saved'},native:{available:()=>true,status:async()=>scenario==='denied'?'denied':scenario==='permission-unknown'?'not_determined':'authorized',request:async()=>scenario==='permission-unknown'?'unknown':'denied'}}},[scenario])
 return <I18nProvider><main className="page-frame page-content stack"><p>합성 설정 · 실제 시스템 권한·알림 없음</p>{ready&&(scenario==='disclosure'?<BotNotificationSettingsDisclosure><BotNotificationSettingsView agentId={7} agentName="합성 Bot" ownerId={1} identity={identity} canUse={()=>true} adapter={adapter}/></BotNotificationSettingsDisclosure>:<BotNotificationSettingsView agentId={7} agentName="합성 Bot" ownerId={1} identity={identity} canUse={()=>true} adapter={adapter}/>)}</main></I18nProvider>
}
const meta={title:'Product/Bot local notifications',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const Ready:Story={args:{scenario:'ready'}}
export const Denied:Story={args:{scenario:'denied'}}
export const SaveUnknown:Story={args:{scenario:'unknown'}}
export const PermissionUnknown:Story={args:{scenario:'permission-unknown'}}

export const RefreshConflict:Story={args:{scenario:'refresh-conflict'}}
export const RefreshFailure:Story={args:{scenario:'refresh-failure'}}
export const MissingReceipt:Story={args:{scenario:'missing-receipt'}}
export const CloseAndReopen:Story={args:{scenario:'disclosure'}}
