import type { ProviderCatalog as ProviderCatalogResponse, ProviderDescriptor } from '../../api/connections'
import type { Translate } from '../../i18n'
import { en, type MessageKey } from '../../i18n/locales/en'
import { useId, useState } from 'react'
import DirectorySearch from '../DirectorySearch'

export function providerDisplayName(provider: Pick<ProviderDescriptor, 'label_key'>, translate: Translate) {
  const key = provider.label_key
  return key.startsWith('connections.providers.') && Object.prototype.hasOwnProperty.call(en, key)
    ? translate(key as MessageKey)
    : translate('connections.providers.unknown')
}

function providerSearchAliases(provider: Pick<ProviderDescriptor, 'label_key'>, translate: Translate) {
  const key = `${provider.label_key}.searchAliases`
  return Object.prototype.hasOwnProperty.call(en, key) ? translate(key as MessageKey) : ''
}

function normalizedSearchTerm(value: string) {
  return value.normalize('NFKC').toLocaleLowerCase()
}

export function ProviderCatalog({ catalog, value, disabled, label, translate, onChange }: {
  catalog: ProviderCatalogResponse
  value: string
  disabled?: boolean
  label: string
  translate: Translate
  onChange: (providerId: string) => void
}) {
  const [search, setSearch] = useState('')
  const selectId = useId()
  const helpId = useId()
  const selected = catalog.providers.find((provider) => provider.provider_id === value)
  const query = normalizedSearchTerm(search.trim())
  const matches = query ? catalog.providers.filter((provider) => [
    providerDisplayName(provider, translate),
    provider.provider_id,
    providerSearchAliases(provider, translate),
  ].some((term) => normalizedSearchTerm(term).includes(query))) : catalog.providers
  const selectedIsHidden = selected && !matches.some((provider) => provider.provider_id === selected.provider_id)
  return <div className="provider-catalog">
    <DirectorySearch compact value={search} onChange={setSearch} disabled={disabled} preventSubmit
      label={translate('connections.providerSearch')} hint={translate('connections.providerSearchPlaceholder')}
      shown={matches.length} total={catalog.providers.length} loading={false} known controls={selectId} />
    <details className="compact-details"><summary>{translate('directory.ui.searchDetails')}</summary><p id={helpId} className="field-hint">{translate('connections.providerSearchHelp')}</p></details>
    {selectedIsHidden && <p role="status" className="notice notice--info">
      {translate('connections.providerSelectionOutside', { name: providerDisplayName(selected, translate) })}
    </p>}
    <label className="field">{label}
      <select id={selectId} aria-describedby={helpId} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
        {selectedIsHidden && <option value={selected.provider_id}>
          {providerDisplayName(selected, translate)}
        </option>}
        {matches.map((provider) => <option key={provider.provider_id} value={provider.provider_id}>
          {providerDisplayName(provider, translate)}
        </option>)}
      </select>
    </label>
    {matches.length === 0 && <p role="status" className="field-hint">
      {translate('connections.providerSearchEmpty')}
    </p>}

  </div>
}
