// @vitest-environment jsdom
import { webcrypto } from 'node:crypto'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useAuthStore } from '../src/stores/authStore'
import { Surface } from './SkillRunComposer.stories'

afterEach(() => { cleanup(); useAuthStore.getState().logout(); vi.unstubAllGlobals() })
async function prepare(scenario:'ready'|'unknown'|'mismatch') {
  vi.stubGlobal('crypto',webcrypto)
  render(<Surface scenario={scenario}/>)
  fireEvent.click(await screen.findByRole('button',{name:/Run a Skill once|Skill 한 번 실행/}))
  fireEvent.change(await screen.findByLabelText('Bot'),{target:{value:'8'}})
  await waitFor(()=>expect((screen.getByLabelText('Skill') as HTMLSelectElement).disabled).toBe(false))
  fireEvent.change(screen.getByLabelText('Skill'),{target:{value:'11111111-1111-4111-8111-111111111111'}})
  fireEvent.change(screen.getByLabelText('topic'),{target:{value:'합성 주제'}})
  fireEvent.change(screen.getByLabelText(/Question for the Bot|Bot에게 할 질문/),{target:{value:'합성 질문'}})
  fireEvent.click(screen.getByRole('button',{name:/Review content|내용 검토/}))
  if(scenario!=='mismatch') await screen.findByText(/Skill reference sent to the model|모델에 전달할 Skill 참고 내용/)
}
it('reviews the literal expansion and accepts a matching structured receipt', async()=>{
  await prepare('ready')
  expect(screen.getByText(/synthetic-model/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button',{name:/Send one reviewed request|검토한 내용으로 한 번 요청/}))
  expect(await screen.findByText(/합성 대화에 질문이 추가됨/)).toBeTruthy()
  expect(screen.getByText('합성 전송 1회')).toBeTruthy()
})
it('checks the original unknown request after remount without resending', async()=>{
  await prepare('unknown')
  fireEvent.click(screen.getByRole('button',{name:/Send one reviewed request|검토한 내용으로 한 번 요청/}))
  await screen.findByRole('button',{name:/Check result|결과 확인/})
  fireEvent.click(screen.getByRole('button',{name:'합성 화면 다시 열기'}))
  fireEvent.click(await screen.findByRole('button',{name:/Check result|결과 확인/}))
  expect(await screen.findByText(/합성 대화에 질문이 추가됨/)).toBeTruthy()
  expect(screen.getByText('합성 전송 1회')).toBeTruthy()
})
it('rejects a preview whose expanded text differs from the selected Skill', async()=>{
  await prepare('mismatch')
  expect(await screen.findByRole('alert')).toBeTruthy()
  expect(screen.queryByRole('button',{name:/Send one reviewed request|검토한 내용으로 한 번 요청/})).toBeNull()
  expect(screen.getByText('합성 전송 0회')).toBeTruthy()
})
