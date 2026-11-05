// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SlackPendingReplies } from './SlackConnection'
import { useState } from 'react'
import type { SlackFlowAdapter, SlackStatus, SlackConfig } from '../api/slackConnection'

afterEach(cleanup)
const status={state:'stored',owner_epoch:'a'.repeat(64),core_epoch:'b'.repeat(64)} as SlackStatus
const config={version:1,state:'configured',revision:1} as SlackConfig
const row={run_id:'00000000-0000-4000-8000-000000000003',event_id:'Ev1',workspace_id:'T1',channel_id:'C1',sender_id:'U1',thread_ts:'123.456789',state:'unknown',attempt_id:'00000000-0000-4000-8000-000000000004',config_revision:1,result_text:'expected reply'} as const

for (const code of [409,422]) it(`clears pending after definite HTTP ${code} rejection`,async()=>{
 const adapter={unresolved:vi.fn(async()=>({items:[row],next_cursor:null})),resolve:vi.fn(async()=>{throw {response:{status:code}}}),resolution:vi.fn()} as unknown as SlackFlowAdapter
 render(<SlackPendingReplies status={status} config={config} identity={`rejected-${code}`} canUse={()=>true} adapter={adapter} ko={false}/> )
 fireEvent.click(await screen.findByRole('button',{name:'I found the reply in Slack'}))
 fireEvent.click(screen.getByRole('button',{name:'Record confirmation'}))
 await screen.findByText('The state changed and no confirmation was recorded. Check the latest list.')
 await waitFor(()=>expect(screen.getByRole('button',{name:'I found the reply in Slack'})).toBeTruthy())
 expect(screen.queryByRole('button',{name:'Check confirmation record'})).toBeNull()
 expect(adapter.unresolved).toHaveBeenCalledTimes(2)
})

it('keeps the original receipt check for a network-unknown result',async()=>{
 const adapter={unresolved:vi.fn(async()=>({items:[row],next_cursor:null})),resolve:vi.fn(async()=>{throw Error('network')}),resolution:vi.fn(async()=>{throw Error('unknown')})} as unknown as SlackFlowAdapter
 render(<SlackPendingReplies status={status} config={config} identity="network-unknown" canUse={()=>true} adapter={adapter} ko={false}/> )
 fireEvent.click(await screen.findByRole('button',{name:'I found the reply in Slack'}))
 fireEvent.click(screen.getByRole('button',{name:'Record confirmation'}))
 fireEvent.click(await screen.findByRole('button',{name:'Check confirmation record'}))
 await waitFor(()=>expect(adapter.resolution).toHaveBeenCalledTimes(1))
 expect(adapter.resolve).toHaveBeenCalledTimes(1)
 expect(screen.queryByRole('button',{name:'I found the reply in Slack'})).toBeNull()
})

it('refreshes scope after a revision conflict before another confirmation',async()=>{
 const resolve=vi.fn().mockRejectedValueOnce({response:{status:409}}).mockImplementation(async(id,input)=>({version:1,run_id:id,operation_id:input.operation_id,decision:input.decision}))
 const adapter={unresolved:vi.fn(async()=>({items:[row],next_cursor:null})),resolve,resolution:vi.fn()} as unknown as SlackFlowAdapter
 function Harness(){const [revision,setRevision]=useState(1);return <SlackPendingReplies status={status} config={{...config,revision}} identity="revision-change" canUse={()=>true} adapter={adapter} onRefreshScope={async()=>setRevision(2)} ko={false}/>}
 render(<Harness/>)
 fireEvent.click(await screen.findByRole('button',{name:'I found the reply in Slack'}));fireEvent.click(screen.getByRole('button',{name:'Record confirmation'}))
 await screen.findByText('The state changed and no confirmation was recorded. Check the latest list.')
 await waitFor(()=>expect(screen.getByRole('button',{name:'I found the reply in Slack'})).toBeTruthy())
 fireEvent.click(screen.getByRole('button',{name:'I found the reply in Slack'}));fireEvent.click(screen.getByRole('button',{name:'Record confirmation'}))
 await waitFor(()=>expect(resolve).toHaveBeenCalledTimes(2))
 expect(resolve.mock.calls[0][1].expected_config_revision).toBe(1)
 expect(resolve.mock.calls[1][1].expected_config_revision).toBe(2)
})
