import type {Meta,StoryObj} from '@storybook/react-vite'
import {useLayoutEffect,useState} from 'react'
import userEvent from '@testing-library/user-event'
import {within} from '@testing-library/react'
import './networkGuard'
import '../src/index.css'
import {I18nProvider} from '../src/i18n'
import PageWorkspace from '../src/components/PageWorkspace'
import {useAuthStore} from '../src/stores/authStore'
import {eventsApi} from '../src/api/events'
import {roomResultsApi} from '../src/api/roomResults'
const id='af041000-0000-4000-8000-000000000011'
const doc=(text:string)=>JSON.stringify({format:'gamja.page.v1',title:'Synthetic report',datasets:[],views:[],blocks:[{type:'heading',level:1,text:'Summary'},{type:'markdown',text}]})
function Surface({scenario}:{scenario:'ready'|'changed'|'unknown'}) {
 const [ready,setReady]=useState(false),[changed,setChanged]=useState(false),[events,setEvents]=useState<any[]>([])
 useLayoutEffect(()=>{
  const old=useAuthStore.getState(),history=roomResultsApi.pageConversation,send=eventsApi.sendPage,receipt=eventsApi.pageReceipt
  useAuthStore.setState({user:{id:1,email:'test@example.invalid',username:'Preview'},epoch:1})
  sessionStorage.removeItem(`page-message:1:501:${id}`)
  roomResultsApi.pageConversation=async()=>({version:1,room_id:501,result_id:id,upper_seq:0,items:[],root_states:[],next_after_seq:null})
  let saved:any=null
  eventsApi.sendPage=async(room,content,target,key,selection)=>{saved={event_id:'af041000-0000-4000-8000-000000000822',room_id:room,seq:1,device_id:null,actor_type:'user',actor_id:1,actor_name_snapshot:'Preview',kind:'message',payload:{content,page_target:target,...(selection?{page_selection:selection}:{})},created_at:'2026-11-04T00:00:00Z',idempotency_key:key};if(scenario==='unknown')throw Error('Synthetic response unknown');return saved}
  eventsApi.pageReceipt=async()=>{if(!saved)throw Error('Synthetic receipt missing');return saved}
  setReady(true)
  return()=>{useAuthStore.setState({user:old.user,epoch:old.epoch});roomResultsApi.pageConversation=history;eventsApi.sendPage=send;eventsApi.pageReceipt=receipt;sessionStorage.removeItem(`page-message:1:501:${id}`)}
 },[scenario])
 const value={room_id:501,result_id:id,version:1,current_version:1,title:'Synthetic report',content:doc(changed?'Updated paragraph':'Original paragraph'),author_name_snapshot:'Bot'} as any
 return <I18nProvider><main className="page-frame page-content"><p>합성 데이터 · 실제 계정이나 모델을 사용하지 않습니다.</p>{scenario==='changed'&&<button type="button" onClick={()=>setChanged(true)}>Simulate Page change</button>}{ready&&<PageWorkspace value={value} ownerId={1} authEpoch={1} active canUse={()=>true} events={events} actions={[]} onNavigate={()=>{}} onEvent={event=>setEvents(old=>[...old,event])} onLatest={()=>{}}/>}</main></I18nProvider>
}
const meta={title:'Product/Page section feedback',component:Surface} satisfies Meta<typeof Surface>
export default meta
type Story=StoryObj<typeof meta>
export const Ready:Story={args:{scenario:'ready'}}
const select = async (canvasElement:HTMLElement) => { const canvas=within(canvasElement);await userEvent.click((await canvas.findAllByRole('button',{name:/Give feedback on this section|이 부분에 의견 남기기/}))[1]);await userEvent.type(canvas.getByRole('textbox',{name:/How should this section change|이 부분을 어떻게 바꿀까요/}),'Shorten this paragraph');return canvas }
export const Changed:Story={args:{scenario:'changed'},play:async({canvasElement})=>{const canvas=await select(canvasElement);await userEvent.click(canvas.getByRole('button',{name:'Simulate Page change'}))}}
export const Unknown:Story={args:{scenario:'unknown'},play:async({canvasElement})=>{const canvas=await select(canvasElement);await userEvent.click(canvas.getByRole('button',{name:/Send message|보내기/}))}}

