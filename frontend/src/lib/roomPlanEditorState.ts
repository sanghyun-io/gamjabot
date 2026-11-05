import type { PlanRejection } from './roomPlanFeedback'
import { directoryViewGeneration } from './directoryViewMemory'

export type Criterion = { id: string; text: string; done: boolean; evidence_seq: number | null }
export type RemovedGoalCriterion = { criterion: Readonly<Criterion>; index: number }
export type Goal = { revision: number; title: string; criteria: Criterion[]; complete: boolean }
export type Watch = { id: string; revision: number; name: string; url: string; interval_minutes: number;
  timezone: string; mode: 'text_change' | 'keyword_appears' | 'keyword_disappears'; keyword: string;
  stop_after_change: boolean; state: 'active' | 'paused' | 'ended'; next_at: string | null;
  last_at: string | null; last_error: string | null; failures: number; observing: boolean }
export type WatchForm = { id?: string; revision?: number; command_id: string; name: string; url: string;
  interval_minutes: string; timezone: string; mode: Watch['mode']; keyword: string;
  stop_after_change: boolean; enabled: boolean }
export type PlanData = { goal: Goal; watches: Watch[];
  evidence: { seq: number; event_id: string; kind: string; actor: string; label: string }[] }
export type PlanKind = 'goal' | 'watch' | 'control'
export type PlanResult = { kind: 'goal'; value: Goal } | { kind: 'watch' | 'control'; value: Watch }
export type PlanEditor = Readonly<{
  goalCriteriaOpen?: Readonly<Record<string, boolean>>;
  removedGoalCriterion?: Readonly<RemovedGoalCriterion> | null;
  draft: Goal | null; watch: WatchForm | null; watchDrafts?: readonly WatchForm[]; watchSession?: string; goalRevision: number; watchRevision: number;
  goalValidation: boolean; watchValidation: boolean; operation: string | null; notice: '' | 'saving' | 'saved'; error: '' | 'unknown' | 'changed' | 'failure';
  rejection: { reason: PlanRejection; kind: PlanKind; subject: string } | null; result: PlanResult | null;
  uncertain: readonly { id: string; kind: PlanKind; subject: string }[];
}>
const initial: PlanEditor = Object.freeze({ goalCriteriaOpen: Object.freeze({}), draft: null, removedGoalCriterion: null, watch: null, watchDrafts: Object.freeze([]), watchSession: '', goalRevision: 0, watchRevision: 0,
  goalValidation: false, watchValidation: false, operation: null, notice: '', error: '', rejection: null, result: null, uncertain: [] })
const editors = new Map<number, { identity: string; state: PlanEditor }>()
const listeners = new Set<() => void>()
export function readPlanEditor(identity: string, roomId: number) {
  const editor = editors.get(roomId)
  return editor?.identity === identity ? editor.state : initial
}
export function subscribePlanEditors(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function clearPlanEditors() { editors.clear(); listeners.forEach(listener => listener()) }
export function updatePlanEditor(identity: string, roomId: number, generation: number,
  update: (state: PlanEditor) => PlanEditor) {
  if (generation !== directoryViewGeneration()) return false
  const previous = readPlanEditor(identity, roomId), next = update(previous)
  if (previous === next) return false
  editors.set(roomId, { identity, state: Object.freeze(next) })
  listeners.forEach(listener => listener())
  return true
}

// Parked forms keep literal input and their original command/revision binding.
// Restoring a form is UI-only; receipt/CAS/unknown admission remains in RoomPlans.
export const watchDraftKey=(form:WatchForm)=>form.id||form.command_id
export function selectWatchDraft(previous:PlanEditor,incoming:WatchForm):PlanEditor{
 if(previous.operation)return previous
 const target=watchDraftKey(incoming),active=previous.watch
 if(active&&watchDraftKey(active)===target)return previous
 const parked=previous.watchDrafts||[],restored=parked.find(form=>watchDraftKey(form)===target)||incoming
 const remaining=parked.filter(form=>watchDraftKey(form)!==target&&(!active||watchDraftKey(form)!==watchDraftKey(active)))
 if(active)remaining.push(Object.freeze({...active}))
 return {...previous,watch:Object.freeze({...restored}),watchDrafts:Object.freeze(remaining),watchSession:crypto.randomUUID(),watchRevision:previous.watchRevision+1,watchValidation:false,notice:''}
}

// One local undo slot. It is not a server command or a record deletion.
// Other goal fields and parked watch inputs stay intact.
export function removeGoalCriterion(previous:PlanEditor,expected:Goal,criterionId:string):PlanEditor{
 if(previous.operation||previous.draft&&previous.draft!==expected)return previous
 const index=expected.criteria.findIndex(row=>row.id===criterionId)
 if(index<0)return previous
 return {...previous,draft:{...expected,complete:false,criteria:expected.criteria.filter(row=>row.id!==criterionId)},
  removedGoalCriterion:Object.freeze({criterion:Object.freeze({...expected.criteria[index]}),index}),
  goalRevision:previous.goalRevision+1,notice:''}
}
export function restoreGoalCriterion(previous:PlanEditor):PlanEditor{
 const removed=previous.removedGoalCriterion,goal=previous.draft
 if(previous.operation||!removed||!goal||goal.criteria.length>=12||goal.criteria.some(row=>row.id===removed.criterion.id))return previous
 const criteria=[...goal.criteria]
 criteria.splice(Math.min(removed.index,criteria.length),0,{...removed.criterion})
 return {...previous,draft:{...goal,complete:false,criteria},removedGoalCriterion:null,goalRevision:previous.goalRevision+1,notice:''}
}

// Reordering changes only array positions. IDs, literal input, evidence and
// completion markers remain the same; persistence stays on explicit goal save.
export function moveGoalCriterion(previous:PlanEditor,expected:Goal,criterionId:string,direction:-1|1):PlanEditor{
 if(previous.operation||previous.draft&&previous.draft!==expected)return previous
 const from=expected.criteria.findIndex(row=>row.id===criterionId),to=from+direction
 if(from<0||to<0||to>=expected.criteria.length)return previous
 const criteria=[...expected.criteria]
 ;[criteria[from],criteria[to]]=[criteria[to],criteria[from]]
 return {...previous,draft:{...expected,criteria},goalRevision:previous.goalRevision+1,notice:''}
}
