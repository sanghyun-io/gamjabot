import { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { en, type MessageCatalog, type MessageKey, type MessageValue } from './locales/en'
import { loadCatalog, readLoadedCatalog } from './catalogLoader'

export const SUPPORTED_LOCALES = ['ko', 'en', 'ja', 'zh-CN', 'zh-TW', 'es', 'fr', 'de', 'pt-BR'] as const
export type SupportedLocale = typeof SUPPORTED_LOCALES[number]
export type LocaleChoice = 'system' | SupportedLocale
export const LOCALE_STORAGE_KEY = 'gamja.locale'

const supported = new Set<string>(SUPPORTED_LOCALES)

export function canonicalizeLocale(input: string): SupportedLocale | null {
  const normalized = input.trim().replaceAll('_', '-')
  if (!normalized) return null
  try {
    const tag = new Intl.Locale(normalized)
    const language = tag.language.toLowerCase()
    if (language === 'zh') {
      const script = tag.script?.toLowerCase()
      const region = tag.region?.toUpperCase()
      return script === 'hant' || ['TW', 'HK', 'MO'].includes(region || '') ? 'zh-TW' : 'zh-CN'
    }
    if (language === 'pt') return 'pt-BR'
    if (['ko', 'en', 'ja', 'es', 'fr', 'de'].includes(language)) return language as SupportedLocale
  } catch { return null }
  return null
}

export function resolveLocale(choice: LocaleChoice, languages: readonly string[]): SupportedLocale {
  if (choice !== 'system') return choice
  for (const language of languages) {
    const resolved = canonicalizeLocale(language)
    if (resolved) return resolved
  }
  return 'en'
}

export function readLocaleChoice(storage: Pick<Storage, 'getItem'> | null | undefined): LocaleChoice {
  try {
    const stored = storage?.getItem(LOCALE_STORAGE_KEY)
    return stored === 'system' || (stored && supported.has(stored)) ? stored as LocaleChoice : 'system'
  } catch { return 'system' }
}

function systemLanguages(): readonly string[] {
  if (typeof navigator === 'undefined') return ['en']
  return navigator.languages?.length ? navigator.languages : [navigator.language || 'en']
}

// Only the nine supported locale keys can reach these private caches. Keep
// formatter objects, never the numbers, rendered text, Owner data or options.
const defaultNumberFormats = new Map<SupportedLocale, Intl.NumberFormat>()
const defaultPluralRules = new Map<SupportedLocale, Intl.PluralRules>()
function defaultNumberFormat(locale: SupportedLocale) {
  let value = defaultNumberFormats.get(locale)
  if (!value) { value = new Intl.NumberFormat(locale); defaultNumberFormats.set(locale, value) }
  return value
}
function pluralRules(locale: SupportedLocale) {
  let value = defaultPluralRules.get(locale)
  if (!value) { value = new Intl.PluralRules(locale); defaultPluralRules.set(locale, value) }
  return value
}
function formatNumericValue(locale: SupportedLocale, value: number, options?: Intl.NumberFormatOptions) {
  // Explicit options remain fresh: preserve validation and option getter
  // behavior instead of building an unbounded or lossy options cache.
  return (options === undefined ? defaultNumberFormat(locale) : new Intl.NumberFormat(locale, options)).format(value)
}

function interpolate(template: string, locale: SupportedLocale, values: Record<string, string | number> = {}) {
  if (!template.includes('{')) return template
  return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (whole, name: string) => {
    if (!(name in values)) return whole
    const value = values[name]
    return typeof value === 'number' ? defaultNumberFormat(locale).format(value) : value
  })
}

function messageTemplate(message: MessageValue, locale: SupportedLocale, values?: Record<string, string | number>) {
  if (typeof message === 'string') return message
  const count = typeof values?.count === 'number' ? values.count : Number(values?.count || 0)
  const category = pluralRules(locale).select(count)
  return message[category] || message.other || message.one || ''
}

export type I18nApi = {
  choice: LocaleChoice
  locale: SupportedLocale
  setChoice: (choice: LocaleChoice) => void
  t: (key: MessageKey, values?: Record<string, string | number>) => string
  formatDateTime: (value: Date | string | number, options?: Intl.DateTimeFormatOptions) => string
  formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string
}
export type Translate = I18nApi['t']

const defaultApi: I18nApi = {
  choice: 'system', locale: 'en', setChoice: () => {},
  t: (key, values) => interpolate(messageTemplate(en[key], 'en', values), 'en', values),
  formatDateTime: (value, options) => new Intl.DateTimeFormat('en', options).format(new Date(value)),
  formatNumber: (value, options) => formatNumericValue('en', value, options),
}

