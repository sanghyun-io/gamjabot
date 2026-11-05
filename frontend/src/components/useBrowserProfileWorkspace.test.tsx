// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import api from '../lib/api'
import { useBrowserProfileWorkspace } from './useBrowserProfileWorkspace'
import type { BrowserProfile } from './BrowserProfileActionDialog'

const profile=(id:string)=>({id,name:id,revision:1,connected:true,controller:'agent',agent_id:1,agent_ids:[1],origin_urls:['https://example.test'],account_label:'',last_used_at:null,expires_in_seconds:60}) as BrowserProfile
const response=(p:BrowserProfile,ctx='context')=>({data:new Blob(['image'],{type:'image/jpeg'}),headers:{'x-browser-profile-id':p.id,'x-browser-profile-revision':'1','x-browser-context-id':ctx,'x-captured-at':'2026-10-31T00:00:00Z'}})
afterEach(()=>vi.restoreAllMocks())
it('keeps two independently, retains failed old snapshot, rejects third and releases URLs',async()=>{
  const a=profile('a'),b=profile('b'),c=profile('c'),all=[a,b,c],current=()=>true,generation={current:1}
  const revoke=vi.fn();vi.stubGlobal('URL',{createObjectURL:vi.fn().mockReturnValueOnce('blob:a').mockReturnValueOnce('blob:b'),revokeObjectURL:revoke})
  const get=vi.spyOn(api,'get').mockImplementation(async url=>response(url.includes('/a/')?a:b) as never)
  const {result,unmount}=renderHook(()=>useBrowserProfileWorkspace(1,'owner',true,current,generation,id=>all.find(p=>p.id===id)))
  await act(async()=>{await result.current.capture(a);await result.current.capture(b)})
  expect(result.current.slots.map(s=>s.preview?.url)).toEqual(['blob:a','blob:b'])
  await act(async()=>{await result.current.capture(c)})
  expect(get).toHaveBeenCalledTimes(2)
  get.mockRejectedValueOnce(new Error('offline'))
  await act(async()=>{await result.current.capture(a)})
  expect(result.current.slots[0].failed).toBe(true);expect(result.current.slots[0].preview?.url).toBe('blob:a')
  act(()=>result.current.abort('a'))
  expect(result.current.slots[1].preview?.url).toBe('blob:b')
  act(()=>result.current.close('a'));expect(revoke).toHaveBeenCalledWith('blob:a')
  unmount();expect(revoke).toHaveBeenCalledWith('blob:b')
  vi.unstubAllGlobals()
})
it('rejects mismatched profile headers without displaying bytes',async()=>{
  const a=profile('a'), current=()=>true,generation={current:1}
  vi.spyOn(api,'get').mockResolvedValueOnce({...response(a),headers:{...response(a).headers,'x-browser-profile-id':'wrong'}} as never)
  const {result}=renderHook(()=>useBrowserProfileWorkspace(1,'owner',true,current,generation,()=>a))
  await act(async()=>{await result.current.capture(a)})
  expect(result.current.slots[0].failed).toBe(true);expect(result.current.slots[0].preview).toBeNull()
})
it('discards a late response after the selected preview is closed',async()=>{
  const a=profile('a'), current=()=>true,generation={current:1}
  let finish!:(value:unknown)=>void
  vi.spyOn(api,'get').mockImplementation(()=>new Promise(resolve=>{finish=resolve}) as never)
  const create=vi.fn(()=> 'blob:late');vi.stubGlobal('URL',{createObjectURL:create,revokeObjectURL:vi.fn()})
  const {result,unmount}=renderHook(()=>useBrowserProfileWorkspace(1,'owner',true,current,generation,()=>a))
  let pending!:Promise<void>
  act(()=>{pending=result.current.capture(a)})
  act(()=>result.current.close('a'))
  await act(async()=>{finish(response(a));await pending})
  expect(result.current.slots).toEqual([]);expect(create).not.toHaveBeenCalled()
  unmount();vi.unstubAllGlobals()
})
it('aborts pending capture and rejects late bytes when workspace closes',async()=>{
  const a=profile('a'),current=()=>true,generation={current:1};let finish!:(value:unknown)=>void;let signal:AbortSignal|undefined
  vi.spyOn(api,'get').mockImplementation((_url,config)=>{signal=config?.signal as AbortSignal;return new Promise(resolve=>{finish=resolve}) as never})
  const create=vi.fn(()=> 'blob:late');vi.stubGlobal('URL',{createObjectURL:create,revokeObjectURL:vi.fn()})
  const {result,rerender,unmount}=renderHook(({open})=>useBrowserProfileWorkspace(1,'owner',open,current,generation,()=>a),{initialProps:{open:true}})
  let pending!:Promise<void>;act(()=>{pending=result.current.capture(a)});rerender({open:false})
  expect(signal?.aborted).toBe(true)
  await act(async()=>{finish(response(a));await pending})
  expect(create).not.toHaveBeenCalled();unmount();vi.unstubAllGlobals()
})
it('masks prior owner image during the first render of a new identity',async()=>{
  const a=profile('a'),current=()=>true,generation={current:1};vi.stubGlobal('URL',{createObjectURL:()=> 'blob:old',revokeObjectURL:vi.fn()})
  vi.spyOn(api,'get').mockResolvedValueOnce(response(a) as never)
  const observed:string[][]=[]
  const {result,rerender,unmount}=renderHook(({identity})=>{const workspace=useBrowserProfileWorkspace(1,identity,true,current,generation,()=>a);observed.push(workspace.slots.map(s=>s.id));return workspace},{initialProps:{identity:'owner-1'}})
  await act(async()=>{await result.current.capture(a)})
  expect(result.current.slots).toHaveLength(1);observed.length=0
  rerender({identity:'owner-2'})
  expect(observed[0]).toEqual([])
  unmount();vi.unstubAllGlobals()
})
