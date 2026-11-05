import { useEffect, useRef, useState } from 'react'
import { useI18n } from '../i18n'
import type { MessageKey } from '../i18n/locales/en'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from '../lib/directoryViewMemory'
import { useImeFormGuard } from '../lib/useImeFormGuard'
import { agentPresetPrefix, readAgentPresets, saveAgentPreset, deleteAgentPreset,
  type AgentPresetFilters, type StoredAgentPreset } from '../lib/agentFilterPresets'

export default function AgentFilterPresets({ identity, current, canSave, onApply, describe }: {
  identity: string; current: AgentPresetFilters; canSave: boolean
  onApply: (filters: AgentPresetFilters) => void
  describe: (filters: AgentPresetFilters) => { label: string; value: string }[]
}) {
  const { t } = useI18n()
  const owner = useRef(useAuthStore.getState().user?.id || 0).current
  const generation = useRef(directoryViewGeneration()).current
  const [rows, setRows] = useState<StoredAgentPreset[]>([]), [skipped, setSkipped] = useState(0)
  const [selected, setSelected] = useState(''), [name, setName] = useState('')
  const [notice, setNotice] = useState<MessageKey | null>(null), [failed, setFailed] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<StoredAgentPreset | null>(null)
  const nameInput = useRef<HTMLInputElement>(null), chooser = useRef<HTMLSelectElement>(null)
  const guard = useImeFormGuard(identity)
  const isCurrent = () => {
    const state = useAuthStore.getState()
    return !!state.token && state.user?.id === owner && `${state.user.id}:${state.epoch}` === identity && generation === directoryViewGeneration()
  }
  const failure = (error: unknown) => {
    if (!isCurrent()) return
    const code = error instanceof Error ? error.message : ''
    setFailed(true)
    setNotice(code === 'limit' ? 'agents.presets.limit' : code === 'duplicate' ? 'agents.presets.duplicate'
      : code === 'invalid' ? 'agents.presets.invalid' : code === 'changed' ? 'agents.presets.changed' : 'agents.presets.unavailable')
  }
  const refresh = () => {
    if (!isCurrent()) return
    try {
      const result = readAgentPresets(owner)
      setRows(result.items); setSkipped(result.skipped)
      setSelected(value => result.items.some(row => row.id === value) ? value : '')
      setPendingDelete(value => value && result.items.some(row => row.id === value.id && row.raw === value.raw) ? value : null)
    } catch (error) { failure(error) }
  }
  useEffect(() => {
    refresh()
    const changed = (event: StorageEvent) => { if (event.key === null || event.key.startsWith(agentPresetPrefix(owner))) refresh() }
    window.addEventListener('storage', changed); window.addEventListener('focus', refresh)
    return () => { window.removeEventListener('storage', changed); window.removeEventListener('focus', refresh) }
  }, [identity, owner, generation])
  const chosen = rows.find(row => row.id === selected)
  const apply = () => {
    if (!isCurrent() || !chosen) return
    try {
      const latest = readAgentPresets(owner).items.find(row => row.id === chosen.id && row.raw === chosen.raw)
      if (!latest) throw new Error('changed')
      onApply({ ...latest.filters }); setPendingDelete(null); setFailed(false); setNotice('agents.presets.applied')
    } catch (error) { failure(error); refresh() }
  }
  return <section className="surface stack stack--tight" aria-label={t('agents.presets.title')}>
    <h3 className="section-title">{t('agents.presets.title')}</h3>
    <p className="field-hint">{t('agents.presets.scope')}</p>
    <div className="page-actions" style={{ alignItems: 'end' }}>
      <label className="field" style={{ flex: '1 1 16rem', minWidth: 0 }}>{t('agents.presets.choose')}
        <select ref={chooser} value={selected} onChange={event => { if (!isCurrent()) return; setSelected(event.target.value); setPendingDelete(null); setNotice(null) }}>
          <option value="">{t(rows.length ? 'agents.presets.choose' : 'agents.presets.empty')}</option>
          {rows.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
        </select>
      </label>
      <button type="button" className="button button--secondary" disabled={!chosen} onClick={apply}>{t('agents.presets.apply')}</button>
    </div>
    {chosen && <details className="compact-details"><summary>{t('agents.presets.contents')}</summary>
      <dl className="stack stack--tight" style={{ overflowWrap: 'anywhere' }}>{describe(chosen.filters).map(row => <div key={row.label}><dt className="metadata">{row.label}</dt><dd style={{ whiteSpace: 'pre-wrap' }}>{row.value}</dd></div>)}</dl>
      <button type="button" className="button button--secondary danger-link" onClick={() => { if (isCurrent()) { setPendingDelete(chosen); setNotice(null) } }}>{t('agents.presets.delete')}</button>
    </details>}
    {pendingDelete && <div className="notice notice--warning stack stack--tight">
      <p>{t('agents.presets.deleteQuestion', { name: pendingDelete.name })}</p><div className="page-actions">
        <button type="button" className="button button--secondary danger-link" onClick={() => {
          if (!isCurrent()) return
          try {
            deleteAgentPreset(owner, pendingDelete); chooser.current?.focus({ preventScroll: true })
            setPendingDelete(null); setFailed(false); setNotice('agents.presets.deleted'); refresh()
          } catch (error) { failure(error); refresh() }
        }}>{t('agents.presets.confirmDelete')}</button>
        <button type="button" className="button button--secondary" onClick={() => { if (isCurrent()) { chooser.current?.focus({ preventScroll: true }); setPendingDelete(null) } }}>{t('common.cancel')}</button>
      </div>
    </div>}
    <details className="compact-details"><summary>{t('agents.presets.saveCurrent')}</summary>
      <form className="stack stack--tight" {...guard.handlers} onSubmit={event => {
        event.preventDefault()
        if (guard.isSubmitBlocked(event) || !isCurrent() || !canSave) return
        try {
          const saved = saveAgentPreset(owner, name, { ...current })
          refresh(); setSelected(saved.id); setName(''); setPendingDelete(null); setFailed(false); setNotice('agents.presets.saved')
          nameInput.current?.focus({ preventScroll: true })
        } catch (error) { failure(error) }
      }}>
        <label className="field">{t('agents.presets.name')}<input ref={nameInput} value={name} maxLength={80} required onChange={event => { if (isCurrent()) setName(event.target.value) }} /></label>
        <button type="submit" className="button button--secondary" disabled={!name.trim() || !canSave}>{t('agents.presets.save')}</button>
      </form>
    </details>
    {skipped > 0 && <p className="notice notice--warning">{t('agents.presets.unreadable')}</p>}
    {notice && <p role={failed ? 'alert' : 'status'} className={`notice ${failed ? 'notice--warning' : 'notice--info'}`}>{t(notice)}</p>}
  </section>
}
