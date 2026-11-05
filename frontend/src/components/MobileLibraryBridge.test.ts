// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { fireEvent, waitFor } from '@testing-library/dom'
import { readFileSync } from 'node:fs'
declare const process:{env:Record<string,string|undefined>}
const source:string=readFileSync('../backend/app/services/mobile_page.py','utf8')
const page=source.match(/PAGE='''([\s\S]*?)'''\nJS=r'''/)?.[1],script=source.match(/\nJS=r'''([\s\S]*?)'''\s*$/)?.[1]
afterEach(()=>{vi.unstubAllGlobals();vi.restoreAllMocks();document.body.replaceChildren()})
it.skipIf(!process.env.MOBILE_LIBRARY_BRIDGE)('loads a synthetic paired phone upload using the actual mobile gateway',async()=>{
 if(!page||!script)throw Error('production page unavailable')
 const doc=new DOMParser().parseFromString(page,'text/html');document.body.replaceChildren(...Array.from(doc.body.childNodes).filter(x=>x.nodeName!=='SCRIPT'))
 const nativeFetch=globalThis.fetch;vi.stubGlobal('setInterval',vi.fn());vi.stubGlobal('fetch',(path:string,options?:RequestInit)=>nativeFetch(new URL(path,process.env.MOBILE_LIBRARY_BRIDGE),options))
 Element.prototype.scrollIntoView=vi.fn();window.eval(script)
 await waitFor(()=>expect(document.getElementById('directory')?.hidden).toBe(false));fireEvent.click(document.getElementById('libraryOpen')!)
 await waitFor(()=>expect(document.getElementById('libraryItems')?.textContent).toContain(process.env.MOBILE_LIBRARY_BOT_NAME));const card=Array.from(document.querySelectorAll('#libraryItems article')).find(x=>x.textContent?.includes(process.env.MOBILE_LIBRARY_BOT_NAME!));fireEvent.click(card!.querySelector('button')!)
 await waitFor(()=>expect(document.getElementById('libraryTabs')?.hidden).toBe(false));fireEvent.click(document.querySelector('#libraryTabs [data-tab="uploads"]')!)
 await waitFor(()=>expect(document.getElementById('libraryItems')?.textContent).toContain('synthetic-mobile.txt'))
 fireEvent.click(document.querySelector('#libraryItems button')!);await waitFor(()=>expect(document.getElementById('libraryDetail')?.textContent).toContain('synthetic-mobile.txt'))
 expect(document.querySelector('#libraryDetail a')?.textContent).toBe('파일 다운로드')
})
