import { useId } from 'react'
import { useI18n } from '../i18n'
import { editorTextLength } from '../lib/editorValidation'

function addressHint(value: string) {
  try { const url = new URL(value); return !['http:', 'https:'].includes(url.protocol) || !url.hostname || !!url.username || !!url.password }
  catch { return true }
}
export function profileInputState(name: string, account: string, origins: string) {
  const nameChars = editorTextLength(name), accountChars = editorTextLength(account), controls = /[\u0000-\u001f\u007f]/
  // Keep exactly the original split/trim/filter dispatch contract, including duplicate lines.
  const lines = origins.split(/\n/).map((value, index) => ({ value: value.trim(), line: index + 1 })).filter(row => !!row.value)
  const tooLongLine = lines.find(row => editorTextLength(row.value) > 2048)?.line
  const nameValid = !!name.trim() && nameChars <= 100 && !controls.test(name), accountValid = accountChars <= 100 && !controls.test(account)
  const originsValid = lines.length >= 1 && lines.length <= 8 && tooLongLine === undefined
  return { nameChars, accountChars, lines, tooLongLine, nameValid, accountValid, originsValid,
    formatHints: lines.slice(0, 8).filter(row => addressHint(row.value)).map(row => row.line), valid: nameValid && accountValid && originsValid }
}
export default function BrowserProfileInputFields({ name, account, origins, state, onChange }: {
  name: string; account: string; origins: string; state: ReturnType<typeof profileInputState>; onChange: (field: 'name' | 'account' | 'origins', value: string) => void
}) {
  const { t } = useI18n(), id = useId()
  return <div className="form-grid">
    <label className="field" htmlFor={`${id}-name`}>{t('browserProfiles.name')}
      <input id={`${id}-name`} required value={name} aria-invalid={name.length > 0 && !state.nameValid || undefined} aria-describedby={`${id}-name-rule ${id}-name-count ${id}-unicode`} onChange={event => onChange('name', event.target.value)}/>
      <span id={`${id}-name-count`} className="metadata">{t('browserProfiles.input.charCount', { count: state.nameChars, max: 100 })}</span>
      <small id={`${id}-name-rule`}>{t('browserProfiles.input.nameRule')}</small>
    </label>
    <label className="field" htmlFor={`${id}-account`}>{t('browserProfiles.account')}
      <input id={`${id}-account`} value={account} aria-invalid={!state.accountValid || undefined} aria-describedby={`${id}-account-rule ${id}-account-count ${id}-account-help ${id}-unicode`} onChange={event => onChange('account', event.target.value)}/>
      <span id={`${id}-account-count`} className="metadata">{t('browserProfiles.input.charCount', { count: state.accountChars, max: 100 })}</span>
      <small id={`${id}-account-rule`}>{t('browserProfiles.input.accountRule')}</small><small id={`${id}-account-help`}>{t('browserProfiles.accountHelp')}</small>
    </label>
    <label className="field" htmlFor={`${id}-origins`}>{t('browserProfiles.origins')}
      <textarea id={`${id}-origins`} required rows={3} value={origins} placeholder="https://example.com" aria-invalid={origins.length > 0 && !state.originsValid || undefined}
        aria-describedby={`${id}-origins-count ${id}-origins-rule ${id}-origins-help ${id}-unicode ${id}-origins-issues`} onChange={event => onChange('origins', event.target.value)}/>
      <span id={`${id}-origins-count`} className="metadata">{t('browserProfiles.input.lineCount', { count: state.lines.length })}</span>
      <small id={`${id}-origins-rule`}>{t('browserProfiles.input.originsRule')}</small><small id={`${id}-origins-help`}>{t('browserProfiles.scopeHelp')}</small>
    </label>
    <div id={`${id}-origins-issues`} className="stack stack--tight">
      {state.lines.length > 8 && <p role="alert" className="notice notice--warning">{t('browserProfiles.input.tooMany')}</p>}
      {state.tooLongLine !== undefined && <p role="alert" className="notice notice--warning">{t('browserProfiles.input.lineTooLong', { line: state.tooLongLine })}</p>}
      {state.formatHints.map(line => <p key={line} className="field-hint">{t('browserProfiles.input.formatHint', { line })}</p>)}
    </div>
    <p id={`${id}-unicode`} className="field-hint">{t('browserProfiles.input.unicode')}</p>
  </div>
}
