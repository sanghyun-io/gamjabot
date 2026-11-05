// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { I18nProvider } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import BrowserDemonstration from './BrowserDemonstration'
import { browserDemonstrationsApi, demonstrationDraftApi } from '../api/browserDemonstrations'
vi.mock('../api/browserDemonstrations',()=>({browserDemonstrationsApi:{list:vi.fn(),read:vi.fn(),updateReview:vi.fn(),start:vi.fn(),stop:vi.fn(),save:vi.fn(),command:vi.fn()},demonstrationDraftApi:{preflight:vi.fn(),submit:vi.fn(),read:vi.fn(),stop:vi.fn()}}))
const profile={id:'b'.repeat(32),name:'Profile',account_label:'',agent_ids:[8],origin_urls:['https://example.invalid'],revision:1,controller:'user' as const,agent_id:null,connected:true,last_used_at:null,expires_in_seconds:600}
const item={version:1 as const,demo_id:'a'.repeat(32),agent_id:8,profile_id:profile.id,profile_revision:1,review_revision:2,state:'stopped' as const,expires_in_seconds:0,goal:'Original',steps:['Click a button'],draft:{name:'Draft',description:'',content:'Original draft'},source_trust:'untrusted_hints' as const,review:{goal:'Saved goal',steps:['Saved step'],rules:'Saved rule',fields:{name:'Draft',description:'',content:'Saved draft'}}}
const mount=()=>render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><BrowserDemonstration roomId={501} profile={profile} agents={[{id:8,name:'Bot'}]} readOnly={false}/></MemoryRouter></I18nProvider>)
beforeEach(()=>{useAuthStore.getState().setAuth('synthetic',{id:1,email:'x@example.invalid',username:'Owner'});vi.mocked(browserDemonstrationsApi.list).mockResolvedValue({items:[item],next_after_demo_id:null,unavailable_count:0});vi.mocked(browserDemonstrationsApi.read).mockResolvedValue(item)})
afterEach(()=>{cleanup();sessionStorage.clear();useAuthStore.getState().logout();vi.resetAllMocks()})
it('restores a selected review and serializes edited content to the same revision',async()=>{
 vi.mocked(browserDemonstrationsApi.updateReview).mockImplementation(async(_room,_id,revision,review)=>({...item,review_revision:revision+1,review}))
 mount();fireEvent.click(await screen.findByRole('button',{name:'Saved goal · Ready to review'}));expect((await screen.findByLabelText('Instructions') as HTMLTextAreaElement).value).toBe('Saved draft')
 fireEvent.change(screen.getByLabelText('Decision, failure and approval rules'),{target:{value:'Changed rule'}})
 await waitFor(()=>expect(browserDemonstrationsApi.updateReview).toHaveBeenCalledWith(501,item.demo_id,2,expect.objectContaining({rules:'Changed rule'}),expect.any(String)),{timeout:2500})
 expect(await screen.findByText('Review saved on this Mac.')).toBeTruthy()
})
it('keeps local edits on a revision conflict until the owner compares them',async()=>{
 vi.mocked(browserDemonstrationsApi.updateReview).mockRejectedValue({response:{status:409,data:{detail:{code:'browser_demonstration_review_revision_conflict'}}}})
 mount();fireEvent.click(await screen.findByRole('button',{name:'Saved goal · Ready to review'}));fireEvent.change(await screen.findByLabelText('Decision, failure and approval rules'),{target:{value:'Keep mine'}})
 await screen.findByText('The saved review changed elsewhere.',{}, {timeout:2500});expect((screen.getByLabelText('Decision, failure and approval rules') as HTMLTextAreaElement).value).toBe('Keep mine')
 fireEvent.click(screen.getByRole('button',{name:'Check saved version'}));expect(await screen.findByText('Saved version 2')).toBeTruthy()
})
it('does not overwrite a locally edited review with an older model result',async()=>{
 vi.mocked(demonstrationDraftApi.preflight).mockResolvedValue({agent_id:8,review_revision:2,connection_id:2,connection_name:'Fixture',provider_id:'opencode-go',model_id:'fixture',preflight_token:'a'.repeat(64),max_requests:1,tools:false})
 vi.mocked(demonstrationDraftApi.submit).mockResolvedValue({})
 vi.mocked(demonstrationDraftApi.read).mockResolvedValue({event_id:'event',room_id:501,agent_id:8,review_revision:2,status:'completed',fields:{name:'AI',description:'',content:'Old model result'},failure_code:null})
 mount();fireEvent.click(await screen.findByRole('button',{name:'Saved goal · Ready to review'}));fireEvent.click(await screen.findByRole('button',{name:'Review model request'}));fireEvent.click(await screen.findByRole('checkbox',{name:'I reviewed this text and agree to send it to this model.'}));fireEvent.click(screen.getByRole('button',{name:'Prepare draft with this Bot'}))
 await waitFor(()=>expect(demonstrationDraftApi.submit).toHaveBeenCalledTimes(1));fireEvent.change(screen.getByLabelText('Instructions'),{target:{value:'My newer edit'}});fireEvent.click(screen.getByRole('button',{name:'Check original request'}))
 expect(await screen.findByText(/model result was not applied/i)).toBeTruthy();expect((screen.getByLabelText('Instructions') as HTMLTextAreaElement).value).toBe('My newer edit')
})
it('continues pagination when the first page has no matching profile',async()=>{
 const cursor='1'.repeat(32)
 vi.mocked(browserDemonstrationsApi.list).mockResolvedValueOnce({items:[],next_after_demo_id:cursor,unavailable_count:1}).mockResolvedValueOnce({items:[item],next_after_demo_id:null,unavailable_count:0})
 mount();expect(await screen.findByText('1 demonstrations could not be checked and are not shown.')).toBeTruthy()
 fireEvent.click(screen.getByRole('button',{name:'More demonstrations'}))
 await waitFor(()=>expect(browserDemonstrationsApi.list).toHaveBeenCalledWith(501,cursor))
 expect(await screen.findByRole('button',{name:'Saved goal · Ready to review'})).toBeTruthy()
})
