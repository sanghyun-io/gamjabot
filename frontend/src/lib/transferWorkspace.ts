import type { BlueprintApplyResult, BlueprintPreview, WorkspaceBlueprint } from '../api/workspaceBlueprint'
import { directoryViewGeneration } from './directoryViewMemory'
export type TransferInputSource = Readonly<{ kind: 'file' | 'manual'; name?: string; readAt: string; bytes: number; modified: boolean }>
export type TransferPhase = 'pending' | 'succeeded' | 'rejected' | 'unknown'
export type TransferPlan = Readonly<{ id: string; raw: string; inputSource: TransferInputSource | null; version: number; blueprint: WorkspaceBlueprint; phase: TransferPhase; preview?: BlueprintPreview; consumed: boolean; code?: string; startedAt: string; finishedAt?: string }>
export type TransferCommand = Readonly<{ id: string; plan: TransferPlan & { preview: BlueprintPreview }; phase: TransferPhase; result?: BlueprintApplyResult; code?: string; startedAt: string; finishedAt?: string; waitStopped?: boolean; acknowledged?: boolean }>
export type TransferExport = Readonly<{ id: string; mode: 'copy' | 'download' | 'save' | 'preview'; phase: TransferPhase; result?: WorkspaceBlueprint; startedAt: string; finishedAt?: string }>
type Workspace = Readonly<{ raw: string; inputSource: TransferInputSource | null; version: number; open: boolean; plan: TransferPlan | null; commands: readonly TransferCommand[]; pending: string | null; exported: TransferExport | null; receivedExport: TransferExport | null }>
const initial: Workspace = { raw: '', inputSource: null, version: 0, open: false, plan: null, commands: [], pending: null, exported: null, receivedExport: null }
const rows = new Map<string, Workspace>(), listeners = new Set<() => void>();let sequence = 0
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value))
const emit = () => { sequence++; listeners.forEach(listener => listener()) }
export const transferVersion = () => sequence
export function subscribeTransfer(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export const readTransfer = (identity: string) => rows.get(identity) || initial
export function clearTransfers() { rows.clear(); emit() }
const valid = (generation: number) => generation === directoryViewGeneration()
export function editTransfer(identity: string, generation: number, raw: string, source?: TransferInputSource | null) {
  const previous = readTransfer(identity);if (!valid(generation) || previous.pending || new TextEncoder().encode(raw).byteLength > 2 * 1024 * 1024) return false
  if (raw === previous.raw && source === undefined) return true
  const inputSource = source !== undefined ? source && copy(source) : previous.inputSource?.kind === 'file' ? { ...previous.inputSource, modified: true } : { kind: 'manual' as const, readAt: new Date().toISOString(), bytes: new TextEncoder().encode(raw).byteLength, modified: false }
  rows.set(identity, { ...previous, raw, inputSource, version: previous.version + 1 });emit();return true
}
export function openTransfer(identity: string, generation: number, open: boolean) { if (!valid(generation)) return; const previous = readTransfer(identity);if (previous.open === open) return;rows.set(identity, { ...previous, open });emit() }
export function beginTransferPreview(identity: string, generation: number, blueprint: WorkspaceBlueprint) {
  const previous = readTransfer(identity);if (!valid(generation) || previous.pending || previous.plan?.phase === 'pending') return null
  const plan: TransferPlan = { id: crypto.randomUUID(), raw: previous.raw, inputSource: previous.inputSource && copy(previous.inputSource), version: previous.version, blueprint: copy(blueprint), phase: 'pending', consumed: false, startedAt: new Date().toISOString() }
  rows.set(identity, { ...previous, plan });emit();return plan
}
export function finishTransferPreview(identity: string, generation: number, original: TransferPlan, outcome: Pick<TransferPlan, 'phase' | 'preview' | 'code'>) {
  const previous = readTransfer(identity);if (!valid(generation) || previous.plan?.id !== original.id) return
  rows.set(identity, { ...previous, plan: { ...original, ...copy(outcome), finishedAt: new Date().toISOString() } });emit()
}
export function transferBlocked(identity: string) { const previous = readTransfer(identity);return !!previous.pending || previous.commands.some(command => command.phase === 'unknown' && !command.acknowledged) }
export function beginTransferApply(identity: string, generation: number, expectedPlanId: string) {
  const previous = readTransfer(identity), plan = previous.plan
  if (!valid(generation) || transferBlocked(identity) || !plan || plan.id !== expectedPlanId || plan.phase !== 'succeeded' || !plan.preview || plan.consumed || plan.raw !== previous.raw || plan.version !== previous.version) return null
  const command: TransferCommand = { id: crypto.randomUUID(), plan: copy({ ...plan, preview: plan.preview }), phase: 'pending', startedAt: new Date().toISOString() }
  rows.set(identity, { ...previous, plan: { ...plan, consumed: true }, commands: [...previous.commands, command], pending: command.id });emit();return command
}
export function finishTransferApply(identity: string, generation: number, original: TransferCommand, outcome: Pick<TransferCommand, 'phase' | 'result' | 'code'>) {
  const previous = readTransfer(identity)
  if (!valid(generation) || !previous.commands.some(command => command.id === original.id && (command.phase === 'pending' || command.phase === 'unknown' && command.waitStopped))) return
  rows.set(identity, { ...previous, pending: previous.pending === original.id ? null : previous.pending, commands: previous.commands.map(command => command.id === original.id ? { ...command, ...copy(outcome), finishedAt: new Date().toISOString() } : command) });emit()
}
export function stopTransferWait(identity: string, generation: number, id: string) {
  const previous = readTransfer(identity);if (!valid(generation) || !previous.commands.some(command => command.id === id && command.phase === 'pending')) return
  rows.set(identity, { ...previous, pending: previous.pending === id ? null : previous.pending, commands: previous.commands.map(command => command.id === id ? { ...command, phase: 'unknown' as const, waitStopped: true } : command) });emit()
}
export function acknowledgeTransfer(identity: string, generation: number, id: string) {
  const previous = readTransfer(identity);if (!valid(generation)) return
  rows.set(identity, { ...previous, commands: previous.commands.map(command => command.id === id && command.phase === 'unknown' ? { ...command, acknowledged: true } : command) });emit()
}
export function beginTransferExport(identity: string, generation: number, mode: TransferExport['mode']) {
  const previous = readTransfer(identity);if (!valid(generation) || previous.pending || previous.exported?.phase === 'pending') return null
  const exported: TransferExport = { id: crypto.randomUUID(), mode, phase: 'pending', startedAt: new Date().toISOString() }
  rows.set(identity, { ...previous, exported });emit();return exported
}
export function finishTransferExport(identity: string, generation: number, original: TransferExport, result?: WorkspaceBlueprint) {
  const previous = readTransfer(identity);if (!valid(generation) || previous.exported?.id !== original.id) return
  const exported: TransferExport = { ...original, phase: result ? 'succeeded' : 'rejected', result: result && copy(result), finishedAt: new Date().toISOString() }
  rows.set(identity, { ...previous, exported, receivedExport: result ? exported : previous.receivedExport });emit()
}
