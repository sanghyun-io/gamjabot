// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest'
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {QueryClient,QueryClientProvider} from '@tanstack/react-query'
import {MemoryRouter} from 'react-router-dom'
import Agents from './Agents'
import {agentsApi} from '../api/agents'
import {connectionsApi} from '../api/connections'
import {botDirectoryApi} from '../api/botDirectory'
import {routinesApi,type Routine} from '../api/routines'
import {useAuthStore} from '../stores/authStore'
const row:Routine={version:1,routine_id:'00000000-0000-4000-8000-000000000001',revision:1,agent_id:7,room_id:501,name:'Synthetic schedule',instruction:'Synthetic',enabled:true,schedule:{kind:'weekly',weekdays:[0],hour:9,minute:0,timezone:'Asia/Seoul'},next_run_at:'2026-11-05T00:00:00Z',availability:'available'}
afterEach(()=>{cleanup();vi.restoreAllMocks()})
it('retries directory failure before moving into the one existing Bot routine manager',async()=>{
 HTMLElement.prototype.scrollIntoView=vi.fn();useAuthStore.getState().setAuth('synthetic',{id:1,email:'preview@example.invalid',username:'Preview'})
 vi.spyOn(agentsApi,'list').mockResolvedValue([{id:7,user_id:1,name:'Ada',description:'test',system_prompt:'x',connection_id:1,model_id:'m',model:'m',temperature:'auto',max_tokens:0,is_active:true,created_at:'2026-01-01',updated_at:null}]);vi.spyOn(connectionsApi,'list').mockResolvedValue([])
 const directory=vi.spyOn(botDirectoryApi,'read').mockRejectedValueOnce(Error('offline')).mockResolvedValue({version:1,revision:0,sections:[],items:[]});vi.spyOn(routinesApi,'list').mockResolvedValue([row])
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}});render(<QueryClientProvider client={client}><MemoryRouter><Agents/></MemoryRouter></QueryClientProvider>)
 fireEvent.click(await screen.findByRole('button',{name:'Open agenda'}));await screen.findByText('Synthetic schedule');expect((screen.getByRole('button',{name:'Manage Ada routines'}) as HTMLButtonElement).disabled).toBe(true)
 fireEvent.click(screen.getByRole('button',{name:'Retry Bot directory'}));await waitFor(()=>expect(directory).toHaveBeenCalledTimes(2));const manage=await screen.findByRole('button',{name:'Manage Ada routines'});await waitFor(()=>expect((manage as HTMLButtonElement).disabled).toBe(false));fireEvent.click(manage)
 await waitFor(()=>{const details=document.querySelectorAll('details.bot-routines');expect(details).toHaveLength(1);expect((details[0] as HTMLDetailsElement).open).toBe(true)})
})
