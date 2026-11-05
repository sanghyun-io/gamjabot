import AvatarFace from './AvatarFace'
import { validAvatar } from '../lib/avatarRecipe'
import ReadOnlyComparisonValue from './ReadOnlyComparisonValue'
import {useI18n} from '../i18n'
import type {AgentDraft} from '../lib/agentEditorWorkspace'

// Literal read-only values: never resolves connection names from a newer list or edits the draft.
export default function AgentDraftComparison({baseline, received, draft}: {baseline: AgentDraft; received: AgentDraft | null; draft: AgentDraft}) {
  const {t} = useI18n()
  const columns = [
    {id: 'baseline', label: t('agents.compare.baseline'), value: baseline},
    {id: 'received', label: t('agents.compare.received'), value: received},
    {id: 'draft', label: t('agents.compare.draft'), value: draft},
  ]
  const fields: {key: keyof AgentDraft; label: string}[] = [
    {key: 'avatar', label: t('avatar.title')},
    {key: 'name', label: t('agents.name')},
    {key: 'description', label: t('agents.description')},
    {key: 'connectionId', label: t('agents.savedConnection')},
    {key: 'modelId', label: t('agents.exactModel')},
    {key: 'systemPrompt', label: t('agents.systemPrompt')},
    {key: 'temperature', label: t('agents.compare.temperature')},
    {key: 'maxTokens', label: t('agents.compare.maxTokens')},
  ]
  return <section className="stack stack--tight" aria-label={t('agents.compare.title')} style={{minWidth: 0, overflowWrap: 'anywhere'}}>
    <p className="field-hint">{t('agents.compare.scope')}</p><p className="field-hint">{t('editor.compare.readingHelp')}</p>
    {fields.map(field => <div key={field.key} className="stack stack--tight">
      <h3 className="section-title">{field.label}</h3>
      <div style={{display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%, 210px),1fr))', gap: 12, minWidth: 0}}>
        {columns.map(column => {
          const value = column.value?.[field.key]
          const text = !column.value ? t('agents.compare.unavailable')
            : value === '' ? t('agents.compare.empty')
            : field.key === 'connectionId' ? t('agents.savedConnectionNumber', {id: String(value)}) : String(value)
          return <div key={column.id} className="notice stack stack--tight" style={{minWidth: 0}}>
            <strong>{column.label}</strong>
            {column.id !== 'baseline' && column.value && JSON.stringify(value) !== JSON.stringify(baseline[field.key]) && <span className="metadata">{t('editor.compare.differs')}</span>}
            {field.key === 'avatar' && validAvatar(value) ? <AvatarFace value={value} size={60}/> : <ReadOnlyComparisonValue text={text} />}
          </div>
        })}
      </div>
    </div>)}
  </section>
}
