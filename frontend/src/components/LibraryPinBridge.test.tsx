// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { I18nProvider } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import LibraryPinManager, { useLibraryPinActions } from './LibraryPinManager'
import type { PinSource } from '../api/libraryPins'
declare const process: {env:Record<string,string|undefined>}
vi.mock('../lib/api',()=>({default:{
 get:async(path:string,config?:{params?:Record<string,string>})=>{const u=new URL('/api/v1'+path,process.env.PIN_BRIDGE_URL);Object.entries(config?.params||{}).forEach(([k,v])=>u.searchParams.set(k,String(v)));const r=await fetch(u);if(!r.ok)throw Error(`HTTP ${r.status}`);return {data:await r.json()}},
 post:async(path:string,body:unknown)=>{const r=await fetch(new URL('/api/v1'+path,process.env.PIN_BRIDGE_URL),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw Error(`HTTP ${r.status}`);return {data:await r.json()}},
}}))
afterEach(()=>{cleanup();sessionStorage.clear()})
it.skipIf(!process.env.PIN_BRIDGE_URL)('pins, reads, and unpins a synthetic stored upload through real FastAPI routes',async()=>{
 const bot=Number(process.env.PIN_BRIDGE_BOT),owner=Number(process.env.PIN_BRIDGE_OWNER)
 const source:PinSource={kind:'upload',upload_id:process.env.PIN_BRIDGE_UPLOAD!,sha256:process.env.PIN_BRIDGE_SHA!}
 useAuthStore.getState().setAuth('synthetic',{id:owner,email:'bridge@example.invalid',username:'Owner'})
 function Child(){const x=useLibraryPinActions();return <button disabled={x.busy} onClick={()=>x.pin(source)}>Pin synthetic upload</button>}
 render(<I18nProvider systemLanguages={()=>['en']}><MemoryRouter><LibraryPinManager botId={bot} onUpload={()=>{}}><Child/></LibraryPinManager></MemoryRouter></I18nProvider>)
 const pin=screen.getByRole('button',{name:'Pin synthetic upload'});await waitFor(()=>expect(pin.hasAttribute('disabled')).toBe(false));fireEvent.click(pin)
 expect(await screen.findByText('synthetic.txt')).toBeTruthy()
 fireEvent.click(screen.getByRole('button',{name:'Unpin'}))
 await screen.findByText('No pinned items.')
})
