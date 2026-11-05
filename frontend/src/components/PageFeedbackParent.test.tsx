// @vitest-environment jsdom
import {webcrypto} from 'node:crypto'
import {afterEach,expect,it,vi} from 'vitest'
import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {QueryClient,QueryClientProvider} from '@tanstack/react-query'
import RoomResults from './RoomResults'
import {roomResultsApi} from '../api/roomResults'
import {eventsApi} from '../api/events'
import {useAuthStore} from '../stores/authStore'
const id='22222222-2222-4222-8222-222222222222',source='11111111-1111-4111-8111-111111111111'
const page=(version:number)=>({room_id:501,result_id:id,version,current_version:version,title:'Plan',kind:'page',author_agent_id:8,author_name_snapshot:'Bot',state:'draft',state_revision:1,linked_opinion_count:0,created_at:'2026-10-16T00:00:00Z',source:{event_id:source,seq:2,href:'/chat/501?seq=2'},execution:{trigger_event_id:null,round:1,generation:1,model_use:null},content:JSON.stringify({format:'gamja.page.v1',title:'Plan',datasets:[],views:[],blocks:[{type:'markdown',text:version===1?'Original':'Updated'}]}),file_ref:null,file_status:null,evidence:[],evidence_status:'none',review_support:'source_reply_only',link_report:null,state_owner_id:null,state_changed_at:null,link_report_owner_id:null,link_reported_at:null}) as any
afterEach(()=>{cleanup();vi.restoreAllMocks();sessionStorage.clear()})
it('keeps the opinion across RoomResults latest-version remount and requires section reselection',async()=>{
 Object.defineProperty(globalThis,'crypto',{value:webcrypto,configurable:true});useAuthStore.getState().setAuth('synthetic',{id:1,email:'test@example.invalid',username:'Owner'});const epoch=useAuthStore.getState().epoch
 vi.spyOn(roomResultsApi,'list').mockResolvedValue({room_id:501,items:[page(1)],next_cursor:null});vi.spyOn(roomResultsApi,'versions').mockResolvedValue({room_id:501,result_id:id,current_version:1,items:[page(1)],next_before_version:null});vi.spyOn(roomResultsApi,'detail').mockImplementation(async(_room,_id,v)=>page(v));vi.spyOn(roomResultsApi,'pageCurrent').mockResolvedValue(page(2));vi.spyOn(roomResultsApi,'pageConversation').mockResolvedValue({version:1,room_id:501,result_id:id,upper_seq:0,items:[],root_states:[],next_after_seq:null});vi.spyOn(eventsApi,'sendPage').mockRejectedValue({response:{status:409,data:{detail:{code:'result_page_version_changed'}}}})
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});render(<QueryClientProvider client={client}><RoomResults roomId={501} ownerId={1} authEpoch={epoch} active visible events={[]} source={null} onClearSource={()=>{}} onNavigate={()=>{}}/></QueryClientProvider>)
 fireEvent.click(await screen.findByRole('button',{name:/Plan.*Page/}));fireEvent.click(await screen.findByRole('button',{name:'Give feedback on this section'}));fireEvent.change(screen.getByLabelText('How should this section change?'),{target:{value:'Keep this draft'}});fireEvent.click(screen.getByRole('button',{name:'Send message'}));await screen.findByText('The page changed. Review the latest version before sending.');fireEvent.change(screen.getByLabelText('How should this section change?'),{target:{value:'Latest draft edit'}});fireEvent.click(await screen.findByRole('button',{name:'Open latest'}));await screen.findByText('Updated');expect((screen.getByRole('textbox',{name:/Message about Plan/}) as HTMLTextAreaElement).value).toBe('Latest draft edit');fireEvent.click(screen.getByRole('button',{name:'Give feedback on this section'}));expect((screen.getByLabelText('How should this section change?') as HTMLTextAreaElement).value).toBe('Latest draft edit')
})
