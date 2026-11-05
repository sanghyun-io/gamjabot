// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import api from './api'
import { eventsApi } from '../api/events'
import { toolsApi, type ToolActivity, type ActionPreview, type ExecutionBinding } from '../api/tools'
import { scopeStopsApi, type ScopeStopReceipt } from '../api/scopeStops'
import { actionPreviewMatches, executionContext, routineMcpRootMatches, toolActionIdentity, toolApprovalAvailable } from './toolExecution'
import GrantPrompt from '../components/GrantPrompt'
import { useAuthStore } from '../stores/authStore'
import { I18nProvider } from '../i18n'
vi.mock('./api', () => ({ default: { get: vi.fn(), post: vi.fn() } }))
const id = (n: number) => `af034000-0000-4000-8000-${String(n).padStart(12, '0')}`
const root = { room_id: 501, event_id: id(1), seq: 1, actor_type: 'user' as const, actor_id: 1, kind: 'message' as const }
const binding = (): ExecutionBinding => ({ version: 1, root_scope_id: id(21), scope_id: id(23), owner_id: 1, root_room_id: 501,
  root_request_event_id: root.event_id, execution_room_id: 701, agent_id: 9, run_generation: 3, job_id: id(40), phase: 'response', context_mode: 'delegated',
  deadline_at: '2099-10-29T01:30:00Z', lineage: [21,22,23].map(n => ({ scope_id: id(n), epoch: 0 })) })
const action = (): ToolActivity => ({ action_id: id(10), room_id: 701, agent_id: 9, trigger_event_id: root.event_id, family: 'keyboard_text', target: 'desktop:keyboard',
  expected_effect: 'Type 5 characters', state: 'waiting', scope: null, grant_id: null, requested_at: '2026-10-29T01:00:00Z', started_at: null, completed_at: null,
  review_id: id(100), execution_binding: binding(), root_ref: root, scope_status: 'active', revocation_state: null })
const preview = (a = action()): ActionPreview => ({ action_id: a.action_id, family: a.family, target: a.target, expected_effect: a.expected_effect,
  scope_target: a.target, scope_description: 'One reviewed action', workflow_id: null, arguments: { text: 'Hello' }, payload_digest: 'e'.repeat(64), review_id: a.review_id!,
  execution_binding: structuredClone(a.execution_binding), root_ref: a.root_ref, scope_status: a.scope_status, revocation_state: a.revocation_state,
  approval_policy: { scopes: ['once'], unverified_context_required: true, start_within_seconds: 60 },
  review: { version: 2, captured_at: '2026-10-29T01:00:00Z', action: { action_id: a.action_id, room_id: a.room_id, agent_id: a.agent_id, trigger_event_id: a.trigger_event_id,
    family: a.family, target: a.target, scope_target: a.target, expected_effect: a.expected_effect, payload_digest: 'e'.repeat(64), execution_binding: structuredClone(a.execution_binding) },
    authority: { owner_id: 1, room_id: a.room_id, agent_id: a.agent_id, proposer: { owner_id: 1, room_id: a.room_id, agent_id: a.agent_id, connection_id: 1, provider_id: 'synthetic', model_id: 'synthetic' } },
    targets: [], external_context: { account: 'unsupported', target_version: 'unsupported' } } })
