import { directoryViewGeneration } from './directoryViewMemory'

export type ProjectAssignmentRequest = Readonly<{
  id: string; identity: string; projectId: number; agentId: number; agentName: string;
  action: 'add' | 'remove'; status: 'sending' | 'accepted' | 'unknown' | 'rejected';
  needsRead: boolean; inspected: boolean; acknowledged: boolean;
}>
const projects = new Map<number, readonly ProjectAssignmentRequest[]>()
const empty: readonly ProjectAssignmentRequest[] = []
const listeners = new Set<() => void>()
const emit = () => listeners.forEach(listener => listener())
export function subscribeProjectAssignments(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener) } }
export function projectAssignmentRequests(identity: string, projectId: number) {
  const rows = projects.get(projectId)
  return rows?.[0]?.identity === identity ? rows : empty
}
export function clearProjectAssignments() { projects.clear(); emit() }
export function projectAssignmentsBlocked(rows: readonly ProjectAssignmentRequest[]) {
  return rows.some(row => row.status === 'sending' || row.needsRead || row.status === 'unknown' && !row.acknowledged)
}
export function startProjectAssignment(row: ProjectAssignmentRequest, generation: number) {
  if (generation !== directoryViewGeneration() || projectAssignmentsBlocked(projectAssignmentRequests(row.identity, row.projectId))) return false
  const previous = projectAssignmentRequests(row.identity, row.projectId).filter(item => item.status === 'unknown')
  projects.set(row.projectId, [...previous, Object.freeze(row)]); emit(); return true
}
export function updateProjectAssignment(identity: string, projectId: number, id: string, generation: number,
  patch: Partial<Pick<ProjectAssignmentRequest, 'status' | 'needsRead' | 'inspected' | 'acknowledged'>>) {
  if (generation !== directoryViewGeneration()) return
  const rows = projectAssignmentRequests(identity, projectId)
  if (!rows.some(row => row.id === id)) return
  projects.set(projectId, rows.map(row => row.id === id ? Object.freeze({ ...row, ...patch }) : row)); emit()
}
