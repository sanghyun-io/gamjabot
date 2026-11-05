import { useEffect, useRef } from 'react'
import type { AuthMethodId } from '../../api/connections'

export interface AuthMethodChoice {
  method: AuthMethodId
  label: string
  enabled: boolean
  reason?: string
}

export function AuthMethodPicker({ legend, name, value, choices, disabled, focusVersion = 0, onChange }: {
  legend: string
  name: string
  value: AuthMethodId
  choices: AuthMethodChoice[]
  disabled?: boolean
  focusVersion?: number
  onChange: (method: AuthMethodId) => void
}) {
  const firstEnabled = choices.find((choice) => choice.enabled)
  const focusRef = useRef<HTMLInputElement>(null)
  useEffect(() => { if (focusVersion > 0) focusRef.current?.focus() }, [focusVersion])
  return <fieldset className="method-picker">
    <legend className="section-title">{legend}</legend>
    {choices.map((choice) => <label key={choice.method} className={`method-picker__option${choice.enabled ? '' : ' method-picker__option--disabled'}`}>
      <span><input ref={choice.method === firstEnabled?.method ? focusRef : undefined} type="radio" name={name}
        value={choice.method} checked={value === choice.method} disabled={disabled || !choice.enabled}
        onChange={() => onChange(choice.method)} /> {choice.label}</span>
      {!choice.enabled && choice.reason && <span className="field-hint">{choice.reason}</span>}
    </label>)}
  </fieldset>
}
