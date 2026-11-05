// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { I18nProvider } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import BrowserDemonstration from './BrowserDemonstration'
import { browserDemonstrationsApi, demonstrationDraftApi } from '../api/browserDemonstrations'
vi.mock('../api/browserDemonstrations', () => ({ browserDemonstrationsApi: { list:vi.fn(), updateReview:vi.fn(), start:vi.fn(), command:vi.fn(), read:vi.fn(), stop:vi.fn(), save:vi.fn() }, demonstrationDraftApi:{preflight:vi.fn(),submit:vi.fn(),read:vi.fn(),stop:vi.fn()} }))
const profile = {id:'b'.repeat(32),name:'Fixture',account_label:'',agent_ids:[8],origin_urls:['https://example.invalid'],revision:1,controller:'user' as const,agent_id:null,connected:true,last_used_at:null,expires_in_seconds:600}
const demo = {version:1 as const,demo_id:'a'.repeat(32),agent_id:8,profile_id:profile.id,profile_revision:1,review_revision:1,review:{goal:'Goal',steps:['Click a button'],rules:'',fields:{name:'Draft',description:'',content:'Review'}},state:'recording' as const,expires_in_seconds:600,goal:'Private goal',steps:[],draft:null,source_trust:'untrusted_hints' as const}
const element=()=> <I18nProvider systemLanguages={()=>['en']}><MemoryRouter><BrowserDemonstration roomId={501} profile={profile} agents={[{id:8,name:'Bot'}]} readOnly={false}/></MemoryRouter></I18nProvider>
beforeEach(()=>{vi.mocked(browserDemonstrationsApi.list).mockResolvedValue({items:[],next_after_demo_id:null,unavailable_count:0});vi.mocked(browserDemonstrationsApi.updateReview).mockImplementation(async(_room,_id,revision,review)=>({...demo,state:'stopped',review_revision:revision+1,review}));vi.mocked(browserDemonstrationsApi.read).mockResolvedValue({...demo,state:'stopped'})})
afterEach(()=>{cleanup();sessionStorage.clear();useAuthStore.getState().logout();vi.resetAllMocks()})
it('recovers the same start key after a reload without persisting the goal', async()=>{
 useAuthStore.getState().setAuth('synthetic', {id:1,email:'x@example.invalid',username:'Owner'})
 vi.mocked(browserDemonstrationsApi.start).mockRejectedValueOnce(new Error('lost'))
 vi.mocked(browserDemonstrationsApi.command).mockResolvedValue(demo)
 const first=render(element());fireEvent.change(screen.getByLabelText('Goal'),{target:{value:'Private goal'}});fireEvent.click(screen.getByRole('button',{name:'Start demonstration'}))
 await screen.findByText(/start response is unknown/i)
 const stored=Object.values(sessionStorage).join(' ');expect(stored).not.toContain('Private goal')
 const key=vi.mocked(browserDemonstrationsApi.start).mock.calls[0][2];first.unmount();render(element())
 fireEvent.click(screen.getByRole('button',{name:'Check original request'}))
 await waitFor(()=>expect(browserDemonstrationsApi.command).toHaveBeenCalledWith(501,key))
 expect(await screen.findByText(/Recording hints/)).toBeTruthy()
})

it('recovers a stopped draft after a lost stop response and allows editing', async()=>{
 useAuthStore.getState().setAuth('synthetic', {id:1,email:'x@example.invalid',username:'Owner'})
 vi.mocked(browserDemonstrationsApi.start).mockResolvedValue(demo)
 vi.mocked(browserDemonstrationsApi.stop).mockRejectedValue(new Error('lost'))
 vi.mocked(browserDemonstrationsApi.read).mockResolvedValue({...demo,state:'stopped',expires_in_seconds:0,review:{...demo.review,fields:{name:'Draft',description:'',content:'Review first'}},draft:{name:'Draft',description:'',content:'Review first'}})
 render(element());fireEvent.change(screen.getByLabelText('Goal'),{target:{value:'Goal'}});fireEvent.click(screen.getByRole('button',{name:'Start demonstration'}))
 fireEvent.click(await screen.findByRole('button',{name:'Stop and review'}))
 await screen.findByText('Could not confirm the result. Check the browser or request and try again.')
 fireEvent.click(screen.getByRole('button',{name:'Check original request'}))
 const input=await screen.findByLabelText('Instructions') as HTMLTextAreaElement
 expect(input.value).toBe('Review first');fireEvent.change(input,{target:{value:'Edited'}});expect(input.value).toBe('Edited')
})

it('allows a new demonstration after interruption', async()=>{
 useAuthStore.getState().setAuth('synthetic', {id:1,email:'x@example.invalid',username:'Owner'})
 vi.mocked(browserDemonstrationsApi.start).mockResolvedValue({...demo,state:'interrupted',expires_in_seconds:0})
 render(element());fireEvent.change(screen.getByLabelText('Goal'),{target:{value:'Goal'}});fireEvent.click(screen.getByRole('button',{name:'Start demonstration'}))
 fireEvent.click(await screen.findByRole('button',{name:'Prepare a new demonstration'}))
 expect(screen.getByRole('button',{name:'Start demonstration'})).toBeTruthy()
})

