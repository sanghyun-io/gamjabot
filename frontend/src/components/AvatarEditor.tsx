import { Shuffle } from 'lucide-react'
import { useI18n } from '../i18n'
import AvatarFace from './AvatarFace'
import { avatarParts, randomAvatar, skinColors, accentColors, type AvatarConfig, type AvatarPart } from '../lib/avatarRecipe'

export default function AvatarEditor({ value, onChange, disabled = false, expanded = false }: {
  value: AvatarConfig; onChange: (value: AvatarConfig) => void; disabled?: boolean; expanded?: boolean
}) {
  const { t } = useI18n()
  return <section className="avatar-editor" aria-label={t('avatar.title')}>
    <div className="avatar-editor__preview"><AvatarFace value={value} size={88}/><div><strong>{t('avatar.title')}</strong><div><button type="button" className="button button--secondary button--compact" disabled={disabled} onClick={() => onChange(randomAvatar())}><Shuffle size={15} aria-hidden="true"/>{t('avatar.reroll')}</button></div></div></div>
    <details className="compact-details" open={expanded || undefined}><summary>{t('avatar.customize')}</summary>
      <fieldset disabled={disabled} className="avatar-editor__parts">
        {(Object.keys(avatarParts) as AvatarPart[]).map(part => <label className="field" key={part}>{t(`avatar.${part}`)}
          <select value={value[part]} onChange={event => onChange({ ...value, [part]: Number(event.target.value) })}>
            {Array.from({ length: avatarParts[part] }, (_, index) => <option value={index} key={index}>{t(`avatar.${part}`)} {index + 1}</option>)}
          </select>
          {(part === 'skin' || part === 'accent') && <span className="avatar-editor__swatches" aria-hidden="true">{(part === 'skin' ? skinColors : accentColors).map((color, i) => <span key={color} style={{ background: color, outline: value[part] === i ? '2px solid var(--text-strong)' : undefined }}/>)}</span>}
        </label>)}
      </fieldset>
    </details>
  </section>
}
