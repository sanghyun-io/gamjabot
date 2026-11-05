// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import ResultRevision from './ResultRevision'
import { roomResultsApi, type ResultDetail, type RevisionStatus } from '../api/roomResults'
import { eventsApi } from '../api/events'
import { useAuthStore } from '../stores/authStore'
import { I18nProvider } from '../i18n'
const id='af041000-0000-4000-8000-000000000801',rootId='af041000-0000-4000-8000-000000000820',answerId='af041000-0000-4000-8000-000000000821',instruction='Shorten the report'
const ref=(event_id:string,seq:number,actor_type:'user'|'agent',actor_id:number)=>({room_id:501,event_id,seq,actor_type,actor_id,kind:'message' as const,href:`/chat/501?seq=${seq}&event=${event_id}`})
const root=ref(rootId,2,'user',1),answer=ref(answerId,3,'agent',8)
const value={result_id:id,room_id:501,version:2,current_version:2,kind:'text',title:'Report',author_agent_id:8,source:{event_id:'af041000-0000-4000-8000-000000000810',seq:1}} as ResultDetail
const binding={room_id:501,result_id:id,expected_current_version:2,author_agent_id:8,instruction,root_ref:root}
const pending:RevisionStatus={...binding,state:'pending'},ready:RevisionStatus={...binding,state:'ready',source_event_id:answerId,source_ref:answer},completed:RevisionStatus={...binding,state:'completed',result_ref:{room_id:501,result_id:id,version:3,href:`/chat/501?result=${id}&version=3`,source_event_id:answerId,source_ref:answer}}
const detail={...value,version:3,current_version:3,source:{event_id:answerId,seq:3,href:answer.href}}
beforeEach(()=>{localStorage.removeItem('gamja.locale');useAuthStore.getState().setAuth('synthetic',{id:1,email:'owner@example.invalid',username:'Owner'});vi.spyOn(eventsApi,'list').mockImplementation(async(_room,after)=>[{...(after===1?root:answer),device_id:null,actor_name_snapshot:'Synthetic',payload:after===1?{page_revision:{version:1,result_id:id,base_version:2,agent_id:8,instruction}}:{trigger_event_id:rootId},created_at:'2026-10-30T00:00:00Z',idempotency_key:null}]);vi.spyOn(roomResultsApi,'detail').mockResolvedValue(detail);vi.spyOn(roomResultsApi,'revisionActive').mockResolvedValue({room_id:501,result_id:id,base_version:2,revision:null})})
afterEach(()=>{cleanup();vi.restoreAllMocks()})
function show(){render(<I18nProvider systemLanguages={()=>['en']}><ResultRevision value={value} ownerId={1} identity={`1:${useAuthStore.getState().epoch}`} canUse={()=>true} canStart active onNavigate={vi.fn()} onOpenVersion={vi.fn()} onSaved={vi.fn()}/></I18nProvider>)}
async function send(){fireEvent.click(await screen.findByText('Request a revision'));fireEvent.change(screen.getByLabelText('What should change?'),{target:{value:instruction}});fireEvent.click(screen.getByText('Send revision request'))}
it('saves a verified ready answer once and shows the exact new version',async()=>{
 const start=vi.spyOn(roomResultsApi,'revisionStart').mockResolvedValue(pending),status=vi.spyOn(roomResultsApi,'revisionStatus').mockResolvedValue(ready),finalize=vi.spyOn(roomResultsApi,'revisionFinalize').mockResolvedValue(completed)
 show();await send();await screen.findByText('Waiting for the Bot’s revised answer.');fireEvent.click(screen.getByText('Check status'))
 expect(await screen.findByText('The revised report was saved as a new version.')).toBeTruthy();expect(screen.getByText('View new version v3')).toBeTruthy();expect(start).toHaveBeenCalledTimes(1);expect(status).toHaveBeenCalledTimes(1);expect(finalize).toHaveBeenCalledTimes(1)
})
it('uses only the same receipt after a lost request and does not replay an uncertain finalize without confirmed readiness',async()=>{
 const start=vi.spyOn(roomResultsApi,'revisionStart').mockRejectedValue(new Error('lost')),receipt=vi.spyOn(roomResultsApi,'revisionReceipt').mockResolvedValue(ready),status=vi.spyOn(roomResultsApi,'revisionStatus').mockResolvedValue({...binding,state:'unknown'}),finalize=vi.spyOn(roomResultsApi,'revisionFinalize').mockRejectedValue(new Error('lost'))
 show();await send();await screen.findByText(/Could not confirm the result/);fireEvent.click(screen.getByText('Check status'));await waitFor(()=>expect(finalize).toHaveBeenCalledTimes(1));await screen.findByText(/Could not confirm the result/);fireEvent.click(screen.getByText('Check status'));await waitFor(()=>expect(status).toHaveBeenCalledTimes(1));expect(finalize).toHaveBeenCalledTimes(1);expect(start).toHaveBeenCalledTimes(1);expect(receipt.mock.calls[0][2]).toBe(start.mock.calls[0][4])
})
it('recovers the same ready request after remount and finalizes without dispatching again',async()=>{
 vi.mocked(roomResultsApi.revisionActive).mockResolvedValue({room_id:501,result_id:id,base_version:2,revision:ready})
 const start=vi.spyOn(roomResultsApi,'revisionStart'),finalize=vi.spyOn(roomResultsApi,'revisionFinalize').mockResolvedValue(completed)
 show();expect(await screen.findByText('The revised report was saved as a new version.')).toBeTruthy();expect(start).not.toHaveBeenCalled();expect(finalize).toHaveBeenCalledTimes(1)
})
it('keeps new requests blocked when the active revision cannot be verified',async()=>{
 vi.mocked(roomResultsApi.revisionActive).mockResolvedValue({room_id:501,result_id:id,base_version:2,revision:{...ready,author_agent_id:9}})
 const start=vi.spyOn(roomResultsApi,'revisionStart'),finalize=vi.spyOn(roomResultsApi,'revisionFinalize')
 show();expect(await screen.findByRole('alert')).toBeTruthy();expect(screen.queryByText('Request a revision')).toBeNull();expect(start).not.toHaveBeenCalled();expect(finalize).not.toHaveBeenCalled()
})
it('recovers a missing request only on an explicit action with the original key and body',async()=>{
 const start=vi.spyOn(roomResultsApi,'revisionStart').mockRejectedValueOnce(new Error('lost')).mockResolvedValue(pending)
 vi.spyOn(roomResultsApi,'revisionReceipt').mockRejectedValue({response:{status:404}})
 show();await send();await screen.findByText(/Could not confirm the result/);fireEvent.click(screen.getByText('Check status'))
 const button=await screen.findByText('Recover the same request');expect(start).toHaveBeenCalledTimes(1);fireEvent.click(button);fireEvent.click(button)
 await screen.findByText('Waiting for the Bot’s revised answer.');expect(start).toHaveBeenCalledTimes(2);expect(start.mock.calls[1]).toEqual(start.mock.calls[0])
})
