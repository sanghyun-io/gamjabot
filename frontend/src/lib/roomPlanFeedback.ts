import type { MessageKey } from '../i18n/locales/en'

export const planRejectionMessages = {
  goal: 'roomPlans.reject.goal', criterion: 'roomPlans.reject.criterion',
  evidence: 'roomPlans.reject.evidence', incomplete: 'roomPlans.reject.incomplete',
  address: 'roomPlans.reject.address', keyword: 'roomPlans.reject.keyword',
  limit: 'roomPlans.reject.limit', missing: 'roomPlans.reject.missing',
  ended: 'roomPlans.reject.ended', format: 'roomPlans.reject.format',
} satisfies Record<string, MessageKey>
export type PlanRejection = keyof typeof planRejectionMessages
const expected: Record<string, readonly [number, PlanRejection]> = {
  room_plan_invalid_goal: [422, 'goal'], room_plan_invalid_criterion: [422, 'criterion'],
  room_plan_evidence_required: [422, 'evidence'], room_plan_criteria_incomplete: [422, 'incomplete'],
  room_plan_invalid_public_url_or_timezone: [422, 'address'], room_plan_keyword_required: [422, 'keyword'],
  room_plan_watch_limit: [422, 'limit'], room_plan_not_found: [404, 'missing'], room_plan_ended: [409, 'ended'],
}
// Store only an allowlisted reason. No response bodies, URLs, credentials or
// validation input echoes enter the editor workspace or rendered notice.
export function planRejection(error: unknown): PlanRejection | null {
  const response = (error as { response?: { status?: unknown; data?: { detail?: unknown } } } | null)?.response
  const detail = response?.data?.detail
  const code = detail && typeof detail === 'object' && !Array.isArray(detail) ? (detail as { code?: unknown }).code : undefined
  if (typeof code === 'string' && Object.prototype.hasOwnProperty.call(expected, code)) {
    const [status, reason] = expected[code]
    return response?.status === status ? reason : null
  }
  return response?.status === 422 && Array.isArray(detail) ? 'format' : null
}
