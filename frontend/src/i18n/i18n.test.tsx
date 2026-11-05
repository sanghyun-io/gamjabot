// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import {
  I18nProvider,
  LOCALE_STORAGE_KEY,
  canonicalizeLocale,
  readLocaleChoice,
  resolveLocale,
  useI18n,
} from './index'

afterEach(() => {
  cleanup()
  window.localStorage.clear()
  document.documentElement.removeAttribute('lang')
})

describe('locale resolution', () => {
  it('canonicalizes supported regional and script variants', () => {
    expect(canonicalizeLocale('zh-Hant-HK')).toBe('zh-TW')
    expect(canonicalizeLocale('zh-Hans-SG')).toBe('zh-CN')
    expect(canonicalizeLocale('pt-PT')).toBe('pt-BR')
    expect(canonicalizeLocale('fr-CA')).toBe('fr')
    expect(canonicalizeLocale('xx-YY')).toBeNull()
  })

  it('keeps the system sentinel and ignores corrupt storage', () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'system')
    expect(readLocaleChoice(window.localStorage)).toBe('system')
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'not-a-locale')
    expect(readLocaleChoice(window.localStorage)).toBe('system')
    expect(resolveLocale('system', ['xx', 'ja-JP'])).toBe('ja')
    expect(resolveLocale('system', ['xx'])).toBe('en')
  })
})

function Harness() {
  const { choice, locale, setChoice, t, formatNumber } = useI18n()
  const [count, setCount] = useState(1)
  return <div>
    <p>{choice}:{locale}</p>
    <p>{t('common.items', { count })}</p>
    <p>{t('common.items', { count: 12345 })}</p>
    <p>{t('chat.roomTitle', { id: '12345' })}</p>
    <p>{formatNumber(12345)}</p>
    <button onClick={() => setCount(2)}>count</button>
    <button onClick={() => setChoice('de')}>Deutsch</button>
    <button onClick={() => setChoice('system')}>System</button>
  </div>
}

describe('I18nProvider', () => {
  it('persists explicit choices, rerenders subscribers, and updates html lang', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'ko')
    render(<I18nProvider systemLanguages={() => ['en-US']}><Harness /></I18nProvider>)
    expect(await screen.findByText('ko:ko')).toBeTruthy()
    expect(document.documentElement.lang).toBe('ko')
    fireEvent.click(screen.getByRole('button', { name: 'Deutsch' }))
    expect(await screen.findByText('de:de')).toBeTruthy()
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('de')
    expect(document.documentElement.lang).toBe('de')
  })

  it('uses Intl plural categories and selected-locale number formatting', () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'en')
    render(<I18nProvider systemLanguages={() => ['ko']}><Harness /></I18nProvider>)
    expect(screen.getByText('1 item')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'count' }))
    expect(screen.getByText('2 items')).toBeTruthy()
    expect(screen.getByText('12,345')).toBeTruthy()
    expect(screen.getByText('12,345 items')).toBeTruthy()
    expect(screen.getByText('Room #12345')).toBeTruthy()
  })

  it('re-resolves a stored system choice when the OS language changes', async () => {
    let languages = ['en-US']
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'system')
    render(<I18nProvider systemLanguages={() => languages}><Harness /></I18nProvider>)
    expect(screen.getByText('system:en')).toBeTruthy()
    languages = ['ja-JP']
    act(() => window.dispatchEvent(new Event('languagechange')))
    expect(await screen.findByText('system:ja')).toBeTruthy()
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('system')
  })
})
