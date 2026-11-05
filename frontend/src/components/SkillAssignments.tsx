import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { skillsApi, type SkillBinding } from '../api/skills'
import { useBotScope } from '../lib/useBotScope'
import { useAuthStore } from '../stores/authStore'
import { useI18n } from '../i18n'

type Phase = 'sending'|'unknown'|'conflict'|'rejected'
const pending = new Map<string, Phase>()
useAuthStore.subscribe((current, previous) => { if (!current.token || current.epoch !== previous.epoch || current.user?.id !== previous.user?.id) pending.clear() })
export default function SkillAssignments({ agentId }: { agentId: number }) {
  const { t } = useI18n(), scope = useBotScope(), cache = useQueryClient(), lock = useRef(false), key = `${scope.identity}:${agentId}`
  const [open, setOpen] = useState(false), [phase, setPhaseState] = useState<Phase | null>(() => pending.get(key) || null)
  const setPhase = (value: Phase | null) => { if (value) pending.set(key,value); else pending.delete(key); setPhaseState(value) }
  useEffect(() => { setPhaseState(pending.get(key) || null); return () => { if (pending.get(key) === 'sending') pending.set(key,'unknown') } }, [key])
  const skills = useQuery({ queryKey: ['skills',scope.identity,''], enabled: open, retry:false, queryFn: async () => { const data = await skillsApi.list(); if (!scope.canUse()) throw new Error('Owner changed'); return data } })
  const bindingKey = ['skill-bindings',scope.identity,agentId] as const
  const bindings = useQuery({ queryKey:bindingKey, enabled:open, retry:false, queryFn: async () => { const data = await skillsApi.bindings(agentId); if (!scope.canUse()) throw new Error('Owner changed'); return data } })
  const read = async () => { if (!scope.canUse() || lock.current) return; lock.current = true; try { const result = await bindings.refetch({ cancelRefetch:false }); if (scope.canUse() && result.isSuccess) setPhase(null) } finally { lock.current=false } }
  const change = async (binding: SkillBinding) => {
    if (!scope.canUse() || lock.current || phase || bindings.isFetching || !bindings.isSuccess || !bindings.data.items.some(x=>x.skill_id===binding.skill_id && x.revision===binding.revision && x.enabled===binding.enabled)) return
    lock.current=true; setPhase('sending')
    try { await cache.cancelQueries({queryKey:bindingKey}); await skillsApi.setBinding(agentId,binding.skill_id,binding.revision,!binding.enabled); if (scope.canUse()) { const result=await bindings.refetch({cancelRefetch:false}); setPhase(result.isSuccess ? null : 'unknown') } }
    catch (failure) { if (scope.canUse()) { const response=(failure as {response?:{status?:number;data?:{detail?:{code?:string}}}}).response; setPhase(response?.status===409 ? response.data?.detail?.code === 'skill_binding_conflict' ? 'conflict' : 'rejected' : 'unknown') } }
    finally { lock.current=false }
  }
  const available = skills.data || []
  const incomplete = skills.isSuccess && bindings.isSuccess && available.some(item => !bindings.data.items.some(row => row.skill_id === item.skill_id))
  return <details className="skill-assignments" open={open} onToggle={event => setOpen(event.currentTarget.open)}><summary>{t('skills.botSkills')}</summary>{open && <div className="stack stack--tight">
    <p className="metadata">{t('skills.botHelp')}</p>
    {(skills.isError || bindings.isError || incomplete) && <p role="alert">{t('skills.choiceFailed')} <button className="text-link" onClick={() => { if (scope.canUse()) { void skills.refetch({cancelRefetch:false}); void read() } }}>{t('skills.reload')}</button></p>}
    {phase && <p role={phase==='sending'?'status':'alert'}>{t(phase==='sending'?'skills.saving':phase==='conflict'?'skills.conflict':phase==='rejected'?'skills.bindingRejected':'skills.unknown')}{phase!=='sending' && <> <button className="text-link" onClick={() => { void read() }}>{t('skills.reload')}</button></>}</p>}
    {skills.isSuccess && bindings.isSuccess && !available.length && <p className="metadata">{t('skills.noSkills')}</p>}
    {skills.isSuccess && bindings.isSuccess && available.map(item => { const binding=bindings.data.items.find(row=>row.skill_id===item.skill_id); return binding ? <label key={item.skill_id} className="skill-assignment"><input type="checkbox" checked={binding.enabled} disabled={!!phase || bindings.isFetching || incomplete || !item.active && !binding.enabled} onChange={() => { void change(binding) }}/><span>{item.name}{!item.active && <> · {t('skills.inactive')}</>}</span></label> : null })}
    <Link className="text-link" to="/skills">{t('skills.manage')}</Link>
  </div>}</details>
}
