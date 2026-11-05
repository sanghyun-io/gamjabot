// @vitest-environment jsdom
import {afterEach,expect,it,vi} from 'vitest'
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import MessageDeliveryNotice from './MessageDeliveryNotice'
import {useAuthStore} from '../stores/authStore'
import {directoryViewGeneration} from '../lib/directoryViewMemory'
import {freezePendingMessage,type PendingRoomMessage} from '../lib/pendingRoomMessages'
afterEach(()=>cleanup())
it('checks routine resolution without auto-resend or parking an uncertain request',async()=>{
 useAuthStore.getState().setAuth('synthetic',{id:1,email:'test@example.invalid',username:'Preview'});const epoch=useAuthStore.getState().epoch,generation=directoryViewGeneration(),ref={agent_id:1,routine_id:'00000000-0000-4000-8000-000000000001',revision:1};const item:PendingRoomMessage={identity:`1:${epoch}`,roomId:501,key:'00000000-0000-4000-8000-000000000002',content:'Synthetic',sourceContent:'Synthetic',sourceEditId:'x',outputKind:'auto',routineRef:ref,draftVersion:1,afterSeq:0,status:'unknown',parked:false};freezePendingMessage(item,generation)
 const retry=vi.fn(),check=vi.fn(async()=> 'pending' as const),park=vi.fn();render(<MessageDeliveryNotice items={[item]} ownerId={1} epoch={epoch} generation={generation} busy={false} onResolveRoutine={check} onRetry={retry} onPark={park} onEvents={()=>{}} onFinish={()=>{}} onView={()=>{}} onFocusComposer={()=>{}}/>);fireEvent.click(screen.getByRole('button',{name:/Check/i}));await waitFor(()=>expect(check).toHaveBeenCalledWith(item));expect(screen.getByText(/remains unconfirmed/)).toBeTruthy();expect(retry).not.toHaveBeenCalled();expect(park).not.toHaveBeenCalled();expect(screen.queryByRole('button',{name:/park/i})).toBeNull()
})
