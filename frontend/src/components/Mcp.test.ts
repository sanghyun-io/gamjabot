import { describe, expect, it } from 'vitest'
import { validTransport, validPreview, validCommand, type McpServer } from '../api/mcp'
const server:McpServer={version:1,server_id:'b2000000-0000-4000-8000-000000000001',owner_id:1,revision:1,name:'Docs',enabled:true,transport:{type:'http',url:'https://example.invalid/mcp'},credential_ref:null,discovery_state:'not_discovered'}
describe('app connection boundaries',()=>{
 it('requires an absolute target without credentials',()=>{expect(validTransport(server.transport)).toBe(true);expect(validTransport({type:'http',url:'https://user:secret@example.invalid/'})).toBe(false);expect(validTransport({type:'http',url:'https://example.invalid/mcp?token=redacted'})).toBe(false);expect(validTransport({type:'http',url:'http://example.invalid/mcp'})).toBe(false);expect(validTransport({type:'stdio',executable:'/bin/tool',argv:['api_key=redacted']})).toBe(false);expect(validTransport({type:'stdio',executable:'/bin/tool',argv:['--token','redacted']})).toBe(false);expect(validTransport({type:'stdio',executable:'relative',argv:[]})).toBe(false);expect(validTransport({type:'stdio',executable:'/bin/tool',argv:['one']})).toBe(true)})
 it('binds preview and discovery to the exact server revision',()=>{const p={version:1,preview_id:'b2000000-0000-4000-8000-000000000002',server_id:server.server_id,revision:1,transport:server.transport,expires_at:'2099-10-29T00:00:00Z'};expect(validPreview(p,server)).toBe(true);expect(validPreview({...p,revision:2},server)).toBe(false);expect(validPreview({...p,transport:{type:'http',url:'https://other.invalid'}},server)).toBe(false);const receipt={version:1,operation:'discover',discovery:{discovery_id:'b2000000-0000-4000-8000-000000000003',server_id:server.server_id,server_revision:1,state:'completed',failure_code:null}};expect(validCommand(receipt,'discover',server)).toBe(true);expect(validCommand({...receipt,discovery:{...receipt.discovery,server_revision:2}},'discover',server)).toBe(false)})
})
import { validSecretRef } from '../api/mcpCredentials'
it('accepts only the frozen Owner, server and operation secret reference',()=>{const id='0123456789abcdef0123456789abcdef',ref=`mcp:1:${server.server_id}:${id}`;expect(validSecretRef(ref,1,server.server_id,id)).toBe(true);expect(validSecretRef(ref,2,server.server_id,id)).toBe(false);expect(validSecretRef(ref,1,server.server_id,'ffffffffffffffffffffffffffffffff')).toBe(false)})
import { vi } from 'vitest'
import api from '../lib/api'
import { mcpApi } from '../api/mcp'
import { useAuthStore } from '../stores/authStore'
it('binds the selected tools to the reviewed server revision',async()=>{
 useAuthStore.getState().setAuth('synthetic',{id:1,email:'owner@example.invalid',username:'Owner'})
 const ids=['a'.repeat(64)],binding={server_id:server.server_id,revision:2,enabled:false,tool_ids:null}
 const request=vi.spyOn(api,'put').mockResolvedValue({data:{version:1,agent_id:8,server_id:server.server_id,revision:3,enabled:true,tool_ids:ids}})
 try{await mcpApi.bind(8,binding,server.revision,true,ids);expect(request).toHaveBeenCalledWith(`/bots/8/mcp/${server.server_id}`,{expected_revision:2,expected_server_revision:1,enabled:true,tool_ids:ids})}finally{request.mockRestore()}
})
import { mcpTargetParts } from './GrantPrompt'
import type { ToolActivity } from '../api/tools'
it('renders a valid MCP target and falls back on mismatched tool or malformed target',()=>{
 const action={family:'mcp_tool_call',mcp_binding:{tool_name:'read_document',transport_type:'http'}} as unknown as ToolActivity
 const value='문서 도구 | {"type":"http","url":"https://example.invalid/mcp"} | read_document'
 expect(mcpTargetParts(value,action)).toEqual({name:'문서 도구',transport:{type:'http',url:'https://example.invalid/mcp'},tool:'read_document'})
 expect(mcpTargetParts(value.replace('read_document','other_tool'),action)).toBeNull()
 expect(mcpTargetParts('unexpected target',action)).toBeNull()
})
