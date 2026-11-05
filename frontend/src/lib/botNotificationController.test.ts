// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const mocks=vi.hoisted(()=>({auth:{token:'token',user:{id:1},epoch:7},status:vi.fn(),resolve:vi.fn(),session:vi.fn(),next:vi.fn(),ready:vi.fn(),finish:vi.fn(),available:vi.fn(),nativeStatus:vi.fn(),take:vi.fn(),deliver:vi.fn()}))
vi.mock('../stores/authStore',()=>({useAuthStore:{getState:()=>mocks.auth}}))
vi.mock('../api/botNotifications',()=>({botNotificationsApi:{status:mocks.status,resolve:mocks.resolve,session:mocks.session,next:mocks.next,ready:mocks.ready,finish:mocks.finish},notificationNative:{available:mocks.available,status:mocks.nativeStatus,take:mocks.take,deliver:mocks.deliver}}))
import { startBotNotifications } from './botNotificationController'
const id='00000000-0000-4000-8000-000000000001',epoch='a'.repeat(64)
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()}
beforeEach(()=>{vi.useFakeTimers();vi.clearAllMocks();mocks.auth={token:'token',user:{id:1},epoch:7};mocks.available.mockReturnValue(true);mocks.nativeStatus.mockResolvedValue('authorized');mocks.take.mockResolvedValue(id);mocks.status.mockResolvedValue({owner_id:1,owner_epoch:epoch});mocks.resolve.mockRejectedValue(Error('offline'))})
afterEach(()=>vi.useRealTimers())
it('keeps a consumed click for explicit recovery when destination lookup fails',async()=>{
 const open=vi.fn(),problem=vi.fn(),unresolved=vi.fn();const stop=startBotNotifications(1,7,open,problem,unresolved)
 await flush();expect(mocks.take).toHaveBeenCalledTimes(1);expect(unresolved).toHaveBeenCalledWith(id,epoch);expect(problem).toHaveBeenCalledOnce();expect(open).not.toHaveBeenCalled()
 await vi.advanceTimersByTimeAsync(60000);expect(mocks.resolve).toHaveBeenCalledTimes(1);stop()
})
it('retains the click when owner status is unavailable before the epoch is known',async()=>{
 mocks.status.mockRejectedValue(Error('offline'));const unresolved=vi.fn();const stop=startBotNotifications(1,7,vi.fn(),vi.fn(),unresolved)
 await flush();expect(unresolved).toHaveBeenCalledWith(id,null);expect(mocks.resolve).not.toHaveBeenCalled();stop()
})
it('does not expose a click after the signed-in identity changes during lookup',async()=>{
 let reject!:(reason:Error)=>void;mocks.status.mockImplementation(()=>new Promise((_,r)=>{reject=r}))
 const unresolved=vi.fn(),problem=vi.fn();const stop=startBotNotifications(1,7,vi.fn(),problem,unresolved);await flush()
 mocks.auth={token:'other',user:{id:2},epoch:8};reject(Error('offline'));await flush();expect(unresolved).not.toHaveBeenCalled();expect(problem).not.toHaveBeenCalled();stop()
})
