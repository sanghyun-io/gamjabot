// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import BotLibrary from './BotLibrary'
import { botLibraryApi, validLibraryPage, type BotLibraryItem } from '../api/botLibrary'
import { libraryUploadsApi } from '../api/libraryUploads'
import { agentsApi } from '../api/agents'
import { useAuthStore } from '../stores/authStore'
import { I18nProvider } from '../i18n'
const rid='af041000-0000-4000-8000-000000000801',source='af041000-0000-4000-8000-000000000810'
const item:BotLibraryItem={result_id:rid,room_id:501,version:2,kind:'text',title:'Saved note',author_agent_id:8,author_name_snapshot:'Bot',state:'draft',state_revision:1,created_at:'2026-10-30T00:00:00Z',updated_at:'2026-10-30T00:00:00Z',linked_opinion_count:0,source_href:`/chat/501?seq=1&event=${source}`,detail_href:`/conversations/501/results/${rid}/versions/2`}
const page={agent_id:8,items:[item],next_cursor:null}
afterEach(()=>{cleanup();vi.restoreAllMocks()})
it('checks Bot identity and internal result/source paths',()=>{
 expect(validLibraryPage(page,8)).toBe(true)
 expect(validLibraryPage({...page,items:[{...item,author_agent_id:9}]},8)).toBe(false)
 expect(validLibraryPage({...page,items:[{...item,source_href:'https://example.invalid/'}]},8)).toBe(false)
 expect(validLibraryPage({...page,items:[{...item,detail_href:'/conversations/502/results/other'}]},8)).toBe(false)
})
it('shows saved versions and exact source, then hides cached rows when access is revoked',async()=>{
 useAuthStore.getState().setAuth('synthetic',{id:1,email:'owner@example.invalid',username:'Owner'})
 vi.spyOn(libraryUploadsApi,'list').mockResolvedValue({bot_id:8,items:[],next_cursor:null})
 vi.spyOn(agentsApi,'get').mockResolvedValue({id:8,user_id:1,name:'Bot'} as never)
 const list=vi.spyOn(botLibraryApi,'list').mockResolvedValue(page)
 const client=new QueryClient({defaultOptions:{queries:{retry:false}}})
 render(<I18nProvider systemLanguages={()=>['en']}><QueryClientProvider client={client}><MemoryRouter initialEntries={['/agents/8/library']}><Routes><Route path="/agents/:agentId/library" element={<BotLibrary/>}/></Routes></MemoryRouter></QueryClientProvider></I18nProvider>)
 expect(await screen.findByText('Saved note')).toBeTruthy()
 expect(screen.getByRole('link',{name:'View result and versions'}).getAttribute('href')).toBe(`/chat/501?result=${rid}&version=2`)
 expect(screen.getByRole('link',{name:'View original conversation'}).getAttribute('href')).toBe(`/chat/501?seq=1&event=${source}`)
 list.mockRejectedValue({response:{status:404}})
 await client.invalidateQueries({queryKey:['bot-library']})
 expect(await screen.findByRole('alert')).toBeTruthy()
 expect(screen.queryByText('Saved note')).toBeNull()
})
