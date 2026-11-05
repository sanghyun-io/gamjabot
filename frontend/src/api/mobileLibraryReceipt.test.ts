import { afterEach, expect, it, vi } from 'vitest'
import api from '../lib/api'
import { mobileAccessApi, type MobileLibraryCommand } from './remote'
const id='a'.repeat(64),command:MobileLibraryCommand={operation_id:'11111111-1111-4111-8111-111111111111',enabled:true,expected_revision:0,expected_owner_epoch:'b'.repeat(64)}
afterEach(()=>vi.restoreAllMocks())
it('requires the exact device and original command in the receipt',async()=>{
 const good={...command,device_id:id,state:'applied',code:null,library_read:true,library_revision:1};const post=vi.spyOn(api,'post').mockResolvedValue({data:good})
 expect(await mobileAccessApi.libraryChange(id,command)).toEqual(good)
 for(const change of [{device_id:'c'.repeat(64)},{operation_id:'22222222-2222-4222-8222-222222222222'},{enabled:false},{expected_revision:1},{expected_owner_epoch:'c'.repeat(64)}]){
  post.mockResolvedValueOnce({data:{...good,...change}});await expect(mobileAccessApi.libraryResolve(id,command)).rejects.toThrow('Invalid library receipt')
 }
})
