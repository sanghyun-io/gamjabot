import type { ResultCommand, ResultKind, ResultSave, ResultState } from '../api/roomResults'
import { directoryViewGeneration } from './directoryViewMemory'

export type ResultTarget = Readonly<{ id: string; version: number; title: string }>
export type ResultDraft = Readonly<{
  editId: string; sourceId: string; sourceSeq: number; sourceKind: 'text' | 'file';
  kind: ResultKind; title: string; url: string; evidence: string; target: ResultTarget | null;
  revision: number; dirty: boolean;
}>
export type ResultSaveRequest = Readonly<{
  key: string; fingerprint: string; body: Readonly<ResultSave>; target: ResultTarget | null;
  sourceSeq: number; draftId: string; draftRevision: number; viewRevision: number;
  status: 'sending' | 'accepted' | 'unknown' | 'rejected' | 'conflict';
  receipt?: ResultCommand; mismatch?: boolean; acknowledged?: boolean;
}>
export type ResultCommandIntent =
  | Readonly<{ operation: 'state'; state: ResultState; expected_state_revision: number }>
  | Readonly<{ operation: 'link-report'; report: 'unchecked' | 'reported_bad'; expected_state_revision: number }>
  | Readonly<{ operation: 'review'; review_event_id: string; reviewSeq: number }>
export type ResultChangeRequest = Readonly<{
  key: string; fingerprint: string; target: ResultTarget; intent: ResultCommandIntent;
  state: ResultState; viewRevision: number;
  status: 'sending' | 'accepted' | 'unknown' | 'rejected' | 'conflict';
  receipt?: ResultCommand; mismatch?: boolean; acknowledged?: boolean;
}>
export type ResultListFilter = Readonly<{ query: string; kind: ResultKind | 'all'; state: ResultState | 'all'; order?: 'received' | 'title' }>
export type ResultSaveWorkspace = Readonly<{
  listFilter: ResultListFilter; listStart?: string | null;
  drafts: Readonly<Record<string, ResultDraft>>; activeSourceId: string | null;
  selection: { id: string; version: number } | null; compare: boolean; comparisonBases: Readonly<Record<string, number>>; viewRevision: number;
  requests: readonly ResultSaveRequest[];
  commands: readonly ResultChangeRequest[];
}>
const initial: ResultSaveWorkspace = Object.freeze({ listStart: null, listFilter: Object.freeze({ query: '', kind: 'all', state: 'all', order: 'received' }), drafts: {}, activeSourceId: null,
  selection: null, compare: false, comparisonBases: Object.freeze({}), viewRevision: 0, requests: [], commands: [] })
export const resultWorkspaceSending = (state: ResultSaveWorkspace) =>
  [...state.requests, ...state.commands].some(request => request.status === 'sending')
export const resultWorkspaceUncertain = (state: ResultSaveWorkspace) =>
  [...state.requests, ...state.commands].some(request => (request.status === 'unknown' || request.mismatch) && !request.acknowledged)
const workspaces = new Map<number, { identity: string; state: ResultSaveWorkspace }>()
const listeners = new Set<() => void>()
export function readResultWorkspace(identity: string, roomId: number) {
  const value = workspaces.get(roomId)
  return value?.identity === identity ? value.state : initial
}
export function subscribeResultWorkspaces(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearResultWorkspaces() { workspaces.clear(); listeners.forEach(listener => listener()) }
export function updateResultWorkspace(identity: string, roomId: number, generation: number,
  update: (state: ResultSaveWorkspace) => ResultSaveWorkspace) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readResultWorkspace(identity, roomId), next = update(previous)
  if (previous === next) return false
  workspaces.set(roomId, { identity, state: Object.freeze(next) })
  listeners.forEach(listener => listener())
  return true
}