it('keeps target names opt-in and lets the owner review meaning and rules', async()=>{
 useAuthStore.getState().setAuth('synthetic', {id:1,email:'x@example.invalid',username:'Owner'})
 vi.mocked(browserDemonstrationsApi.start).mockResolvedValue({...demo,steps:['Click a button: Continue','Enter a value (not recorded)']})
 vi.mocked(browserDemonstrationsApi.stop).mockResolvedValue({...demo,state:'stopped',expires_in_seconds:0,steps:['Click a button: Continue','Enter a value (not recorded)'],review:{...demo.review,steps:['Click a button: Continue','Enter a value (not recorded)']},draft:{name:'Draft',description:'',content:'Review'}})
 render(element());fireEvent.change(screen.getByLabelText('Goal'),{target:{value:'Goal'}})
 const option=screen.getByRole('checkbox',{name:'Also record names of buttons, links and ordinary input fields'}) as HTMLInputElement
 expect(option.checked).toBe(false);fireEvent.click(option);fireEvent.click(screen.getByRole('button',{name:'Start demonstration'}))
 await waitFor(()=>expect(browserDemonstrationsApi.start).toHaveBeenCalledWith(501,expect.objectContaining({include_target_labels:true}),expect.any(String)))
 fireEvent.click(await screen.findByRole('button',{name:'Stop and review'}))
 expect(await screen.findByText('Click a button · Continue')).toBeTruthy()
 const meanings=screen.getAllByLabelText('What this step means');fireEvent.change(meanings[0],{target:{value:'Choose the next step'}})
 fireEvent.change(screen.getByLabelText('Decision, failure and approval rules'),{target:{value:'Stop on failure'}})
 expect((meanings[0] as HTMLInputElement).value).toBe('Choose the next step')
})


it('requires model review and consent and only applies the returned editable draft', async()=>{
 useAuthStore.getState().setAuth('synthetic', {id:1,email:'x@example.invalid',username:'Owner'})
 vi.mocked(browserDemonstrationsApi.start).mockResolvedValue(demo)
 vi.mocked(browserDemonstrationsApi.stop).mockResolvedValue({...demo,state:'stopped',expires_in_seconds:0,steps:['Click a button'],draft:{name:'Draft',description:'',content:'Review'}})
 vi.mocked(demonstrationDraftApi.preflight).mockResolvedValue({agent_id:8,review_revision:1,connection_id:2,connection_name:'Fixture connection',provider_id:'opencode-go',model_id:'fixture-model',preflight_token:'a'.repeat(64),max_requests:1,tools:false})
 vi.mocked(demonstrationDraftApi.submit).mockResolvedValue({})
 vi.mocked(demonstrationDraftApi.read).mockResolvedValue({event_id:'event',room_id:501,agent_id:8,status:'completed',review_revision:1,fields:{name:'Model draft',description:'',content:'Editable'},failure_code:null})
 render(element());fireEvent.change(screen.getByLabelText('Goal'),{target:{value:'Goal'}});fireEvent.click(screen.getByRole('button',{name:'Start demonstration'}));fireEvent.click(await screen.findByRole('button',{name:'Stop and review'}))
 fireEvent.click(await screen.findByRole('button',{name:'Review model request'}))
 const send=await screen.findByRole('button',{name:'Prepare draft with this Bot'}) as HTMLButtonElement
 expect(send.disabled).toBe(true);expect(screen.getByText(/fixture-model/)).toBeTruthy()
 fireEvent.click(screen.getByRole('checkbox',{name:'I reviewed this text and agree to send it to this model.'}));fireEvent.click(send)
 await waitFor(()=>expect(demonstrationDraftApi.submit).toHaveBeenCalledWith(501,demo.demo_id,expect.objectContaining({reviewed_goal:'Goal',reviewed_steps:['Click a button'],consent:true}),expect.any(String)))
 fireEvent.click(await screen.findByRole('button',{name:'Check original request'}))
 expect((await screen.findByLabelText('Instructions') as HTMLTextAreaElement).value).toBe('Editable')
 expect(browserDemonstrationsApi.save).not.toHaveBeenCalled()
})

it('stops only the same draft request and retains uncertain results', async()=>{
 useAuthStore.getState().setAuth('synthetic', {id:1,email:'x@example.invalid',username:'Owner'})
 vi.mocked(browserDemonstrationsApi.start).mockResolvedValue(demo)
 vi.mocked(browserDemonstrationsApi.stop).mockResolvedValue({...demo,state:'stopped',expires_in_seconds:0,steps:['Click a button'],draft:{name:'Draft',description:'',content:'Review'}})
 vi.mocked(demonstrationDraftApi.preflight).mockResolvedValue({agent_id:8,review_revision:1,connection_id:2,connection_name:'Fixture connection',provider_id:'opencode-go',model_id:'fixture-model',preflight_token:'a'.repeat(64),max_requests:1,tools:false})
 vi.mocked(demonstrationDraftApi.submit).mockResolvedValue({})
 vi.mocked(demonstrationDraftApi.stop).mockResolvedValue({event_id:'event',room_id:501,agent_id:8,status:'uncertain',review_revision:1,fields:null,failure_code:null})
 render(element());fireEvent.change(screen.getByLabelText('Goal'),{target:{value:'Goal'}});fireEvent.click(screen.getByRole('button',{name:'Start demonstration'}));fireEvent.click(await screen.findByRole('button',{name:'Stop and review'}));fireEvent.click(await screen.findByRole('button',{name:'Review model request'}));fireEvent.click(await screen.findByRole('checkbox',{name:'I reviewed this text and agree to send it to this model.'}));fireEvent.click(screen.getByRole('button',{name:'Prepare draft with this Bot'}))
 const stop=await screen.findByRole('button',{name:'Stop this draft request'});fireEvent.click(stop)
 await waitFor(()=>expect(demonstrationDraftApi.stop).toHaveBeenCalledWith(501,demo.demo_id,vi.mocked(demonstrationDraftApi.submit).mock.calls[0][3]))
 expect(await screen.findByText(/may have run, but its result is unknown/)).toBeTruthy()
 expect(demonstrationDraftApi.submit).toHaveBeenCalledTimes(1)
})
