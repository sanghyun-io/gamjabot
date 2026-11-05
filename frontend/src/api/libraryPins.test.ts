import { afterEach, expect, it, vi } from 'vitest'
import api from '../lib/api'
import { libraryPinsApi, type PinOperation } from './libraryPins'
const op:PinOperation={operation_id:'11111111-1111-4111-8111-111111111111',source:{kind:'result',room_id:501,result_id:'af041000-0000-4000-8000-000000000801',version:2},expected_revision:3,pinned:true,expected_owner_epoch:'a'.repeat(64)}
const receipt={state:'applied',revision:4,pinned:true,code:null,operation_id:op.operation_id,source:op.source,expected_revision:op.expected_revision,requested_pinned:op.pinned,expected_owner_epoch:op.expected_owner_epoch}
afterEach(()=>vi.restoreAllMocks())
it('accepts only receipts bound to the exact source, operation, revision, action and epoch',async()=>{
 const post=vi.spyOn(api,'post').mockResolvedValue({data:receipt})
 expect(await libraryPinsApi.operate(8,op)).toEqual(receipt)
 for(const change of [{operation_id:'22222222-2222-4222-8222-222222222222'},{source:{...op.source,version:3}},{expected_revision:4},{requested_pinned:false},{expected_owner_epoch:'b'.repeat(64)}]){
  post.mockResolvedValueOnce({data:{...receipt,...change}})
  await expect(libraryPinsApi.resolve(8,op)).rejects.toThrow('Pin receipt mismatch')
 }
})