beforeEach(() => { vi.resetAllMocks(); window.localStorage.removeItem('gamja.locale'); useAuthStore.getState().setAuth('synthetic', { id: 1, email: 'owner@example.invalid', username: 'Owner' }) })
afterEach(() => { cleanup(); vi.restoreAllMocks() })
it('preserves legacy and direct-room policy while delegated reviews allow only fresh once approval', () => {
  const legacy = { ...action(), execution_binding: null, root_ref: null, scope_status: null, revocation_state: null }
  const old = { ...preview(), execution_binding: null, root_ref: null, scope_status: null, revocation_state: null, review: undefined,
    approval_policy: { scopes: ['once', 'room_session'] as ('once'|'room_session')[], unverified_context_required: false, start_within_seconds: null } }
  expect(actionPreviewMatches(legacy, old, 1)).toBe(true)
  const direct = action(); direct.execution_binding = { ...binding(), context_mode: 'user_room', job_id: null, phase: null }
  const directReview = preview(direct); directReview.approval_policy.scopes = ['once', 'room_session']
  expect(actionPreviewMatches(direct, directReview, 1)).toBe(true)
  const delegated = preview(); delegated.approval_policy.scopes = ['once', 'room_session']
  expect(actionPreviewMatches(action(), delegated, 1)).toBe(false)
  const stopped = action(); stopped.revocation_state = 'unknown'
  expect(toolApprovalAvailable(executionContext(stopped, 1))).toBe(false)
})
it('rejects a changed original root, actual actor, lineage epoch or review target', () => {
  const original = action(), seen = preview(original)
  for (const changed of [{ ...original, agent_id: 12 }, { ...original, root_ref: { ...root, event_id: id(2) } },
    { ...original, execution_binding: { ...binding(), lineage: binding().lineage.map(item => ({ ...item, epoch: 1 })) } }]) {
    expect(actionPreviewMatches(changed, seen, 1)).toBe(false); expect(toolActionIdentity(changed)).not.toBe(toolActionIdentity(original))
  }
  expect(actionPreviewMatches(original, { ...seen, target: '/different-target' }, 1)).toBe(false)
})
it('projects actual nested execution rooms for one Owner root and rejects a different root', async () => {
  const a = action(), c = { ...action(), action_id: id(11), room_id: 702, agent_id: 12,
    execution_binding: { ...binding(), execution_room_id: 702, agent_id: 12, scope_id: id(24), lineage: [...binding().lineage, { scope_id: id(24), epoch: 0 }] } }
  vi.mocked(api.get).mockResolvedValue({ data: { version: 1, message_id: id(40), root_ref: root, actions: [a,c] } })
  expect((await toolsApi.rootActions(id(40), root)).actions.map(row => row.room_id)).toEqual([701,702])
  vi.mocked(api.get).mockResolvedValue({ data: { version: 1, message_id: id(40), root_ref: { ...root, room_id: 502 }, actions: [a] } })
  await expect(toolsApi.rootActions(id(40), root)).rejects.toThrow('unavailable')
})
it('reconfirms only the frozen stop receipt without canceling the room or adopting new targets', async () => {
  const stopped: ScopeStopReceipt = { version: 1, stop_id: id(300), room_id: 501, created_at: '2026-10-29T01:00:00Z', state: 'unknown',
    revocations: [{ scope_id: id(21), epoch: 1 }], receipts: [{ scope_id: id(21), epoch: 1, state: 'unknown' }] }
  vi.mocked(api.post).mockResolvedValueOnce({ data: { ...stopped, state: 'settled', receipts: [{ scope_id: id(21), epoch: 1, state: 'settled' }] } })
  expect((await scopeStopsApi.confirm(stopped)).state).toBe('settled')
  expect(api.post).toHaveBeenCalledWith(`/bots/scope-stops/${id(300)}/confirm`)
  vi.mocked(api.post).mockResolvedValueOnce({ data: { ...stopped, revocations: [{ scope_id: id(91), epoch: 1 }], receipts: [{ scope_id: id(91), epoch: 1, state: 'unknown' }] } })
  await expect(scopeStopsApi.confirm(stopped)).rejects.toThrow('unavailable')
  expect(vi.mocked(api.post).mock.calls.every(call => call.length === 1 && !String(call[0]).includes('/cancel'))).toBe(true)
})
it('discards the prior direct-confirmation checkbox when the scope changes', async () => {
  const initial = action(), read = vi.spyOn(toolsApi, 'preview').mockResolvedValue(preview(initial)), refresh = vi.fn()
  const view = (a: ToolActivity) => <I18nProvider systemLanguages={() => ['en']}><MemoryRouter><GrantPrompt roomId={701} roomName="Bea DM" agentName="Bea" action={a} onRefresh={refresh} onStop={vi.fn()} /></MemoryRouter></I18nProvider>
  const rendered = render(view(initial))
  fireEvent.click(await screen.findByRole('checkbox'))
  expect((screen.getByRole('button', { name: /once for Bea/i }) as HTMLButtonElement).disabled).toBe(false)
  const changed = { ...initial, execution_binding: { ...binding(), lineage: binding().lineage.map(item => ({ ...item, epoch: 1 })) } }
  rendered.rerender(view(changed))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('no longer valid'))
  expect(screen.queryByRole('checkbox')).toBeNull()
  const fresh = { ...changed, action_id: id(12), review_id: id(102) }
  read.mockResolvedValue(preview(fresh)); rendered.rerender(view(fresh))
  expect((await screen.findByRole('checkbox') as HTMLInputElement).checked).toBe(false)
  expect((screen.getByRole('button', { name: /once for Bea/i }) as HTMLButtonElement).disabled).toBe(true)
})

