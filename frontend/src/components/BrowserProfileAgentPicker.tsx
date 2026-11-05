import { useMemo, useState } from 'react'
import type { Agent } from '../api/agents'
import type { RoomMember } from '../api/conversations'
import type { BrowserProfile } from './BrowserProfileActionDialog'
import DirectorySearch, { directoryMatcher } from './DirectorySearch'
import { useI18n } from '../i18n'

export type ProfileAgentChoice = { id: number; name: string; state: 'available' | 'occupied' | 'unavailable' | 'unknown'; profiles: string[] }
export function profileAgentChoices({ members, agents, profiles, profileId, selected, ownerId, ready }: {
  members: RoomMember[]; agents: Agent[]; profiles: BrowserProfile[]; profileId?: string; selected: readonly number[]; ownerId: number; ready: boolean
}): ProfileAgentChoice[] {
  const owned = new Map(agents.filter(agent => agent.user_id === ownerId).map(agent => [agent.id, agent]))
  const activeMembers = new Set(members.filter(member => member.is_active).map(member => member.agent_id))
  const occupied = new Map<number, string[]>()
  for (const profile of profiles) if (profile.id !== profileId) for (const id of profile.agent_ids) occupied.set(id, [...(occupied.get(id) || []), profile.name])
  const candidates = [...new Set([...members.filter(member => member.is_active && owned.get(member.agent_id)?.is_active).map(member => member.agent_id), ...selected])]
  return candidates.map(id => ({ id, name: owned.get(id)?.name || `Agent #${id}`, profiles: occupied.get(id) || [],
    state: !ready ? 'unknown' : !activeMembers.has(id) || !owned.get(id)?.is_active ? 'unavailable' : occupied.has(id) ? 'occupied' : 'available' }))
}
export default function BrowserProfileAgentPicker({ choices, selected, ready, refreshing, disabled, onChange, onRefresh }: {
  choices: ProfileAgentChoice[]; selected: readonly number[]; ready: boolean; refreshing: boolean; disabled: boolean
  onChange: (id: number, checked: boolean) => void; onRefresh: () => void
}) {
  const { t } = useI18n(), [query, setQuery] = useState('')
  const matches = useMemo(() => {
    if (!query.trim()) return choices
    const match = directoryMatcher(query)
    return choices.filter(choice => match([choice.name, String(choice.id), `#${choice.id}`, ...choice.profiles], choice.id))
  }, [choices, query])
  const shown = new Set(matches.map(choice => choice.id)), pinned = choices.filter(choice => selected.includes(choice.id) && !shown.has(choice.id))
  const blocked = choices.some(choice => selected.includes(choice.id) && choice.state !== 'available')
  return <fieldset className="stack stack--tight"><legend>{t('browserProfiles.agents')}</legend>
    <p className="field-hint">{t('browserProfiles.agentPicker.help')}</p>
    <p className="metadata">{t('browserProfiles.agentPicker.selected', { count: selected.length })}</p>
    <p className="field-hint">{t('browserProfiles.agentPicker.limit')}</p>
    {!selected.length && <p className="notice notice--info">{t('browserProfiles.agentPicker.manual')}</p>}
    {!ready && <p role="status" className="notice notice--warning">{t('browserProfiles.agentPicker.unknown')}</p>}
    <DirectorySearch exactId value={query} onChange={setQuery} hint={t('browserProfiles.agentPicker.search')} shown={matches.length} total={choices.length} known loading={refreshing} preventSubmit disabled={disabled}/>
    {pinned.length > 0 && <p className="field-hint">{t('browserProfiles.agentPicker.pinned')}</p>}
    {ready && !choices.length && <p>{t('browserProfiles.agentPicker.empty')}</p>}
    {!!query && !matches.length && <p role="status">{t('browserProfiles.agentPicker.noMatches')}</p>}
    {[...pinned, ...matches].map(choice => <label key={choice.id} className="stack stack--tight" style={{ overflowWrap: 'anywhere' }}>
      <span className="inline-actions"><input type="checkbox" checked={selected.includes(choice.id)} disabled={disabled || !selected.includes(choice.id) && (choice.state !== 'available' || selected.length >= 16)} onChange={event => onChange(choice.id, event.target.checked)}/>{choice.name} · #{choice.id}</span>
      {choice.state === 'occupied' && <small>{t('browserProfiles.agentPicker.occupied', { profiles: choice.profiles.join(' · ') })}</small>}
      {choice.state === 'unavailable' && <small>{t('browserProfiles.agentPicker.unavailable')}</small>}
    </label>)}
    {blocked && <p role="status" className="notice notice--warning">{t('browserProfiles.agentPicker.blocked')}</p>}
    <button type="button" className="button button--secondary button--compact" disabled={disabled || refreshing} onClick={onRefresh}>{t('browserProfiles.agentPicker.refresh')}</button>
  </fieldset>
}