const I18nContext = createContext<I18nApi>(defaultApi)

type ActiveLocale = { choice: LocaleChoice; locale: SupportedLocale; catalog: MessageCatalog }
type LocaleRequest = { choice: LocaleChoice; locale: SupportedLocale; persist: boolean }
type LocaleLoadControls = {
  choice: LocaleChoice; loading: SupportedLocale | null; failed: SupportedLocale | null;
  canKeep: boolean; retry: () => void; keepCurrent: () => void;
}
const LocaleLoadContext = createContext<LocaleLoadControls>({
  choice: 'system', loading: null, failed: null, canKeep: false, retry: () => {}, keepCurrent: () => {},
})

// Tiny recovery text is available even if the selected catalog cannot be read.
const bootstrapText: Record<SupportedLocale, { loading: string; failed: string; retry: string; english: string }> = {
  en: { loading: 'Loading your language…', failed: 'Could not load this language.', retry: 'Try this language again', english: 'Continue in English' },
  ko: { loading: '선택한 언어를 불러오는 중입니다…', failed: '선택한 언어를 불러오지 못했습니다.', retry: '이 언어 다시 불러오기', english: '영어로 앱 열기' },
  ja: { loading: '選択した言語を読み込んでいます…', failed: 'この言語を読み込めませんでした。', retry: 'この言語を再読み込み', english: '英語でアプリを開く' },
  'zh-CN': { loading: '正在加载所选语言…', failed: '无法加载所选语言。', retry: '重试加载此语言', english: '使用英语打开应用' },
  'zh-TW': { loading: '正在載入所選語言…', failed: '無法載入所選語言。', retry: '重試載入此語言', english: '使用英語開啟應用程式' },
  de: { loading: 'Sprache wird geladen…', failed: 'Diese Sprache konnte nicht geladen werden.', retry: 'Sprache erneut laden', english: 'Auf Englisch öffnen' },
  es: { loading: 'Cargando el idioma…', failed: 'No se pudo cargar este idioma.', retry: 'Volver a cargar este idioma', english: 'Abrir en inglés' },
  fr: { loading: 'Chargement de la langue…', failed: 'Impossible de charger cette langue.', retry: 'Réessayer cette langue', english: 'Ouvrir en anglais' },
  'pt-BR': { loading: 'Carregando o idioma…', failed: 'Não foi possível carregar este idioma.', retry: 'Tentar este idioma novamente', english: 'Abrir em inglês' },
}