it('blocks an unbound activity row against a valid delegated process preview and accepts the full server-derived activity proof', async () => {
  const bound = { ...action(), family: 'start_process', target: '/bin/sleep [invocation sha256:1936d18df9226bce]',
    expected_effect: 'Run process with local user authority; 1 arguments; owned background lease up to 180 seconds after start',
    execution_binding: { ...binding(), run_generation: 1, deadline_at: '2099-10-29T01:30:00.143527+00:00' } }
  const seen = preview(bound)
  seen.scope_target = '/bin/sleep'; seen.arguments = { executable: '/bin/sleep', argv: ['173'], cwd: '/synthetic/consent-review', background_seconds: 180 }
  seen.approval_policy = { scopes: ['once'], unverified_context_required: false, start_within_seconds: null }
  const review = seen.review as { action: { scope_target: string }; targets: unknown[] }
  review.action.scope_target = '/bin/sleep'; review.targets = [{ role: 'executable', path: '/bin/sleep', status: 'verified' }, { role: 'cwd', path: '/synthetic/consent-review', status: 'verified' }]
  const unbound = { ...bound, execution_binding: undefined, root_ref: undefined, scope_status: undefined, revocation_state: undefined }
  expect(actionPreviewMatches(unbound, seen, 1)).toBe(false); expect(actionPreviewMatches(bound, seen, 1)).toBe(true)
  vi.spyOn(toolsApi, 'preview').mockResolvedValue(seen)
  const view = (value: ToolActivity) => <I18nProvider systemLanguages={() => ['en']}><MemoryRouter><GrantPrompt roomId={701} roomName="Bea DM" agentName="Bea" action={value} onRefresh={vi.fn()} onStop={vi.fn()} /></MemoryRouter></I18nProvider>
  const rendered = render(view(unbound))
  expect((await screen.findByRole('alert')).textContent).toContain('no longer valid')
  expect(screen.queryByRole('button', { name: /once for Bea/i })).toBeNull()
  rendered.rerender(view(bound))
  expect((await screen.findByRole('button', { name: /once for Bea/i }) as HTMLButtonElement).disabled).toBe(false)
  expect(api.post).not.toHaveBeenCalled()
})
it('checks typed routine binding and rejects changed or revoked proof', () => {
  const proof={version:1 as const,owner_id:1,agent_id:9,room_id:701,routine_id:id(60),run_id:id(61),config_revision:2,generation:3,root_event_id:id(62),origin:'test' as const,scheduled_at:'2026-10-29T01:00:00Z'}
  const a={...action(),trigger_event_id:proof.root_event_id,execution_binding:null,routine_binding:proof,root_ref:{room_id:701,event_id:proof.root_event_id,seq:3,actor_type:'system' as const,actor_id:null,kind:'activity' as const},scope_status:'active' as const,revocation_state:null}
  const base=preview(a), review=base.review as {version:number;action:Record<string,unknown>}; review.version=1;review.action.execution_binding=null
  const p={...base,routine_binding:proof,review}
  expect(actionPreviewMatches(a,p,1)).toBe(true)
  expect(actionPreviewMatches(a,{...p,routine_binding:{...proof,run_id:id(63)}},1)).toBe(false)
  expect(actionPreviewMatches({...a,routine_binding:{...proof,owner_id:2}},p,1)).toBe(false)
  expect(actionPreviewMatches({...a,root_ref:{...a.root_ref,actor_type:'user',actor_id:1,kind:'message'}},p,1)).toBe(false)
  expect(toolApprovalAvailable(executionContext({...a,scope_status:'revoked'},1))).toBe(false)
  expect(executionContext({...a,routine_binding:null},1).kind).toBe('invalid')
})
it('renders a routine review with an exact typed proof',async()=>{
 const proof={version:1 as const,owner_id:1,agent_id:9,room_id:701,routine_id:id(60),run_id:id(61),config_revision:2,generation:3,root_event_id:id(62),origin:'test' as const,scheduled_at:'2026-10-29T01:00:00Z'}
 const a={...action(),trigger_event_id:proof.root_event_id,execution_binding:null,routine_binding:proof,root_ref:{room_id:701,event_id:proof.root_event_id,seq:3,actor_type:'system' as const,actor_id:null,kind:'activity' as const},scope_status:'active' as const,revocation_state:null}
 const base=preview(a),review=base.review as {version:number;action:Record<string,unknown>};review.version=1;review.action.execution_binding=null
 vi.spyOn(toolsApi,'preview').mockResolvedValue({...base,routine_binding:proof,review})
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><GrantPrompt roomId={701} roomName="Test" agentName="Bot" action={a} onRefresh={vi.fn()} onStop={vi.fn()} /></MemoryRouter></I18nProvider>)
 expect(await screen.findByRole('button',{name:/once for Bot/i})).toBeTruthy()
})
it('requires exact MCP binding, Owner origin, redacted preview and once-only policy',()=>{
 const base=action(),mcp={server_id:id(70),config_revision:1,binding_revision:1,tool_id:'a'.repeat(64),tool_name:'read_document',schema_sha256:'b'.repeat(64),owner_id:1,agent_id:9,root_event_id:root.event_id,generation:3,transport_sha256:'c'.repeat(64),input_sha256:'d'.repeat(64),transport_type:'http' as const}
 const a={...base,room_id:501,execution_binding:{...binding(),execution_room_id:501,context_mode:'user_room' as const,job_id:null,phase:null},family:'mcp_tool_call',mcp_binding:mcp},p=preview(a),review=p.review as {action:Record<string,unknown>};review.action.family='mcp_tool_call';review.action.mcp_binding=mcp
 const good={...p,family:'mcp_tool_call',mcp_binding:mcp,review}
 expect(actionPreviewMatches(a,good,1)).toBe(true)
 expect(actionPreviewMatches(a,{...good,mcp_binding:{...mcp,binding_revision:2}},1)).toBe(false)
 expect(actionPreviewMatches(a,{...good,approval_policy:{...good.approval_policy,scopes:['once','room_session']}},1)).toBe(false)
 expect(actionPreviewMatches({...a,mcp_binding:{...mcp,owner_id:2}},good,1)).toBe(false)
})
it('requires actual Owner root before allowing an MCP action',async()=>{
 const mcp={server_id:id(70),config_revision:1,binding_revision:1,tool_id:'a'.repeat(64),tool_name:'read_document',schema_sha256:'b'.repeat(64),owner_id:1,agent_id:9,root_event_id:root.event_id,generation:3,transport_sha256:'c'.repeat(64),input_sha256:'d'.repeat(64),transport_type:'http' as const}
 const a={...action(),room_id:501,execution_binding:{...binding(),execution_room_id:501,context_mode:'user_room' as const,job_id:null,phase:null},family:'mcp_tool_call',mcp_binding:mcp},p=preview(a),review=p.review as {action:Record<string,unknown>};review.action.family='mcp_tool_call';review.action.mcp_binding=mcp
 vi.spyOn(toolsApi,'preview').mockResolvedValue({...p,family:'mcp_tool_call',mcp_binding:mcp,review})
 vi.spyOn(eventsApi,'list').mockResolvedValue([{...root,device_id:null,actor_name_snapshot:'Owner',payload:{content:'Check the document'},created_at:'2026-10-29T01:00:00Z',idempotency_key:null}])
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><GrantPrompt roomId={501} roomName="Test" agentName="Bot" action={a} onRefresh={vi.fn()} onStop={vi.fn()} /></MemoryRouter></I18nProvider>)
 fireEvent.click(await screen.findByRole('checkbox'))
 const button=await screen.findByRole('button',{name:/once for Bot/i})
 await vi.waitFor(()=>expect((button as HTMLButtonElement).disabled).toBe(false))
})
it('does not approve an MCP action whose claimed Owner root is a system event',async()=>{
 const mcp={server_id:id(70),config_revision:1,binding_revision:1,tool_id:'a'.repeat(64),tool_name:'read_document',schema_sha256:'b'.repeat(64),owner_id:1,agent_id:9,root_event_id:root.event_id,generation:3,transport_sha256:'c'.repeat(64),input_sha256:'d'.repeat(64),transport_type:'http' as const}
 const a={...action(),room_id:501,execution_binding:{...binding(),execution_room_id:501,context_mode:'user_room' as const,job_id:null,phase:null},family:'mcp_tool_call',mcp_binding:mcp},p=preview(a),review=p.review as {action:Record<string,unknown>};review.action.family='mcp_tool_call';review.action.mcp_binding=mcp
 vi.spyOn(toolsApi,'preview').mockResolvedValue({...p,family:'mcp_tool_call',mcp_binding:mcp,review})
 vi.spyOn(eventsApi,'list').mockResolvedValue([{...root,actor_type:'system',actor_id:null,kind:'activity',device_id:null,actor_name_snapshot:'System',payload:{},created_at:'2026-10-29T01:00:00Z',idempotency_key:null}])
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><GrantPrompt roomId={501} roomName="Test" agentName="Bot" action={a} onRefresh={vi.fn()} onStop={vi.fn()} /></MemoryRouter></I18nProvider>)
 fireEvent.click(await screen.findByRole('checkbox'))
 expect((await screen.findByRole('button',{name:/once for Bot/i}) as HTMLButtonElement).disabled).toBe(true)
})

