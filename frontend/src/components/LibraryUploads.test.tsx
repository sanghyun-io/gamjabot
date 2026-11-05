// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { I18nProvider } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { libraryUploadsApi } from '../api/libraryUploads'
import LibraryUploads from './LibraryUploads'
vi.mock('../api/libraryUploads',async original=>{const v=await original<typeof import('../api/libraryUploads')>();return {...v,libraryUploadsApi:{list:vi.fn(),read:vi.fn(),put:vi.fn(),file:vi.fn(),remove:vi.fn()}}})
const id='11111111-1111-4111-8111-111111111111',digest='ab'.repeat(32),bytes=new Uint8Array([1,2,3]),buffer=new ArrayBuffer(3);new Uint8Array(buffer).set(bytes)
const item={upload_id:id,bot_id:8,filename:'paper.pdf',size_bytes:3,sha256:digest,created_at:'2026-10-31T00:00:00Z',media_type:'application/octet-stream' as const,origin:'owner_upload' as const,removed:false}
const renderView=()=>render(<I18nProvider systemLanguages={()=>['en']}><LibraryUploads botId={8}/></I18nProvider>)
beforeEach(()=>{useAuthStore.getState().setAuth('synthetic',{id:1,email:'x@example.invalid',username:'Owner'});vi.mocked(libraryUploadsApi.list).mockResolvedValue({bot_id:8,items:[],next_cursor:null});vi.stubGlobal('crypto',{randomUUID:()=>id,subtle:{digest:vi.fn(async()=>new Uint8Array(32).fill(0xab).buffer)}});vi.stubGlobal('URL',Object.assign(URL,{createObjectURL:vi.fn(()=> 'blob:fixture'),revokeObjectURL:vi.fn()}))})
afterEach(()=>{cleanup();sessionStorage.clear();useAuthStore.getState().logout();vi.resetAllMocks();vi.unstubAllGlobals()})
it('requires explicit save and recovers an unknown upload by matching its receipt',async()=>{
 vi.mocked(libraryUploadsApi.put).mockRejectedValue(Error('lost'));vi.mocked(libraryUploadsApi.read).mockResolvedValue(item)
 renderView();await screen.findByText('No files added.');const file=new File([bytes],'paper.pdf',{type:'application/pdf'});Object.defineProperty(file,'arrayBuffer',{value:async()=>buffer});fireEvent.change(screen.getByLabelText('Choose a file (up to 20 MiB)'),{target:{files:[file]}})
 expect(libraryUploadsApi.put).not.toHaveBeenCalled();await waitFor(()=>expect(screen.getByRole('button',{name:'Save to library'})).toHaveProperty('disabled',false));fireEvent.click(screen.getByRole('button',{name:'Save to library'}));await screen.findByText(/save result is unknown/i)
 expect(Object.values(sessionStorage).join(' ')).not.toContain('application/pdf');fireEvent.click(screen.getByRole('button',{name:'Check status'}));expect(await screen.findByText('Added by you',{exact:false})).toBeTruthy();expect(libraryUploadsApi.put).toHaveBeenCalledTimes(1)
})
it('refuses a download with a mismatched digest',async()=>{
 vi.mocked(libraryUploadsApi.list).mockResolvedValue({bot_id:8,items:[item],next_cursor:null});vi.mocked(libraryUploadsApi.file).mockResolvedValue({data:buffer,headers:{'content-type':'application/octet-stream'}} as never);vi.mocked(crypto.subtle.digest).mockResolvedValue(new Uint8Array(32).buffer)
 renderView();fireEvent.click(await screen.findByRole('button',{name:'Verify file'}));expect(await screen.findByRole('alert')).toBeTruthy();expect(screen.queryByRole('link',{name:'Download verified file'})).toBeNull()
})
it('does not repeat deletion after a lost response and verifies removed status',async()=>{
 vi.mocked(libraryUploadsApi.list).mockResolvedValue({bot_id:8,items:[item],next_cursor:null});vi.mocked(libraryUploadsApi.remove).mockRejectedValue(Error('lost'));vi.mocked(libraryUploadsApi.read).mockResolvedValue({...item,removed:true})
 renderView();fireEvent.click(await screen.findByRole('button',{name:'Remove saved copy'}));fireEvent.click(screen.getAllByRole('button',{name:'Remove saved copy'})[1]);await screen.findByText(/Removal result is unknown/);fireEvent.click(screen.getByRole('button',{name:'Check status'}));await screen.findByText('Saved copy removed.');expect(libraryUploadsApi.remove).toHaveBeenCalledTimes(1)
})
it('does not upload a file read after the Owner changes',async()=>{
 let finish!:(value:ArrayBuffer)=>void;renderView();await screen.findByText('No files added.');const file=new File([bytes],'paper.pdf',{type:'application/pdf'});Object.defineProperty(file,'arrayBuffer',{value:()=>new Promise(resolve=>{finish=resolve})});fireEvent.change(screen.getByLabelText('Choose a file (up to 20 MiB)'),{target:{files:[file]}});fireEvent.click(screen.getByRole('button',{name:'Save to library'}));useAuthStore.getState().setAuth('other',{id:2,email:'y@example.invalid',username:'Other'});finish(buffer);await waitFor(()=>expect(libraryUploadsApi.put).not.toHaveBeenCalled())
})
it('recovers an unknown receipt after a component remount without storing file bytes',async()=>{
 vi.mocked(libraryUploadsApi.put).mockRejectedValue(Error('lost'));vi.mocked(libraryUploadsApi.read).mockResolvedValue(item)
 const view=renderView();await screen.findByText('No files added.');const file=new File([bytes],'paper.pdf',{type:'application/pdf'});Object.defineProperty(file,'arrayBuffer',{value:async()=>buffer});fireEvent.change(screen.getByLabelText('Choose a file (up to 20 MiB)'),{target:{files:[file]}});fireEvent.click(screen.getByRole('button',{name:'Save to library'}));await screen.findByText(/save result is unknown/i);view.unmount();renderView();expect(await screen.findByText(/save result is unknown/i)).toBeTruthy();fireEvent.click(screen.getByRole('button',{name:'Check status'}));await screen.findByText('Added by you',{exact:false});expect(libraryUploadsApi.put).toHaveBeenCalledTimes(1)
})
it('reselects the original after confirmed missing and retries only the same upload id',async()=>{
 vi.mocked(libraryUploadsApi.put).mockRejectedValueOnce(Error('lost')).mockResolvedValueOnce(item);vi.mocked(libraryUploadsApi.read).mockRejectedValue({isAxiosError:true,response:{status:404}})
 renderView();await screen.findByText('No files added.');const file=new File([bytes],'paper.pdf',{type:'application/pdf'});Object.defineProperty(file,'arrayBuffer',{value:async()=>buffer});const input=screen.getByLabelText('Choose a file (up to 20 MiB)');fireEvent.change(input,{target:{files:[file]}});fireEvent.click(screen.getByRole('button',{name:'Save to library'}));await screen.findByText(/save result is unknown/i);fireEvent.click(screen.getByRole('button',{name:'Check status'}));await screen.findByText(/saved upload was not found/i);expect(libraryUploadsApi.put).toHaveBeenCalledTimes(1);fireEvent.change(input,{target:{files:[file]}});await waitFor(()=>expect(screen.getByRole('button',{name:'Save to library'})).toHaveProperty('disabled',false));fireEvent.click(screen.getByRole('button',{name:'Save to library'}));await screen.findByText('Added by you',{exact:false});expect(libraryUploadsApi.put).toHaveBeenCalledTimes(2);expect(vi.mocked(libraryUploadsApi.put).mock.calls[1][1]).toBe(vi.mocked(libraryUploadsApi.put).mock.calls[0][1])
})
it('rejects reselected bytes with a different digest without a retry',async()=>{
 vi.mocked(libraryUploadsApi.put).mockRejectedValue(Error('lost'));vi.mocked(libraryUploadsApi.read).mockRejectedValue({isAxiosError:true,response:{status:404}})
 renderView();await screen.findByText('No files added.');const file=new File([bytes],'paper.pdf',{type:'application/pdf'});Object.defineProperty(file,'arrayBuffer',{value:async()=>buffer});const input=screen.getByLabelText('Choose a file (up to 20 MiB)');fireEvent.change(input,{target:{files:[file]}});fireEvent.click(screen.getByRole('button',{name:'Save to library'}));await screen.findByText(/save result is unknown/i);fireEvent.click(screen.getByRole('button',{name:'Check status'}));await screen.findByText(/saved upload was not found/i);vi.mocked(crypto.subtle.digest).mockResolvedValue(new Uint8Array(32).buffer);fireEvent.change(input,{target:{files:[file]}});await screen.findByText(/does not match the original name/i);expect(libraryUploadsApi.put).toHaveBeenCalledTimes(1)
})