export function I18nProvider({ children, systemLanguages: getLanguages = systemLanguages }: { children: ReactNode; systemLanguages?: () => readonly string[] }) {
  const [initial] = useState(() => {
    let choice: LocaleChoice = 'system'
    // Accessing localStorage itself can throw in a restricted WebView.
    try { choice = readLocaleChoice(typeof window === 'undefined' ? null : window.localStorage) } catch { /* use system */ }
    return { choice, locale: resolveLocale(choice, getLanguages()) }
  })
  const [current, setCurrent] = useState<ActiveLocale | null>(() => {
    const catalog = readLoadedCatalog(initial.locale)
    return catalog ? { ...initial, catalog } : null
  })
  const [request, setRequest] = useState<LocaleRequest | null>(() => current ? null : { ...initial, persist: false })
  const [failure, setFailure] = useState<LocaleRequest | null>(null)
  const live = useRef(current), desired = useRef(request), failed = useRef<LocaleRequest | null>(null), mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const commit = useCallback((intent: LocaleRequest, catalog: MessageCatalog) => {
    if (!mounted.current || desired.current !== intent) return
    const value = { choice: intent.choice, locale: intent.locale, catalog }
    live.current = value; desired.current = null; failed.current = null
    setCurrent(value); setRequest(null); setFailure(null)
    // Only an explicit, successfully applied selection replaces the profile choice.
    if (intent.persist) {
      try { window.localStorage.setItem(LOCALE_STORAGE_KEY, intent.choice) } catch { /* storage may be unavailable */ }
    }
  }, [])

  const choose = useCallback((next: LocaleChoice, persist = true) => {
    if (!mounted.current || next !== 'system' && !supported.has(next)) return
    const intent: LocaleRequest = { choice: next, locale: resolveLocale(next, getLanguages()), persist }
    desired.current = intent; failed.current = null; setFailure(null)
    const cached = readLoadedCatalog(intent.locale)
    if (cached) commit(intent, cached)
    else setRequest(intent)
  }, [getLanguages, commit])
  const setChoice = useCallback((next: LocaleChoice) => choose(next, true), [choose])

  useEffect(() => {
    if (!request) return
    let active = true
    void loadCatalog(request.locale).then(catalog => {
      if (active) commit(request, catalog)
    }, () => {
      if (!active || !mounted.current || desired.current !== request) return
      failed.current = request; setFailure(request)
    })
    return () => { active = false }
  }, [request, commit])

  useEffect(() => {
    const update = () => {
      const intent = desired.current
      if ((intent?.choice ?? live.current?.choice ?? initial.choice) !== 'system') return
      const locale = resolveLocale('system', getLanguages())
      if (intent?.locale === locale || !intent && live.current?.locale === locale) return
      // A pending explicit selection of System still needs to be saved on success.
      choose('system', intent?.persist ?? false)
    }
    window.addEventListener('languagechange', update)
    return () => window.removeEventListener('languagechange', update)
  }, [getLanguages, initial.choice, choose])

  const retry = useCallback(() => {
    const intent = desired.current
    if (intent && failed.current === intent) choose(intent.choice, intent.persist)
  }, [choose])
  const keepCurrent = useCallback(() => {
    const value = live.current
    // This explicit action fixes the displayed language as the profile choice,
    // including when the previous choice was System. No silent System mismatch.
    if (value) choose(value.locale, true)
  }, [choose])

  useEffect(() => { if (current) document.documentElement.lang = current.locale }, [current?.locale])

  const api = useMemo<I18nApi>(() => {
    const { choice, locale, catalog } = current ?? { choice: initial.choice, locale: 'en' as const, catalog: en }
    return {
      choice, locale, setChoice,
      t: (key, values) => interpolate(messageTemplate(catalog[key] ?? en[key], locale, values), locale, values),
      formatDateTime: (value, options) => new Intl.DateTimeFormat(locale, options).format(new Date(value)),
      formatNumber: (value, options) => formatNumericValue(locale, value, options),
    }
  }, [current, initial.choice, setChoice])
  const controls = useMemo<LocaleLoadControls>(() => ({
    choice: request?.choice ?? current?.choice ?? initial.choice,
    loading: request && failure !== request ? request.locale : null,
    failed: failure?.locale ?? null, canKeep: !!current && !!request, retry, keepCurrent,
  }), [request, failure, current, initial.choice, retry, keepCurrent])
  const target = request?.locale ?? initial.locale, boot = bootstrapText[target]

  return <I18nContext.Provider value={api}><LocaleLoadContext.Provider value={controls}>
    {current ? children : <main lang={target} className="auth-shell"><section className="auth-card stack stack--tight">
      <p role={failure ? 'alert' : 'status'}>{failure ? boot.failed : boot.loading}</p>
      <LocaleSelector feedback={false}/>
      <div className="page-actions">
        {failure && <button type="button" className="button button--secondary" onClick={retry}>{boot.retry}</button>}
        <button type="button" className="text-link" onClick={() => setChoice('en')}>{boot.english}</button>
      </div>
    </section></main>}
  </LocaleLoadContext.Provider></I18nContext.Provider>
}

export function useI18n() { return useContext(I18nContext) }

export function LocaleSelector({ className = '', feedback = true }: { className?: string; feedback?: boolean }) {
  const { setChoice, t } = useI18n(), state = useContext(LocaleLoadContext), helpId = useId()
  const showFeedback = feedback && !!(state.loading || state.failed)
  return <div className={`locale-selector stack stack--tight ${className}`} style={{ maxWidth: '9.5rem' }}>
    <label>
      <span className="sr-only">{t('locale.label')}</span>
      <select aria-label={t('locale.label')} aria-busy={!!state.loading} aria-describedby={showFeedback ? helpId : undefined}
        value={state.choice} onChange={event => setChoice(event.target.value as LocaleChoice)}>
        <option value="system">{t('locale.system')}</option>
        {SUPPORTED_LOCALES.map(locale => <option key={locale} value={locale}>{t(`locale.${locale}` as MessageKey)}</option>)}
      </select>
    </label>
    {showFeedback && <div id={helpId} className="stack stack--tight">
      {state.loading && <p role="status" className="metadata">{t('locale.loading', { language: t(`locale.${state.loading}` as MessageKey) })}</p>}
      {state.failed && <details className="compact-details">
        <summary>{t('locale.failed', { language: t(`locale.${state.failed}` as MessageKey) })}</summary>
        <p role="alert" className="field-hint">{t('locale.loadHelp')}</p>
        <button type="button" className="text-link" onClick={state.retry}>{t('locale.retry')}</button>
      </details>}
      {state.canKeep && <button type="button" className="text-link" onClick={state.keepCurrent}>{t('locale.keep')}</button>}
    </div>}
  </div>
}
