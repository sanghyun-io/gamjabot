import { Fragment } from 'react'
import { useI18n } from '../../i18n'
import type { AuthMethodDescriptor, AuthMethodId, ProtocolId, ProviderDescriptor, PublicFieldDescriptor, PublicFieldValue } from '../../api/connections'
import { isUsableAuthMethod } from '../../api/connections'
import { AuthMethodPicker } from './AuthMethodPicker'

export interface MethodCopy {
  methodLegend: string
  protocol: string
  details: string
  methodNames: Record<AuthMethodId, string>
  usageUnverified: string
  eligibilityUnknown: string
  unavailable: string
  baseUrl: string
  allowLocalHttp: string
  fieldNames: Record<string, string>
  otherField: string
}

export function unavailableReason(provider: ProviderDescriptor, method: AuthMethodDescriptor, copy: MethodCopy) {
  if (provider.usage_classification === 'unverified') return copy.usageUnverified
  if (method.external_eligibility === 'unknown') return copy.eligibilityUnknown
  return copy.unavailable
}

function publicFieldName(field: PublicFieldDescriptor, copy: MethodCopy) {
  return copy.fieldNames[field.field_id]
    || (field.field_id === 'base_url' ? copy.baseUrl : field.field_id === 'allow_local_http' ? copy.allowLocalHttp : `${copy.otherField} (${field.field_id})`)
}

export function MethodFieldsReview({ provider, protocolId, values, copy }: {
  provider: ProviderDescriptor; protocolId: ProtocolId; values: Record<string, PublicFieldValue>; copy: MethodCopy
}) {
  const { t } = useI18n()
  return <><dl className="connection-confirm">
    {provider.fields.map(field => <Fragment key={field.field_id}>
      <dt>{publicFieldName(field, copy)}</dt>
      <dd>{field.kind === 'boolean' ? (typeof values[field.field_id] === 'boolean' ? t(values[field.field_id] === true ? 'connections.enabled' : 'connections.disabled') : t('connections.reviewUnset'))
        : String(values[field.field_id] ?? '') || t('connections.reviewUnset')}</dd>
    </Fragment>)}
  </dl><details className="compact-details"><summary>{copy.details}</summary><dl className="connection-confirm"><dt>{copy.protocol}</dt><dd className="technical">{protocolId}</dd></dl></details></>
}

export function MethodFields({ provider, methodId, protocolId, values, focusVersion, disabled, copy,
  onMethodChange, onProtocolChange, onValuesChange }: {
  provider: ProviderDescriptor
  methodId: AuthMethodId
  protocolId: ProtocolId
  values: Record<string, PublicFieldValue>
  focusVersion: number
  disabled?: boolean
  copy: MethodCopy
  onMethodChange: (method: AuthMethodId) => void
  onProtocolChange: (protocol: ProtocolId) => void
  onValuesChange: (values: Record<string, PublicFieldValue>) => void
}) {
  const fieldName = (field: PublicFieldDescriptor) => publicFieldName(field, copy)
  const fixedApiVersion = provider.provider_id === 'azure-openai' && protocolId === 'azure_openai'
    ? provider.fields.find((field) => field.field_id === 'api_version') : undefined
  const editableFields = provider.fields.filter((field) => field !== fixedApiVersion)

  const renderField = (field: PublicFieldDescriptor) => field.kind === 'boolean'
    ? <label key={field.field_id} className="check-row"><input type="checkbox" disabled={disabled}
        checked={values[field.field_id] === true}
        onChange={(event) => onValuesChange({ ...values, [field.field_id]: event.target.checked })} />
        {fieldName(field)}
      </label>
    : <label key={field.field_id} className="field">{fieldName(field)}
        <input type={field.kind === 'url' ? 'url' : 'text'} required={field.required} disabled={disabled}
          maxLength={field.max_length || undefined} value={String(values[field.field_id] || '')}
          onChange={(event) => onValuesChange({ ...values, [field.field_id]: event.target.value })} />
      </label>

  return <div className="method-fields stack stack--tight">
    <AuthMethodPicker legend={copy.methodLegend} name="auth-method" value={methodId} disabled={disabled} focusVersion={focusVersion}
      choices={provider.methods.map((method) => ({ method: method.method_id, label: copy.methodNames[method.method_id],
        enabled: isUsableAuthMethod(method), reason: unavailableReason(provider, method, copy) }))}
      onChange={onMethodChange} />
    {editableFields.some((field) => field.required) && <div className="form-grid">
      {editableFields.filter((field) => field.required).map(renderField)}
    </div>}
    {(provider.protocols.length > 1 || fixedApiVersion || editableFields.some(field => !field.required)) && <details className="compact-details"><summary>{copy.details}</summary><div className="form-grid">
      {provider.protocols.length > 1 && <label className="field">{copy.protocol}
        <select value={protocolId} disabled={disabled} onChange={(event) => onProtocolChange(event.target.value as ProtocolId)}>
          {provider.protocols.map((protocol) => <option key={protocol} value={protocol}>{protocol}</option>)}
        </select>
      </label>}
      {fixedApiVersion && <div className="field"><span>{fieldName(fixedApiVersion)}</span>
        <span className="technical">{String(values.api_version ?? '')}</span>
      </div>}
      {editableFields.filter((field) => !field.required).map(renderField)}
    </div></details>}
  </div>
}
