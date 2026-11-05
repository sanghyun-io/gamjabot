import { workspaceBlueprintApi, type BlueprintApplyResult, type BlueprintPreview, type WorkspaceBlueprint } from '../api/workspaceBlueprint'
import { useAuthStore } from '../stores/authStore'
import { directoryViewGeneration } from './directoryViewMemory'
import { editorSaveFailure } from '../components/EditorSaveNotice'
import { queryClient } from './queryClient'
import { readTransfer, beginTransferApply, beginTransferExport, beginTransferPreview, finishTransferApply, finishTransferExport, finishTransferPreview, type TransferCommand, type TransferPlan } from './transferWorkspace'
export function transferOwnerCurrent(identity: string, generation: number) { const owner = useAuthStore.getState();return !!owner.token && generation === directoryViewGeneration() && `${owner.user?.id || 0}:${owner.epoch}` === identity }
const positive = (value: number) => Number.isSafeInteger(value) && value > 0
const countsEqual = (left: BlueprintPreview['counts'], right: BlueprintPreview['counts']) => (['connections','agents','projects','project_assignments','room_templates','template_members'] as const).every(key => Number.isSafeInteger(left[key]) && left[key] >= 0 && left[key] === right[key])
function matches(plan: TransferPlan, result: BlueprintApplyResult) {
  const preview = plan.preview
  if (!preview || result.plan_hash !== preview.plan_hash || !countsEqual(result.counts, preview.counts) || !Number.isSafeInteger(result.credentials_unset) || result.credentials_unset < 0 || result.credentials_unset > preview.counts.connections) return false
  const groups = [
    { expected: preview.connections.map(item => ({ ref: item.ref, name: item.target_name })), actual: result.connections.map(item => ({ ref: item.ref, name: item.label, id: item.id })) },
    { expected: preview.agents.map(item => ({ ref: item.ref, name: item.target_name })), actual: result.agents.map(item => ({ ref: item.ref, name: item.name, id: item.id })) },
    { expected: preview.projects.map(item => ({ ref: item.ref, name: item.target_name })), actual: result.projects.map(item => ({ ref: item.ref, name: item.name, id: item.id })) },
    { expected: preview.room_templates.map(item => ({ ref: `${item.project_ref}:${item.ref}`, name: item.target_name })), actual: result.room_templates.map(item => ({ ref: `${item.project_ref}:${item.ref}`, name: item.name, id: item.id })) },
  ]
  return groups.every(group => group.actual.length === group.expected.length && new Set(group.actual.map(item => item.ref)).size === group.actual.length && new Set(group.actual.map(item => item.id)).size === group.actual.length && group.actual.every(item => positive(item.id) && group.expected.some(expected => expected.ref === item.ref && expected.name === item.name)))
}
async function sendApply(identity: string, generation: number, original: TransferCommand) {
  if (!transferOwnerCurrent(identity, generation)) return
  try {
    const result = await workspaceBlueprintApi.apply(original.plan.blueprint, original.plan.preview.plan_hash)
    if (!transferOwnerCurrent(identity, generation)) return
    if (!matches(original.plan, result)) { finishTransferApply(identity, generation, original, { phase: 'unknown', code: 'response_plan_mismatch' });return }
    finishTransferApply(identity, generation, original, { phase: 'succeeded', result })
    for (const key of ['connections','agents','projects','dashboard']) void queryClient.invalidateQueries({ queryKey: [key] })
  } catch (failure) {
    if (!transferOwnerCurrent(identity, generation)) return
    const code = (failure as { response?: { data?: { detail?: { code?: unknown } } } }).response?.data?.detail?.code
    finishTransferApply(identity, generation, original, { phase: editorSaveFailure(failure) === 'rejected' ? 'rejected' : 'unknown', code: typeof code === 'string' ? code : undefined })
  }
}
export function requestTransferApply(identity: string, generation: number, expectedPlanId: string) { if (!transferOwnerCurrent(identity, generation)) return null; const original = beginTransferApply(identity, generation, expectedPlanId);if (original) void sendApply(identity, generation, original);return original }
export function requestTransferPreview(identity: string, generation: number) {
  if (!transferOwnerCurrent(identity, generation)) return null
  const blueprint = JSON.parse(readTransfer(identity).raw) as WorkspaceBlueprint
  const original = beginTransferPreview(identity, generation, blueprint)
  if (original) void (async () => {
    try {
      const value = await workspaceBlueprintApi.preview(original.blueprint)
      if (!transferOwnerCurrent(identity, generation)) return
      if (!/^[a-f0-9]{64}$/.test(value.plan_hash) || value.version !== 1 || !countsEqual(value.counts, { connections: original.blueprint.connections.length, agents: original.blueprint.agents.length, projects: original.blueprint.projects.length, project_assignments: original.blueprint.projects.reduce((n,p)=>n+p.agent_refs.length,0), room_templates: original.blueprint.projects.reduce((n,p)=>n+p.room_templates.length,0), template_members: original.blueprint.projects.reduce((n,p)=>n+p.room_templates.reduce((a,t)=>a+t.agent_refs.length,0),0) })) throw new Error('Plan response mismatch')
      finishTransferPreview(identity, generation, original, { phase: 'succeeded', preview: value })
    } catch (failure) { if (transferOwnerCurrent(identity, generation)) finishTransferPreview(identity, generation, original, { phase: 'rejected', code: (failure as { response?: { status?: number } }).response?.status === 413 ? 'tooLarge' : 'preview' }) }
  })()
  return original
}
export function requestTransferExport(identity: string, generation: number, mode: 'copy' | 'download' | 'save' | 'preview') {
  if (!transferOwnerCurrent(identity, generation)) return null
  const original = beginTransferExport(identity, generation, mode)
  if (original) void (async () => { try { const result = await workspaceBlueprintApi.export();if (transferOwnerCurrent(identity, generation)) finishTransferExport(identity, generation, original, result) } catch { if (transferOwnerCurrent(identity, generation)) finishTransferExport(identity, generation, original) } })()
  return original
}
