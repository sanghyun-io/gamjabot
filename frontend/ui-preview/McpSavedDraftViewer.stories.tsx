import type { Meta, StoryObj } from '@storybook/react-vite'
import { useLayoutEffect, useState } from 'react'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { mcpDraftApi } from '../src/api/mcpDraft'
import ToolActivityRecord from '../src/components/ToolActivityRecord'
import type { ToolActivity } from '../src/api/tools'

const action: ToolActivity = { action_id:'b2000000-0000-4000-8000-000000000012',room_id:501,agent_id:8,trigger_event_id:'b2000000-0000-4000-8000-000000000011',family:'mcp_tool_call',target:'Synthetic mail',expected_effect:'Synthetic mail action',state:'cancelled',scope:null,grant_id:null,requested_at:'2026-10-31T00:00:00Z',started_at:null,completed_at:null,owner_draft:{revision:1,pending:true,applied:false} }
function Surface() {
  const [ready,setReady]=useState(false)
  useLayoutEffect(()=>{
    useAuthStore.getState().setAuth('synthetic-preview-token',{id:1,email:'preview@example.invalid',username:'Preview'})
    const original=mcpDraftApi.read
    mcpDraftApi.read=async()=>({version:1,action_id:action.action_id,review_id:null,revision:1,fields:[],values:{},edits:{'/to':['team@example.invalid'],'/body':'저장된 합성 메시지'},pending:true,available:false})
    setReady(true)
    return ()=>{mcpDraftApi.read=original}
  },[])
  return ready ? <I18nProvider><main className="page-frame page-content"><ToolActivityRecord action={action} agentName="합성 Bot" /></main></I18nProvider> : null
}
const meta={title:'Screens/저장된 MCP 편집',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const TerminalSaved:Story={name:'이전 작업 종료 · 저장된 편집 확인 (합성)'}