it('delegated MCP matches both proofs and rejects missing or changed teammate and cross-room scope',()=>{
 const mcp={server_id:id(70),config_revision:1,binding_revision:1,tool_id:'a'.repeat(64),tool_name:'read_document',schema_sha256:'b'.repeat(64),owner_id:1,agent_id:9,root_event_id:root.event_id,generation:3,transport_sha256:'c'.repeat(64),input_sha256:'d'.repeat(64),transport_type:'http' as const}
 const a={...action(),family:'mcp_tool_call',mcp_binding:mcp},p=preview(a),review=p.review as {action:Record<string,unknown>};review.action.mcp_binding=mcp
 const good={...p,mcp_binding:mcp,review}
 expect(actionPreviewMatches(a,good,1)).toBe(true)
 expect(actionPreviewMatches(a,{...good,execution_binding:{...binding(),job_id:id(41)}},1)).toBe(false)
 expect(actionPreviewMatches(a,{...good,root_ref:{...root,seq:2}},1)).toBe(false)
 const teammate={handoff_id:id(80),phase:'response' as const},same={...a,room_id:501,execution_binding:{...binding(),execution_room_id:501,context_mode:'user_room' as const,job_id:null,phase:null},mcp_binding:{...mcp,teammate_binding:teammate}}
 const samePreview=preview(same),sameReview=samePreview.review as {action:Record<string,unknown>};sameReview.action.mcp_binding=same.mcp_binding
 const seen={...samePreview,mcp_binding:same.mcp_binding,review:sameReview}
 expect(actionPreviewMatches(same,seen,1)).toBe(true)
 expect(actionPreviewMatches(same,{...seen,mcp_binding:mcp},1)).toBe(false)
 expect(actionPreviewMatches({...same,execution_binding:null,root_ref:null,scope_status:null},seen,1)).toBe(false)
 expect(actionPreviewMatches(same,{...seen,review:{...sameReview,action:{...sameReview.action,mcp_binding:mcp}}},1)).toBe(false)
 expect(toolApprovalAvailable(executionContext({...same,scope_status:'revoked'},1))).toBe(false)
})
it('delegated MCP reads the original room root before once approval',async()=>{
 const mcp={server_id:id(70),config_revision:1,binding_revision:1,tool_id:'a'.repeat(64),tool_name:'read_document',schema_sha256:'b'.repeat(64),owner_id:1,agent_id:9,root_event_id:root.event_id,generation:3,transport_sha256:'c'.repeat(64),input_sha256:'d'.repeat(64),transport_type:'http' as const}
 const a={...action(),family:'mcp_tool_call',mcp_binding:mcp},p=preview(a),review=p.review as {action:Record<string,unknown>};review.action.mcp_binding=mcp
 vi.spyOn(toolsApi,'preview').mockResolvedValue({...p,mcp_binding:mcp,review})
 const events=vi.spyOn(eventsApi,'list').mockResolvedValue([{...root,device_id:null,actor_name_snapshot:'Owner',payload:{content:'Check'},created_at:'2026-10-29T01:00:00Z',idempotency_key:null}])
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><GrantPrompt roomId={701} roomName="Bea DM" agentName="Bea" action={a} onRefresh={vi.fn()} onStop={vi.fn()} /></MemoryRouter></I18nProvider>)
 fireEvent.click(await screen.findByRole('checkbox'))
 const button=await screen.findByRole('button',{name:/once for Bea/i})
 await vi.waitFor(()=>expect((button as HTMLButtonElement).disabled).toBe(false))
 expect(events).toHaveBeenCalledWith(501,0,100)
 expect(screen.getByRole('link',{name:/original request/i})).toBeTruthy()
})

