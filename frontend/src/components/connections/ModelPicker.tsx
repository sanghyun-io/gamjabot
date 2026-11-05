import SelectedModelIdentity from './SelectedModelIdentity'
import { useState } from 'react'
import { useI18n } from '../../i18n'
import { reviewTimestamp, reviewTimeOptions } from '../../lib/reviewTime'
import DirectorySearch, { directoryMatcher } from '../DirectorySearch'
import type { ModelCatalog, ModelItem } from '../../api/connections'

export interface ModelPickerCopy {
  knownModel: string
  chooseKnownModel: string
  manualModel: string
  refresh: string
  refreshing: string
  stale: string
  truncated: string
  empty: string
  loadFailure: string
  refreshFailure: string
  configurationChanged: string
  retry: string
  retryRefresh: string
}

type ModelPickerProps = {
  invalid?: boolean; describedBy?: string; catalog?: ModelCatalog; compact?: boolean; value: string;
  defaultModelId?: string; copy: ModelPickerCopy; busy?: boolean; canRefresh?: boolean; loadError?: boolean;
  refreshError?: boolean; configurationChanged?: boolean; searchScope?: string;
  onChange: (modelId: string) => void; onRefresh?: () => void; onRetryLoad?: () => void;
}
export function ModelPicker(props: ModelPickerProps) {
  return <ScopedModelPicker key={props.searchScope || 'current'} {...props} />
}
function ScopedModelPicker({ searchScope, catalog, value, defaultModelId, copy, compact = false, busy, canRefresh = false, loadError = false,
  refreshError = false, configurationChanged = false, invalid = false, describedBy, onChange, onRefresh, onRetryLoad }: ModelPickerProps) {
  const { t, formatDateTime } = useI18n()
  const [query, setQuery] = useState(''), [controlId] = useState(() => `known-model-${crypto.randomUUID()}`)
  const [manualOpen, setManualOpen] = useState<boolean | null>(null)
  const known = catalog?.models || []
  const foldable = compact && known.length > 0
  const matches = directoryMatcher(query)
  const shown = known.filter(item => matches([item.id, item.label]))
  const selected = known.find(item => item.id === value)
  const pinned = selected && !shown.some(item => item.id === selected.id)
  // Model-catalog responses carry a zone. Never borrow the activity helper's
  // legacy UTC assumption for an absent/ambiguous model-list timestamp.
  const observed = typeof catalog?.observed_at === 'string' && /(?:Z|[+-]\d\d:\d\d)$/.test(catalog.observed_at) ? reviewTimestamp(catalog.observed_at) : null
  const sourceKey = selected?.source === 'configured' ? 'connections.modelInfo.configured'
    : selected?.source === 'discovery' ? 'connections.modelInfo.discovery'
    : selected?.source === 'native_catalog' ? 'connections.modelInfo.native' : 'connections.modelInfo.unknown'
  const modelLabel = (item: ModelItem) => item.label && item.label !== item.id ? `${item.label} · ${item.id}` : item.id
  const choose = (item: ModelItem) => onChange(item.id)
  return <div className="model-picker stack stack--tight">
    {known.length > 0 && <>
      <DirectorySearch compact value={query} onChange={setQuery} label={t('connections.modelSearch.title')} hint={t('connections.modelSearch.placeholder')}
        shown={shown.length} total={known.length} known loading={!!busy} controls={controlId} preventSubmit />
      <details className="compact-details"><summary>{t('directory.ui.searchDetails')}</summary><p className="field-hint">{t('connections.modelSearch.help')}</p></details>
      {!shown.length && <p role="status">{t('connections.modelSearch.noMatches')}</p>}
      {pinned && selected && <p className="field-hint">{t('connections.modelSearch.selectedOutside', { model: modelLabel(selected) })}</p>}
    </>}
    {known.length > 0 && <label className="field">{copy.knownModel}
      <select id={controlId} value={selected ? value : ''}
        onChange={(event) => { const item = known.find((candidate) => candidate.id === event.target.value); if (item) choose(item) }}>
        <option value="">{copy.chooseKnownModel}</option>
        {pinned && selected && <option value={selected.id}>{t('connections.modelSearch.currentSelection', { model: modelLabel(selected) })}</option>}
        {shown.map((item) => <option key={item.id} value={item.id}>{modelLabel(item)}</option>)}
      </select>
    </label>}
    {selected && <SelectedModelIdentity scope={searchScope || 'current'} modelId={selected.id}/>}
    <details className={foldable ? 'compact-details' : undefined} open={!foldable || (manualOpen ?? !selected)}
      onToggle={event => { if (foldable) setManualOpen(event.currentTarget.open) }}>
      <summary hidden={!foldable}>{copy.manualModel}</summary>
      <label className="field">{copy.manualModel}
        <input required aria-invalid={invalid} aria-describedby={describedBy} value={value} placeholder={defaultModelId}
          onFocus={() => setManualOpen(true)} onInvalid={() => setManualOpen(true)}
          onChange={event => { setManualOpen(true); onChange(event.target.value) }} />
      </label>
    </details>
    {catalog && <details className="compact-details">
      <summary>{t('connections.modelInfo.title')}</summary>
      <div className="stack stack--tight">
        {selected && <p><span className="technical">{selected.id}</span> · {t(sourceKey)}</p>}
        {!selected && value.trim() && <p>{t('connections.modelInfo.unlisted')}</p>}
        <p>{observed ? t('connections.modelInfo.observed', { time: formatDateTime(observed, reviewTimeOptions) }) : t('connections.modelInfo.noTime')}</p>
        <p className="field-hint">{t('connections.modelInfo.help')}</p>
      </div>
    </details>}

    {onRefresh && <div className="model-picker__status">
      <button type="button" className="button button--secondary button--compact" disabled={busy || !canRefresh} onClick={onRefresh}>
        {busy ? copy.refreshing : copy.refresh}
      </button>
      {catalog?.stale && <span className="field-hint">{copy.stale}</span>}
      {catalog?.truncated && <span className="field-hint">{copy.truncated}</span>}
      {!busy && !loadError && !catalog?.stale && !known.length && <span className="field-hint">{copy.empty}</span>}
    </div>}
    {loadError && <p role="alert" className="notice notice--danger">{copy.loadFailure}{' '}
      <button type="button" className="danger-link" disabled={busy} onClick={onRetryLoad}>{copy.retry}</button>
    </p>}
    {refreshError && <p role="alert" className="notice notice--danger">
      {configurationChanged ? copy.configurationChanged : copy.refreshFailure}{' '}
      <button type="button" className="danger-link" disabled={busy || !canRefresh} onClick={onRefresh}>{copy.retryRefresh}</button>
    </p>}
  </div>
}
