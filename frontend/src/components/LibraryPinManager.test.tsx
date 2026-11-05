// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { I18nProvider } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { libraryPinsApi, type PinOperation, type PinSource } from '../api/libraryPins'
import { roomResultsApi } from '../api/roomResults'
import LibraryPinManager, { useLibraryPinActions } from './LibraryPinManager'
const source:PinSource={kind:'result',room_id:501,result_id:'af041000-0000-4000-8000-000000000801',version:2}
function Child(){const {pin,busy}=useLibraryPinActions();return <button disabled={busy} onClick={()=>pin(source)}>pin source</button>}
afterEach(()=>{cleanup();vi.restoreAllMocks();sessionStorage.clear()})
it('preserves an unknown request and resolves it before allowing another choice',async()=>{
 useAuthStore.getState().setAuth('synthetic',{id:1,email:'test@example.invalid',username:'Owner'})
 vi.spyOn(libraryPinsApi,'context').mockResolvedValue({owner_id:1,owner_epoch:'b'.repeat(64)})
 vi.spyOn(libraryPinsApi,'list').mockResolvedValue({items:[],next_cursor:null})
 vi.spyOn(libraryPinsApi,'status').mockResolvedValue({source,revision:0,pinned:false,availability:'available'})
 const send=vi.spyOn(libraryPinsApi,'operate').mockRejectedValue(new Error('lost response'))
 const resolve=vi.spyOn(libraryPinsApi,'resolve').mockImplementation(async(_bot,op:PinOperation)=>({state:'fenced',revision:null,pinned:null,code:null,operation_id:op.operation_id,source:op.source,expected_revision:op.expected_revision,requested_pinned:op.pinned,expected_owner_epoch:op.expected_owner_epoch}))
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><LibraryPinManager botId={8} onUpload={()=>{}}><Child/></LibraryPinManager></MemoryRouter></I18nProvider>)
 const button=await screen.findByRole('button',{name:'pin source'});await waitFor(()=>expect(button.hasAttribute('disabled')).toBe(false))
 fireEvent.click(button)
 await screen.findByRole('button',{name:'Check request'})
 expect(button.hasAttribute('disabled')).toBe(true)
 expect(send).toHaveBeenCalledTimes(1)
 const stored=JSON.parse(sessionStorage.getItem('gamja.library-pin.1.8')||'null')
 fireEvent.click(screen.getByRole('button',{name:'Check request'}))
 await waitFor(()=>expect(resolve).toHaveBeenCalledWith(8,stored))
 await waitFor(()=>expect(sessionStorage.getItem('gamja.library-pin.1.8')).toBeNull())
 expect(send).toHaveBeenCalledTimes(1)
})
it('does not start an old owner request after the account changes while status is loading',async()=>{
 useAuthStore.getState().setAuth('owner-one',{id:1,email:'one@example.invalid',username:'One'})
 vi.spyOn(libraryPinsApi,'context').mockResolvedValue({owner_id:1,owner_epoch:'b'.repeat(64)})
 vi.spyOn(libraryPinsApi,'list').mockResolvedValue({items:[],next_cursor:null})
 let complete!:(value:{source:PinSource;revision:number;pinned:boolean;availability:'available'})=>void
 vi.spyOn(libraryPinsApi,'status').mockReturnValue(new Promise(resolve=>{complete=resolve}))
 const send=vi.spyOn(libraryPinsApi,'operate')
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><LibraryPinManager botId={8} onUpload={()=>{}}><Child/></LibraryPinManager></MemoryRouter></I18nProvider>)
 const button=await screen.findByRole('button',{name:'pin source'});await waitFor(()=>expect(button.hasAttribute('disabled')).toBe(false));fireEvent.click(button)
 useAuthStore.getState().setAuth('owner-two',{id:2,email:'two@example.invalid',username:'Two'})
 await act(async()=>{complete({source,revision:0,pinned:false,availability:'available'})})
 await waitFor(()=>expect(send).not.toHaveBeenCalled())
 expect(sessionStorage.getItem('gamja.library-pin.1.8')).toBeNull()
 expect(sessionStorage.getItem('gamja.library-pin.2.8')).toBeNull()
})
it('keeps the original owner pending receipt when an operation finishes after account change',async()=>{
 useAuthStore.getState().setAuth('owner-one',{id:1,email:'one@example.invalid',username:'One'})
 vi.spyOn(libraryPinsApi,'context').mockResolvedValue({owner_id:1,owner_epoch:'b'.repeat(64)})
 vi.spyOn(libraryPinsApi,'list').mockResolvedValue({items:[],next_cursor:null})
 vi.spyOn(libraryPinsApi,'status').mockResolvedValue({source,revision:0,pinned:false,availability:'available'})
 let complete!:(value:Awaited<ReturnType<typeof libraryPinsApi.operate>>)=>void
 let sent!:PinOperation
 vi.spyOn(libraryPinsApi,'operate').mockImplementation((_bot,op)=>{sent=op;return new Promise(resolve=>{complete=resolve})})
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><LibraryPinManager botId={8} onUpload={()=>{}}><Child/></LibraryPinManager></MemoryRouter></I18nProvider>)
 const button=await screen.findByRole('button',{name:'pin source'});await waitFor(()=>expect(button.hasAttribute('disabled')).toBe(false));fireEvent.click(button)
 await waitFor(()=>expect(sessionStorage.getItem('gamja.library-pin.1.8')).not.toBeNull())
 useAuthStore.getState().setAuth('owner-two',{id:2,email:'two@example.invalid',username:'Two'})
 await act(async()=>{complete({state:'applied',revision:1,pinned:true,code:null,operation_id:sent.operation_id,source:sent.source,expected_revision:sent.expected_revision,requested_pinned:sent.pinned,expected_owner_epoch:sent.expected_owner_epoch})})
 expect(sessionStorage.getItem('gamja.library-pin.1.8')).not.toBeNull()
 expect(sessionStorage.getItem('gamja.library-pin.2.8')).toBeNull()
})
it('permits an unknown file result shortcut while blocking an unknown uploaded file',async()=>{
 useAuthStore.getState().setAuth('owner-one',{id:1,email:'one@example.invalid',username:'One'})
 vi.spyOn(libraryPinsApi,'context').mockResolvedValue({owner_id:1,owner_epoch:'b'.repeat(64)})
 vi.spyOn(libraryPinsApi,'list').mockResolvedValue({items:[],next_cursor:null})
 vi.spyOn(libraryPinsApi,'status').mockImplementation(async(_bot,s)=>({source:s,revision:0,pinned:false,availability:'unknown'}))
 const send=vi.spyOn(libraryPinsApi,'operate').mockImplementation(async(_bot,op)=>({state:'applied',revision:1,pinned:true,code:null,operation_id:op.operation_id,source:op.source,expected_revision:op.expected_revision,requested_pinned:op.pinned,expected_owner_epoch:op.expected_owner_epoch}))
 const uploaded:PinSource={kind:'upload',upload_id:'11111111-1111-4111-8111-111111111111',sha256:'a'.repeat(64)}
 function Buttons(){const {pin,busy}=useLibraryPinActions();return <><button disabled={busy} onClick={()=>pin(source)}>result</button><button disabled={busy} onClick={()=>pin(uploaded)}>upload</button></>}
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><LibraryPinManager botId={8} onUpload={()=>{}}><Buttons/></LibraryPinManager></MemoryRouter></I18nProvider>)
 const result=screen.getByRole('button',{name:'result'});await waitFor(()=>expect(result.hasAttribute('disabled')).toBe(false));fireEvent.click(result)
 await waitFor(()=>expect(send).toHaveBeenCalledTimes(1));await waitFor(()=>expect(result.hasAttribute('disabled')).toBe(false))
 fireEvent.click(screen.getByRole('button',{name:'upload'}));await screen.findByText('The original is unavailable, so it was not pinned.')
 expect(send).toHaveBeenCalledTimes(1)
})

it('shows each pin independently when another original detail is delayed',async()=>{
 useAuthStore.getState().setAuth('owner-one',{id:1,email:'one@example.invalid',username:'One'})
 const second:PinSource={kind:'result',room_id:501,result_id:'af041000-0000-4000-8000-000000000802',version:2}
 vi.spyOn(libraryPinsApi,'list').mockResolvedValue({items:[{source,revision:1,pinned:true,availability:'available'},{source:second,revision:1,pinned:true,availability:'available'}],next_cursor:null})
 vi.spyOn(roomResultsApi,'detail').mockImplementation(async(_room,id)=>id===source.result_id?new Promise(()=>{}):({room_id:501,result_id:second.result_id,version:2,author_agent_id:8,title:'Second ready',kind:'page'} as never))
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><LibraryPinManager botId={8} onUpload={()=>{}}><Child/></LibraryPinManager></MemoryRouter></I18nProvider>)
 expect(await screen.findByText('Second ready')).toBeTruthy()
 expect(screen.queryByText('No pinned items.')).toBeNull()
 expect(screen.getAllByRole('button',{name:'Unpin'})).toHaveLength(2)
})
