// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { I18nProvider } from '../i18n'
import { useAuthStore } from '../stores/authStore'
import { libraryUploadsApi } from '../api/libraryUploads'
import LibraryUploads from './LibraryUploads'
afterEach(()=>{cleanup();vi.restoreAllMocks()})
it('loads the exact saved upload even when it was not on the first library page',async()=>{
 useAuthStore.getState().setAuth('synthetic',{id:1,email:'one@example.invalid',username:'One'})
 const id='11111111-1111-4111-8111-111111111111',sha='a'.repeat(64)
 vi.spyOn(libraryUploadsApi,'list').mockResolvedValue({bot_id:8,items:[],next_cursor:null})
 vi.spyOn(libraryUploadsApi,'read').mockResolvedValue({bot_id:8,upload_id:id,sha256:sha,filename:'saved.pdf',size_bytes:12,created_at:'2026-10-30T00:00:00Z',media_type:'application/octet-stream',origin:'owner_upload',removed:false})
 Element.prototype.scrollIntoView=vi.fn()
 render(<I18nProvider systemLanguages={()=>['en']}><LibraryUploads botId={8} focusUpload={{id,sha,nonce:1}}/></I18nProvider>)
 expect(await screen.findByText('saved.pdf')).toBeTruthy()
 expect(libraryUploadsApi.read).toHaveBeenCalledWith(8,id)
})
