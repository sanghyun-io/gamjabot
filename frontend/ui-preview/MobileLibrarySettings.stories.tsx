import type { Meta, StoryObj } from '@storybook/react-vite'
import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import './networkGuard'
import '../src/index.css'
import { I18nProvider } from '../src/i18n'
import { useAuthStore } from '../src/stores/authStore'
import { mobileAccessApi, remoteApi } from '../src/api/remote'
import Devices from '../src/pages/Devices'
function SettingsStory({unknown}:{unknown:boolean}){
 const [client]=useState(()=>{
  useAuthStore.getState().setAuth('synthetic',{id:1,email:'preview@example.invalid',username:'Preview'})
  const id='a'.repeat(64);let enabled=false,revision=0
  sessionStorage.removeItem(`gamja.mobile-library.1.${id}`)
  mobileAccessApi.status=async()=>({enabled:true,listening:true,origin:'https://example.invalid',loopback_port:8432,owner_epoch:'b'.repeat(64),devices:[{id,name:'합성 휴대폰',expires_at:1893456000,last_seen_at:null,library_read:enabled,library_revision:revision}]})
  mobileAccessApi.libraryChange=async(_id,cmd)=>{if(unknown)throw Error('synthetic lost response');enabled=cmd.enabled;revision++;return {...cmd,device_id:id,state:'applied',code:null,library_read:enabled,library_revision:revision}}
  mobileAccessApi.libraryResolve=async(_id,cmd)=>({...cmd,device_id:id,state:'fenced',code:'not_applied',library_read:enabled,library_revision:revision})
  remoteApi.listDevices=async()=>[]
  return new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}})
 })
 return <I18nProvider systemLanguages={()=>['ko']}><QueryClientProvider client={client}><MemoryRouter><Devices/></MemoryRouter></QueryClientProvider></I18nProvider>
}
const meta={id:'mobile-library-settings',title:'Screens/모바일 보관함 권한',component:SettingsStory,args:{unknown:false}} satisfies Meta<typeof SettingsStory>
export default meta
type Story=StoryObj<typeof meta>
export const Ready:Story={name:'기기별 보관함 읽기 · 허용 확인',args:{unknown:false}}
export const Unknown:Story={name:'설정 결과 불명 · 확인',args:{unknown:true}}
