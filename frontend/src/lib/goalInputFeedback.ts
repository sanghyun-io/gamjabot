import type { Goal } from './roomPlanEditorState'
import { editorTextLength } from './editorValidation'

export type GoalInputField = 'title' | 'criteria' | `criterion:${string}` | `evidence:${string}`
export type GoalInputIssue = { field: GoalInputField; kind: 'title' | 'criteria' | 'text' | 'evidence'; index?: number }
// Checks local input shape only. Missing entries in the bounded received evidence
// list do not mean an older canonical room event is invalid; the server decides.
export function goalInputIssues(value: Goal): GoalInputIssue[] {
  const issues: GoalInputIssue[] = []
  if (!value.title.trim() || editorTextLength(value.title) > 1000) issues.push({ field: 'title', kind: 'title' })
  if (value.criteria.length < 1 || value.criteria.length > 12) issues.push({ field: 'criteria', kind: 'criteria' })
  value.criteria.forEach((criterion, index) => {
    if (!criterion.text.trim() || editorTextLength(criterion.text) > 500) issues.push({ field: `criterion:${criterion.id}`, kind: 'text', index })
    if (criterion.evidence_seq !== null && (!Number.isSafeInteger(criterion.evidence_seq) || criterion.evidence_seq <= 0)
      || criterion.done && !criterion.evidence_seq) issues.push({ field: `evidence:${criterion.id}`, kind: 'evidence', index })
  })
  return issues
}
