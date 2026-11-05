// @vitest-environment jsdom
import { StrictMode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { I18nProvider } from '../i18n'
import BrowserProfileSharingDialog from './BrowserProfileSharingDialog'
import { browserProfileSharingApi } from '../api/browserProfileSharing'
vi.mock('../api/browserProfileSharing',()=>({browserProfileSharingApi:{read:vi.fn(),save:vi.fn()}}))
const profile={id:'a'.repeat(32),name:'Test profile',account_label:'',agent_ids:[1],origin_urls:['https://example.invalid'],revision:1,controller:'disconnected' as const,agent_id:null,connected:false,last_used_at:null,expires_in_seconds:0}
const base={profile_id:profile.id,home_room_id:1,revision:1,allow_delegated:false,bindings:[{room_id:2,agent_ids:[3,4]}],rooms:[{room_id:2,title:'Review room',agents:[{id:3,name:'Ada',available:true}]}]}
const renderDialog=(current=profile)=>render(<I18nProvider systemLanguages={()=>['en']}><BrowserProfileSharingDialog roomId={1} target={profile} current={current} canUse={()=>true} onClose={()=>{}} onSaved={()=>{}}/></I18nProvider>)
afterEach(()=>{cleanup();vi.resetAllMocks()})
it('requires removal of unavailable bindings before explicit confirmation',async()=>{
 vi.mocked(browserProfileSharingApi.read).mockResolvedValue(base);vi.mocked(browserProfileSharingApi.save).mockResolvedValue({})
 renderDialog();const missing=await screen.findByRole('checkbox',{name:'Bot #4 · Unavailable'});expect((missing as HTMLInputElement).checked).toBe(true)
 expect(screen.getByRole('button',{name:'Save'})).toHaveProperty('disabled',true);fireEvent.click(missing)
 fireEvent.click(screen.getByRole('checkbox',{name:'I reviewed which Bots can use this login.'}));fireEvent.click(screen.getByRole('button',{name:'Save'}))
 await waitFor(()=>expect(browserProfileSharingApi.save).toHaveBeenCalledWith(1,profile.id,1,[{room_id:2,agent_ids:[3]}],false,expect.any(AbortSignal)))
})
it('does not repeat a lost save and keeps choices while comparing a newer revision',async()=>{
 vi.mocked(browserProfileSharingApi.read).mockResolvedValueOnce({...base,bindings:[{room_id:2,agent_ids:[3]}]}).mockResolvedValueOnce({...base,revision:2,bindings:[]})
 vi.mocked(browserProfileSharingApi.save).mockRejectedValue(new Error('lost'))
 const rendered=renderDialog();fireEvent.click(await screen.findByRole('checkbox',{name:'I reviewed which Bots can use this login.'}));fireEvent.click(screen.getByRole('button',{name:'Save'}))
 await screen.findByText(/save result is unknown/i);fireEvent.click(screen.getByRole('button',{name:'Check current sharing'}));await screen.findByText('Current saved sharing')
 expect((screen.getByRole('checkbox',{name:'Ada'}) as HTMLInputElement).checked).toBe(true);expect(browserProfileSharingApi.save).toHaveBeenCalledTimes(1)
 rendered.rerender(<I18nProvider systemLanguages={()=>['en']}><BrowserProfileSharingDialog roomId={1} target={profile} current={{...profile,revision:2}} canUse={()=>true} onClose={()=>{}} onSaved={()=>{}}/></I18nProvider>)
 fireEvent.click(screen.getByRole('button',{name:'Use current revision while keeping my choices'}));expect(screen.getByRole('button',{name:'Save'})).toHaveProperty('disabled',true);vi.mocked(browserProfileSharingApi.save).mockResolvedValue({});fireEvent.click(screen.getByRole('checkbox',{name:'I reviewed which Bots can use this login.'}));fireEvent.click(screen.getByRole('button',{name:'Save'}));await waitFor(()=>expect(browserProfileSharingApi.save).toHaveBeenLastCalledWith(1,profile.id,2,[{room_id:2,agent_ids:[3]}],false,expect.any(AbortSignal)))
})
it('keeps delegated opt-in local through an unknown save and explicit adoption',async()=>{
 vi.mocked(browserProfileSharingApi.read).mockResolvedValueOnce({...base,bindings:[{room_id:2,agent_ids:[3]}]}).mockResolvedValueOnce({...base,revision:2,allow_delegated:false,bindings:[]})
 vi.mocked(browserProfileSharingApi.save).mockRejectedValueOnce(new Error('lost')).mockResolvedValueOnce({})
 const rendered=renderDialog();const option=await screen.findByRole('checkbox',{name:'Allow this login profile for work received from other Bots'}) as HTMLInputElement
 expect(option.checked).toBe(false);fireEvent.click(screen.getByRole('checkbox',{name:'I reviewed which Bots can use this login.'}));fireEvent.click(option)
 expect(screen.getByRole('button',{name:'Save'})).toHaveProperty('disabled',true);fireEvent.click(screen.getByRole('checkbox',{name:'I reviewed which Bots can use this login.'}));fireEvent.click(screen.getByRole('button',{name:'Save'}))
 await screen.findByText(/save result is unknown/i);const local=screen.getByRole('checkbox',{name:'Allow this login profile for work received from other Bots'}) as HTMLInputElement;expect(local.checked).toBe(true);expect(local.disabled).toBe(true);fireEvent.click(screen.getByRole('button',{name:'Check current sharing'}));expect(await screen.findByText(/Not allowed/)).toBeTruthy()
 rendered.rerender(<I18nProvider systemLanguages={()=>['en']}><BrowserProfileSharingDialog roomId={1} target={profile} current={{...profile,revision:2}} canUse={()=>true} onClose={()=>{}} onSaved={()=>{}}/></I18nProvider>)
 fireEvent.click(screen.getByRole('button',{name:'Use current revision while keeping my choices'}));expect((screen.getByRole('checkbox',{name:'Allow this login profile for work received from other Bots'}) as HTMLInputElement).checked).toBe(true)
 fireEvent.click(screen.getByRole('checkbox',{name:'I reviewed which Bots can use this login.'}));fireEvent.click(screen.getByRole('button',{name:'Save'}))
 await waitFor(()=>expect(browserProfileSharingApi.save).toHaveBeenLastCalledWith(1,profile.id,2,[{room_id:2,agent_ids:[3]}],true,expect.any(AbortSignal)))
})


it('creates a fresh read controller on StrictMode effect reconnection',async()=>{
 const signals:AbortSignal[]=[]
 vi.mocked(browserProfileSharingApi.read).mockImplementation((_room,_id,signal)=>{signals.push(signal!);if(signals.length===1)return new Promise((_resolve,reject)=>signal!.addEventListener('abort',()=>reject(new Error('aborted'))));return Promise.resolve({...base,bindings:[{room_id:2,agent_ids:[3]}]})})
 render(<StrictMode><I18nProvider systemLanguages={()=>['en']}><BrowserProfileSharingDialog roomId={1} target={profile} current={profile} canUse={()=>true} onClose={()=>{}} onSaved={()=>{}}/></I18nProvider></StrictMode>)
 expect(await screen.findByRole('checkbox',{name:'Ada'})).toBeTruthy();expect(signals).toHaveLength(2);expect(signals[0].aborted).toBe(true);expect(signals[1].aborted).toBe(false)
 expect(screen.queryByText('Could not load sharing choices.')).toBeNull()
})
