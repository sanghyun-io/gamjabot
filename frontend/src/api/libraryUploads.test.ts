import { afterEach, expect, it, vi } from 'vitest'
import api from '../lib/api'
import { useAuthStore } from '../stores/authStore'
import { libraryUploadsApi } from './libraryUploads'
vi.mock('../lib/api',()=>({default:{put:vi.fn(),get:vi.fn(),delete:vi.fn()}}))
afterEach(()=>{useAuthStore.getState().logout();vi.resetAllMocks()})
it('sends raw empty bytes with the exact encoded name and verifies the owner receipt',async()=>{
 useAuthStore.getState().setAuth('synthetic',{id:1,email:'x@example.invalid',username:'Owner'})
 const id='11111111-1111-4111-8111-111111111111',digest='a'.repeat(64),bytes=new ArrayBuffer(0),receipt={upload_id:id,bot_id:8,filename:'한 글.pdf',size_bytes:0,sha256:digest,created_at:'2026-10-31T00:00:00Z',media_type:'application/octet-stream',origin:'owner_upload',removed:false}
 vi.mocked(api.put).mockResolvedValue({data:receipt});expect(await libraryUploadsApi.put(8,id,receipt.filename,digest,bytes)).toEqual(receipt)
 expect(api.put).toHaveBeenCalledWith(`/bots/8/library/uploads/${id}`,bytes,{headers:{'Content-Type':'application/octet-stream','X-Upload-Filename':encodeURIComponent(receipt.filename),'X-Upload-SHA256':digest},signal:undefined})
 vi.mocked(api.get).mockResolvedValue({data:{...receipt,bot_id:9}});await expect(libraryUploadsApi.read(8,id)).rejects.toThrow('Upload unavailable')
})
