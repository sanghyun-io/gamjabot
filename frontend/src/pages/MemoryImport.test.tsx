// @vitest-environment jsdom
import { webcrypto } from 'node:crypto'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import MemoryImport from './MemoryImport'
import { botsApi, type PrimaryBot } from '../api/bots'
import { eventsApi } from '../api/events'
import { memoryImportApi, type MemoryImportReceipt } from '../api/memoryImport'
import { I18nProvider } from '../i18n'
import { useAuthStore } from '../stores/authStore'
const eventId='ac100000-0000-4000-8000-000000000001',content='Prefers short answers.',source='Example AI'
const primary:PrimaryBot={version:1,revision:2,agent_id:8,agent:{id:8,name:'Ada',description:'',model_id:null,is_active:true},conversation:{room_id:501,project_id:40,href:'/chat/501',active:true},availability:'available'}
const receipt:MemoryImportReceipt={version:1,agent_id:8,room_id:501,href:`/chat/501?seq=1&event=${eventId}`,event_id:eventId,seq:1,replayed:false,status:'submitted'}
beforeEach(()=>{localStorage.removeItem('gamja.locale');useAuthStore.getState().setAuth('synthetic',{id:1,email:'owner@example.invalid',username:'Owner'});Object.defineProperty(crypto,'subtle',{configurable:true,value:webcrypto.subtle});vi.spyOn(botsApi,'primary').mockResolvedValue(primary);vi.spyOn(eventsApi,'list').mockImplementation(async()=>{const digest=Array.from(new Uint8Array(await webcrypto.subtle.digest('SHA-256',new TextEncoder().encode(content))),n=>n.toString(16).padStart(2,'0')).join('');return [{room_id:501,event_id:eventId,seq:1,actor_type:'user',actor_id:1,kind:'message',actor_name_snapshot:'Owner',created_at:'2026-10-30T00:00:00Z',device_id:null,idempotency_key:null,payload:{content:'wrapper',memory_import:{version:1,agent_id:8,source_label:source,content_sha256:digest}}}]})})
afterEach(()=>{cleanup();vi.restoreAllMocks()})
function Location(){const location=useLocation();return <p>{location.pathname+location.search}</p>}
function show(){render(<I18nProvider systemLanguages={()=>['en']}><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><MemoryRouter initialEntries={['/memory-import']}><Routes><Route path="/memory-import" element={<MemoryImport/>}/><Route path="/chat/:room" element={<Location/>}/></Routes></MemoryRouter></QueryClientProvider></I18nProvider>)}
async function fill(){await screen.findByText('Primary Bot · Ada');fireEvent.change(screen.getByLabelText('Answer source'),{target:{value:source}});fireEvent.change(screen.getByLabelText('Paste the answer'),{target:{value:content}})}
it('retries a lost response with the original target, key and input, then shows submitted rather than saved',async()=>{
 const submit=vi.spyOn(memoryImportApi,'submit').mockRejectedValueOnce(Error('lost')).mockResolvedValueOnce({...receipt,replayed:true});show();await fill();fireEvent.click(screen.getByText('Send to primary Bot'));await screen.findByText(/Could not confirm delivery/);fireEvent.click(screen.getByText('Check the same request'));await screen.findByText(receipt.href);expect(submit).toHaveBeenCalledTimes(2);expect(submit.mock.calls[1]).toEqual(submit.mock.calls[0])
})
it('does not send without an active primary conversation',async()=>{
 vi.mocked(botsApi.primary).mockResolvedValue({...primary,availability:'unset',agent_id:null,agent:null,conversation:null});const submit=vi.spyOn(memoryImportApi,'submit');show();await screen.findByText(/An available primary Bot/);expect((screen.getByText('Send to primary Bot') as HTMLButtonElement).disabled).toBe(true);expect(submit).not.toHaveBeenCalled()
})
it('keeps pasted input when the primary changed and rereads before another request',async()=>{
 const submit=vi.spyOn(memoryImportApi,'submit').mockRejectedValue({response:{status:409}});show();await fill();fireEvent.click(screen.getByText('Send to primary Bot'));await screen.findByText(/primary Bot or conversation changed/);fireEvent.click(screen.getByText('Read primary Bot again'));await waitFor(()=>expect((screen.getByLabelText('Paste the answer') as HTMLTextAreaElement).value).toBe(content));expect(submit).toHaveBeenCalledTimes(1)
})
it('keeps the original request unresolved when the submitted event cannot be read',async()=>{
 const submit=vi.spyOn(memoryImportApi,'submit').mockResolvedValue(receipt);vi.mocked(eventsApi.list).mockRejectedValue({response:{status:404}})
 show();await fill();fireEvent.click(screen.getByText('Send to primary Bot'));await screen.findByText(/Could not confirm delivery/);expect(screen.queryByText('Read primary Bot again')).toBeNull();expect(screen.getByText('Check the same request')).toBeTruthy();expect(submit).toHaveBeenCalledTimes(1)
})
it('does not claim a copy when the clipboard fails and allows manual selection',async()=>{
 Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:vi.fn().mockRejectedValue(Error('unavailable'))}});show();await screen.findByText('Primary Bot · Ada');fireEvent.click(screen.getByText('Copy question'));await screen.findByText(/Could not copy/);expect(screen.queryByText('Question copied.')).toBeNull();fireEvent.click(screen.getByText('Select text'));const field=screen.getByLabelText('Question for the other AI') as HTMLTextAreaElement;expect(document.activeElement).toBe(field);expect(field.selectionEnd-field.selectionStart).toBe(field.value.length)
})
it('does not submit while the Korean input method is composing',async()=>{
 const submit=vi.spyOn(memoryImportApi,'submit');show();await fill();const input=screen.getByLabelText('Paste the answer');fireEvent.compositionStart(input);fireEvent.submit(input.closest('form')!);expect(submit).not.toHaveBeenCalled();fireEvent.compositionEnd(input)
})