it('binds routine MCP approval to the exact system run and rejects altered proof or origin',()=>{
 const routine={version:1 as const,owner_id:1,agent_id:9,room_id:701,routine_id:id(60),run_id:id(61),config_revision:2,generation:3,root_event_id:id(62),origin:'test' as const,scheduled_at:'2026-10-29T01:00:00Z'}
 const ref={room_id:701,event_id:routine.root_event_id,seq:3,actor_type:'system' as const,actor_id:null,kind:'activity' as const}
 const mcp={server_id:id(70),config_revision:1,binding_revision:1,tool_id:'a'.repeat(64),tool_name:'read_document',schema_sha256:'b'.repeat(64),owner_id:1,agent_id:9,root_event_id:routine.root_event_id,generation:3,transport_sha256:'c'.repeat(64),input_sha256:'d'.repeat(64),transport_type:'http' as const}
 const a={...action(),trigger_event_id:routine.root_event_id,family:'mcp_tool_call',execution_binding:null,routine_binding:routine,root_ref:ref,mcp_binding:mcp,scope_status:'active' as const,revocation_state:null}
 const p=preview(a),review=p.review as {version:number;action:Record<string,unknown>};review.version=1;review.action.execution_binding=null;review.action.mcp_binding=mcp
 const good={...p,routine_binding:routine,mcp_binding:mcp,review}
 const event={...ref,device_id:null,actor_name_snapshot:'System',payload:{category:'owner_routine',status:'started',routine:{version:1,owner_id:1,agent_id:9,routine_id:routine.routine_id,run_id:routine.run_id,config_revision:2,origin:'test',scheduled_at:routine.scheduled_at}},created_at:routine.scheduled_at,idempotency_key:null}
 const context=executionContext(a,1,good)
 expect(actionPreviewMatches(a,good,1)).toBe(true)
 expect(routineMcpRootMatches(event,context,a)).toBe(true)
 expect(actionPreviewMatches(a,{...good,routine_binding:{...routine,generation:4}},1)).toBe(false)
 expect(actionPreviewMatches(a,{...good,mcp_binding:{...mcp,binding_revision:2}},1)).toBe(false)
 expect(actionPreviewMatches({...a,scope_status:'revoked'},good,1)).toBe(false)
 expect(routineMcpRootMatches({...event,payload:{...event.payload,routine:{...event.payload.routine,origin:'scheduled'}}},context,a)).toBe(false)
 expect(routineMcpRootMatches({...event,actor_type:'user',actor_id:1,kind:'message'},context,a)).toBe(false)
})
it('requires the exact routine root before enabling MCP once approval',async()=>{
 const routine={version:1 as const,owner_id:1,agent_id:9,room_id:701,routine_id:id(60),run_id:id(61),config_revision:2,generation:3,root_event_id:id(62),origin:'test' as const,scheduled_at:'2026-10-29T01:00:00Z'}
 const ref={room_id:701,event_id:routine.root_event_id,seq:3,actor_type:'system' as const,actor_id:null,kind:'activity' as const}
 const mcp={server_id:id(70),config_revision:1,binding_revision:1,tool_id:'a'.repeat(64),tool_name:'read_document',schema_sha256:'b'.repeat(64),owner_id:1,agent_id:9,root_event_id:routine.root_event_id,generation:3,transport_sha256:'c'.repeat(64),input_sha256:'d'.repeat(64),transport_type:'http' as const}
 const a={...action(),trigger_event_id:routine.root_event_id,family:'mcp_tool_call',execution_binding:null,routine_binding:routine,root_ref:ref,mcp_binding:mcp,scope_status:'active' as const,revocation_state:null}
 const p=preview(a),review=p.review as {version:number;action:Record<string,unknown>};review.version=1;review.action.execution_binding=null;review.action.mcp_binding=mcp
 vi.spyOn(toolsApi,'preview').mockResolvedValue({...p,routine_binding:routine,mcp_binding:mcp,review})
 const info={version:1,owner_id:1,agent_id:9,routine_id:routine.routine_id,run_id:routine.run_id,config_revision:2,origin:'test',scheduled_at:routine.scheduled_at}
 vi.spyOn(eventsApi,'list').mockResolvedValue([{...ref,device_id:null,actor_name_snapshot:'System',payload:{category:'owner_routine',status:'started',routine:info},created_at:routine.scheduled_at,idempotency_key:null}])
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><GrantPrompt roomId={701} roomName="Test" agentName="Bot" action={a} onRefresh={vi.fn()} onStop={vi.fn()} /></MemoryRouter></I18nProvider>)
 fireEvent.click(await screen.findByRole('checkbox'))
 const button=await screen.findByRole('button',{name:/once for Bot/i})
 await vi.waitFor(()=>expect((button as HTMLButtonElement).disabled).toBe(false))
})
