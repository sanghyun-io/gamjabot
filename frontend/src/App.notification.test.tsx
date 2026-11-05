// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest'
import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {MemoryRouter,useLocation} from 'react-router-dom'
const m=vi.hoisted(()=>({state:{token:'token',user:{id:1},epoch:7},callbacks:null as null|{ok:(item:any,epoch:string)=>void,unresolved:(id:string,epoch:string|null)=>void},status:vi.fn(),resolve:vi.fn()}))
vi.mock('./stores/authStore',()=>({useAuthStore:Object.assign((f:any)=>f(m.state),{getState:()=>m.state})}))
vi.mock('./lib/botNotificationController',()=>({notificationHref:(item:any)=>`/chat/${item.room_id}`,startBotNotifications:(_o:any,_e:any,ok:any,_p:any,unresolved:any)=>{m.callbacks={ok,unresolved};return()=>{}}}))
vi.mock('./api/botNotifications',()=>({botNotificationsApi:{status:m.status,resolve:m.resolve}}))
vi.mock('./lib/localHost',()=>({getLocalEndpoint:vi.fn().mockRejectedValue(Error('not started'))}))
vi.mock('./lib/api',()=>({default:{get:vi.fn().mockRejectedValue(Error('offline')),post:vi.fn().mockRejectedValue(Error('offline'))}}))
vi.mock('./components/AttentionInbox',()=>({default:()=>null}))
vi.mock('./components/QuickRoomSwitcher',()=>({default:()=>null}))
vi.mock('./components/KeyboardHelp',()=>({default:()=>null}))
vi.mock('./components/AppMenu',()=>({default:()=>null}))
vi.mock('./components/LegacyAuthStorageNotice',()=>({default:()=>null}))
vi.mock('./components/VoiceCall',()=>({VoiceCallPanel:()=>null}))
vi.mock('./lib/voiceCallController',()=>({voiceCallController:{snapshot:()=>({target:null}),suspend:()=>{},resetOwner:()=>{}}}))
vi.mock('./lib/templateLinks',()=>({subscribeTemplateLinks:async()=>()=>{}}))
import {ReportingRoot} from './App'
const epoch='a'.repeat(64),id='00000000-0000-4000-8000-000000000001'
function Probe(){const l=useLocation();return <output data-testid="path">{l.pathname}</output>}
function deferred<T>(){let resolve!:(x:T)=>void;const promise=new Promise<T>(r=>{resolve=r});return {promise,resolve}}
afterEach(()=>{cleanup();vi.clearAllMocks();m.callbacks=null;m.state={token:'token',user:{id:1},epoch:7}})
it('does not navigate after a pending click is dismissed during destination lookup',async()=>{
 const later=deferred<any>();m.status.mockResolvedValue({owner_id:1,owner_epoch:epoch});m.resolve.mockReturnValue(later.promise)
 render(<MemoryRouter initialEntries={['/login']}><Probe/><ReportingRoot/></MemoryRouter>)
 await waitFor(()=>expect(m.callbacks).not.toBeNull());act(()=>m.callbacks!.unresolved(id,epoch))
 fireEvent.click(screen.getByRole('button',{name:'Open notification'}));await waitFor(()=>expect(m.resolve).toHaveBeenCalledOnce())
 fireEvent.click(screen.getByRole('button',{name:'Dismiss'}));await act(async()=>later.resolve({room_id:42}))
 expect(screen.getByTestId('path').textContent).toBe('/login');expect(screen.queryByRole('button',{name:'Open notification'})).toBeNull()
})
it('does not navigate to an older notification after a new click succeeds',async()=>{
 const later=deferred<any>();m.status.mockResolvedValue({owner_id:1,owner_epoch:epoch});m.resolve.mockReturnValue(later.promise)
 render(<MemoryRouter initialEntries={['/login']}><Probe/><ReportingRoot/></MemoryRouter>)
 await waitFor(()=>expect(m.callbacks).not.toBeNull());act(()=>m.callbacks!.unresolved(id,epoch))
 fireEvent.click(screen.getByRole('button',{name:'Open notification'}));await waitFor(()=>expect(m.resolve).toHaveBeenCalledOnce())
 act(()=>m.callbacks!.ok({room_id:99},epoch));await act(async()=>later.resolve({room_id:42}))
 expect(screen.getByTestId('path').textContent).toBe('/chat/99')
})
it('opens the same unresolved notification only after the explicit button',async()=>{
 m.status.mockResolvedValue({owner_id:1,owner_epoch:epoch});m.resolve.mockResolvedValue({room_id:42})
 render(<MemoryRouter initialEntries={['/login']}><Probe/><ReportingRoot/></MemoryRouter>)
 await waitFor(()=>expect(m.callbacks).not.toBeNull());act(()=>m.callbacks!.unresolved(id,epoch));expect(m.resolve).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button',{name:'Open notification'}));await waitFor(()=>expect(screen.getByTestId('path').textContent).toBe('/chat/42'))
 expect(m.resolve).toHaveBeenCalledWith(id,epoch)
})
it('clears an unresolved click when the signed-in identity changes',async()=>{
 const view=render(<MemoryRouter initialEntries={['/login']}><Probe/><ReportingRoot/></MemoryRouter>)
 await waitFor(()=>expect(m.callbacks).not.toBeNull());act(()=>m.callbacks!.unresolved(id,epoch));expect(screen.getByRole('button',{name:'Open notification'})).toBeTruthy()
 m.state={token:'other',user:{id:2},epoch:8};view.rerender(<MemoryRouter initialEntries={['/login']}><Probe/><ReportingRoot/></MemoryRouter>)
 await waitFor(()=>expect(screen.queryByRole('button',{name:'Open notification'})).toBeNull());expect(m.resolve).not.toHaveBeenCalled()
})
