import { afterEach, expect, it, vi } from 'vitest'
import { useAuthStore } from '../stores/authStore'
import { browserProfileSharingApi } from './browserProfileSharing'
import api from '../lib/api'
vi.mock('../lib/api',()=>({default:{get:vi.fn(),post:vi.fn()}}))
afterEach(()=>{useAuthStore.getState().logout();vi.resetAllMocks()})
it('requires an explicit boolean from the server and posts the selected flag',async()=>{
 useAuthStore.getState().setAuth('synthetic',{id:1,email:'x@example.invalid',username:'Owner'})
 const id='a'.repeat(32),body={profile_id:id,revision:1,home_room_id:1,bindings:[],rooms:[]}
 vi.mocked(api.get).mockResolvedValueOnce({data:body}).mockResolvedValueOnce({data:{...body,allow_delegated:false}})
 await expect(browserProfileSharingApi.read(1,id)).rejects.toThrow('Sharing unavailable')
 expect((await browserProfileSharingApi.read(1,id)).allow_delegated).toBe(false)
 vi.mocked(api.post).mockResolvedValue({data:{id,revision:2,home_room_id:1,allow_delegated:true}})
 await browserProfileSharingApi.save(1,id,1,[],true)
 expect(api.post).toHaveBeenCalledWith(`/tools/rooms/1/browser-profiles/${id}/sharing`,{revision:1,bindings:[],allow_delegated:true},{signal:undefined})
})
