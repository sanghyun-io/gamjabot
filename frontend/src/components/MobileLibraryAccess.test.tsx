// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useAuthStore } from '../stores/authStore'
import { mobileAccessApi, type MobileLibraryCommand } from '../api/remote'
import MobileLibraryAccess from './MobileLibraryAccess'
const device={id:'a'.repeat(64),name:'내 폰',library_read:false,library_revision:0}, ownerEpoch='b'.repeat(64)
afterEach(()=>{cleanup();vi.restoreAllMocks();sessionStorage.clear()})
it('requires an explicit per-device confirmation and resolves uncertain requests before another change',async()=>{
 useAuthStore.getState().setAuth('synthetic',{id:1,email:'owner@example.invalid',username:'Owner'})
 const epoch=useAuthStore.getState().epoch
 const send=vi.spyOn(mobileAccessApi,'libraryChange').mockRejectedValue(Error('lost'))
 const resolve=vi.spyOn(mobileAccessApi,'libraryResolve').mockImplementation(async(id,op:MobileLibraryCommand)=>({...op,device_id:id,state:'fenced',code:'not_applied',library_read:false,library_revision:0}))
 render(<MobileLibraryAccess device={device} owner={1} epoch={epoch} ownerEpoch={ownerEpoch} disabled={false} onRefresh={async()=>{}}/>)
 fireEvent.click(await screen.findByRole('button',{name:'보관함 읽기 허용'}));expect(send).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button',{name:'허용 확인'}));await screen.findByRole('button',{name:'요청 확인'})
 expect(send).toHaveBeenCalledTimes(1);const stored=JSON.parse(sessionStorage.getItem(`gamja.mobile-library.1.${device.id}`)!)
 fireEvent.click(screen.getByRole('button',{name:'요청 확인'}));await waitFor(()=>expect(resolve).toHaveBeenCalledWith(device.id,stored));await waitFor(()=>expect(sessionStorage.getItem(`gamja.mobile-library.1.${device.id}`)).toBeNull());expect(send).toHaveBeenCalledTimes(1)
})
it('preserves the original pending request after account change while a response is delayed',async()=>{
 useAuthStore.getState().setAuth('one',{id:1,email:'one@example.invalid',username:'One'});const epoch=useAuthStore.getState().epoch
 let done!:(value:Awaited<ReturnType<typeof mobileAccessApi.libraryChange>>)=>void,command!:MobileLibraryCommand
 vi.spyOn(mobileAccessApi,'libraryChange').mockImplementation((_id,op)=>{command=op;return new Promise(resolve=>{done=resolve})})
 render(<MobileLibraryAccess device={device} owner={1} epoch={epoch} ownerEpoch={ownerEpoch} disabled={false} onRefresh={async()=>{}}/>)
 fireEvent.click(await screen.findByRole('button',{name:'보관함 읽기 허용'}));fireEvent.click(screen.getByRole('button',{name:'허용 확인'}));await waitFor(()=>expect(command).toBeTruthy())
 useAuthStore.getState().setAuth('two',{id:2,email:'two@example.invalid',username:'Two'})
 await act(async()=>done({...command,device_id:device.id,state:'applied',code:null,library_read:true,library_revision:1}))
 expect(sessionStorage.getItem(`gamja.mobile-library.1.${device.id}`)).not.toBeNull();expect(sessionStorage.getItem(`gamja.mobile-library.2.${device.id}`)).toBeNull()
})
