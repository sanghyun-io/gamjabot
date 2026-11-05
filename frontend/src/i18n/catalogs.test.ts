import { describe, expect, it } from 'vitest'
import { en, type MessageCatalog } from './locales/en'
import { loadCatalog } from './catalogLoader'
import { ko } from './locales/ko'
import { ja } from './locales/ja'
import { zhCN } from './locales/zh-CN'
import { zhTW } from './locales/zh-TW'
import { es } from './locales/es'
import { fr } from './locales/fr'
import { de } from './locales/de'
import { ptBR } from './locales/pt-BR'

const catalogs: Record<string, Partial<MessageCatalog>> = {
  ko, en, ja, 'zh-CN': zhCN, 'zh-TW': zhTW, es, fr, de, 'pt-BR': ptBR,
}

function placeholders(value: MessageCatalog[keyof MessageCatalog]) {
  const text = typeof value === 'string' ? value : Object.values(value).join(' ')
  return [...new Set([...text.matchAll(/\{([a-zA-Z0-9_]+)\}/g)].map((match) => match[1]))].sort()
}

describe('translation catalogs', () => {
  it('loads missing translations from English while retaining translated values', async () => {
    const catalog = await loadCatalog('ja')
    expect(catalog['contacts.history']).toEqual(en['contacts.history'])
    expect(catalog['fileQuestion.fullConsent']).toEqual(ja['fileQuestion.fullConsent'])
  })
  it('keeps translated keys and interpolation contracts aligned and falls back to English', () => {
    const expectedKeys = Object.keys(en).sort()
    for (const [locale, catalog] of Object.entries(catalogs)) {
      const merged: MessageCatalog = { ...en, ...catalog }
      expect(Object.keys(merged).sort(), `${locale} keys`).toEqual(expectedKeys)
      for (const key of expectedKeys) {
        expect(placeholders(merged[key as keyof MessageCatalog]), `${locale}:${key} placeholders`)
          .toEqual(placeholders(en[key as keyof MessageCatalog]))
      }
    }
  })
})
