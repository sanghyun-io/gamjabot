// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { fireEvent, waitFor } from '@testing-library/dom'
import { readFileSync } from 'node:fs'
const source:string=readFileSync('../backend/app/services/mobile_page.py','utf8')
const page=source.match(/PAGE='''([\s\S]*?)'''\nJS=r'''/)?.[1]
const script=source.match(/\nJS=r'''([\s\S]*?)'''\s*$/)?.[1]
afterEach(()=>{vi.unstubAllGlobals();vi.restoreAllMocks();document.body.replaceChildren()})
it.each([[401,false],[403,false],[409,false],[503,false],[403,true],[503,true]])('clears displayed content after a library read returns %s (non-JSON: %s)',async(status,nonJson)=>{
 if(!page||!script)throw Error('Production mobile source unavailable')
 const doc=new DOMParser().parseFromString(page,'text/html');document.body.replaceChildren(...Array.from(doc.body.childNodes).filter(x=>x.nodeName!=='SCRIPT'))
 const uuid='11111111-1111-4111-8111-111111111111';let revoked=false
 vi.stubGlobal('setInterval',vi.fn())
 vi.stubGlobal('fetch',vi.fn(async(path:string)=>{const u=new URL(path,'https://synthetic.invalid');let data:unknown
  if(u.pathname==='/session')data={paired:true,library_read:!revoked}
  else if(u.pathname==='/api/rooms')data={items:[],next_cursor:null}
  else if(u.pathname==='/api/library/bots')data={items:[{id:8,name:'Synthetic Bot'}],next_cursor:null}
  else if(revoked)return {ok:false,status,text:async()=>nonJson?'<html>gateway error</html>':JSON.stringify({detail:'denied'})}
  else if(u.pathname==='/api/library/bots/8/results')data={items:[{room_id:1,result_id:uuid,version:1,kind:'page',title:'Synthetic Page',author_agent_id:8}],next_cursor:null}
  else if(u.pathname===`/api/library/bots/8/results/1/${uuid}/1`)data={room_id:1,result_id:uuid,version:1,kind:'page',title:'Synthetic Page',author_agent_id:8,content:JSON.stringify({format:'gamja.page.v1',title:'Synthetic Page',datasets:[],views:[],blocks:[{type:'markdown',text:'Private synthetic content'}]}),file_status:null}
  else throw Error('unexpected synthetic route '+u.pathname)
  return {ok:true,status:200,text:async()=>JSON.stringify(data)}
 }))
 Element.prototype.scrollIntoView=vi.fn()
 window.eval(script)
 await waitFor(()=>expect(document.getElementById('directory')?.hidden).toBe(false))
 fireEvent.click(document.getElementById('libraryOpen')!);await waitFor(()=>expect(document.getElementById('libraryItems')?.textContent).toContain('Synthetic Bot'))
 fireEvent.click(Array.from(document.querySelectorAll('#libraryItems button')).find(x=>x.textContent==='열기')!);await waitFor(()=>expect(document.getElementById('libraryItems')?.textContent).toContain('Synthetic Page'))
 fireEvent.click(Array.from(document.querySelectorAll('#libraryItems button')).find(x=>x.textContent==='열기')!);await waitFor(()=>expect(document.getElementById('libraryDetail')?.textContent).toContain('Private synthetic content'))
 revoked=true;fireEvent.click(document.getElementById('libraryRefresh')!);await waitFor(()=>expect(document.getElementById('libraryDetail')?.textContent).not.toContain('Private synthetic content'))
 expect(document.getElementById('libraryItems')?.textContent).not.toContain('Synthetic Page')
})
it('ignores a delayed library response after returning to conversations',async()=>{
 if(!page||!script)throw Error('Production mobile source unavailable')
 const doc=new DOMParser().parseFromString(page,'text/html');document.body.replaceChildren(...Array.from(doc.body.childNodes).filter(x=>x.nodeName!=='SCRIPT'))
 let release!:(v:unknown)=>void
 vi.stubGlobal('setInterval',vi.fn());vi.stubGlobal('fetch',vi.fn(async(path:string)=>{const u=new URL(path,'https://synthetic.invalid');let data:unknown
  if(u.pathname==='/session')data={paired:true,library_read:true}
  else if(u.pathname==='/api/rooms')data={items:[],next_cursor:null}
  else if(u.pathname==='/api/library/bots')data={items:[{id:8,name:'Slow Bot'}],next_cursor:null}
  else if(u.pathname==='/api/library/bots/8/results')data=await new Promise(resolve=>{release=resolve})
  else throw Error('unexpected synthetic route '+u.pathname)
  return {ok:true,status:200,text:async()=>JSON.stringify(data)}
 }))
 window.eval(script);await waitFor(()=>expect(document.getElementById('directory')?.hidden).toBe(false))
 fireEvent.click(document.getElementById('libraryOpen')!);await waitFor(()=>expect(document.getElementById('libraryItems')?.textContent).toContain('Slow Bot'))
 fireEvent.click(Array.from(document.querySelectorAll('#libraryItems button')).find(x=>x.textContent==='열기')!);await waitFor(()=>expect(release).toBeTruthy())
 fireEvent.click(document.getElementById('back')!);release({items:[{title:'Old private',room_id:1,result_id:'11111111-1111-4111-8111-111111111111',version:1,kind:'page',author_agent_id:8}],next_cursor:null})
 await new Promise(resolve=>setTimeout(resolve,0));expect(document.getElementById('library')?.hidden).toBe(true);expect(document.getElementById('libraryItems')?.textContent).not.toContain('Old private')
})
