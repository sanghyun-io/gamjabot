import { en, type MessageCatalog } from './locales/en'
import type { SupportedLocale } from './index'

// Bundled application text only: no network translation service, Owner data,
// persistence or background prefetch. Keep English available for recovery.
const loaded = new Map<SupportedLocale, MessageCatalog>([['en', en]])
const pending = new Map<SupportedLocale, Promise<MessageCatalog>>()
const loaders: Record<Exclude<SupportedLocale, 'en'>, () => Promise<Partial<MessageCatalog>>> = {
  ko: () => import('./locales/ko').then(module => module.ko),
  ja: () => import('./locales/ja').then(module => module.ja),
  'zh-CN': () => import('./locales/zh-CN').then(module => module.zhCN),
  'zh-TW': () => import('./locales/zh-TW').then(module => module.zhTW),
  es: () => import('./locales/es').then(module => module.es),
  fr: () => import('./locales/fr').then(module => module.fr),
  de: () => import('./locales/de').then(module => module.de),
  'pt-BR': () => import('./locales/pt-BR').then(module => module.ptBR),
}

export function readLoadedCatalog(locale: SupportedLocale) { return loaded.get(locale) }

export function loadCatalog(locale: SupportedLocale): Promise<MessageCatalog> {
  const existing = loaded.get(locale)
  if (existing) return Promise.resolve(existing)
  const running = pending.get(locale)
  if (running) return running
  // English is in the cache from module initialization.
  const request = loaders[locale as Exclude<SupportedLocale, 'en'>]().then(catalog => {
    const merged: MessageCatalog = { ...en, ...catalog }
    loaded.set(locale, merged)
    return merged
  })
  pending.set(locale, request)
  const clear = () => { if (pending.get(locale) === request) pending.delete(locale) }
  // Rejected promises do not pin a language in a permanent failed state here.
  // The underlying WebView module cache remains outside this loader's control.
  void request.then(clear, clear)
  return request
}
