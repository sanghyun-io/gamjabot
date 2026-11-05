import api from '../lib/api'
export interface UsageGroup {
  connection_id: number | null; provider: string; model: string; auth: string | null; billing: string
  attempts: number; input_reported: number; output_reported: number; input_tokens: number | null; output_tokens: number | null
  cache_read: number | null; cache_write: number | null; reasoning: number | null
  priced: number; estimate_usd: number | null; assumed_cache: number; price_at: string | null
}
export interface UsagePrice { provider: string; model: string; cost: Record<string, unknown> | null; observed_at: string | null }
export interface UsageReport {
  days: number; since: string; groups: UsageGroup[]; prices: UsagePrice[]; last_attempt: string | null
  last_success: string | null; last_error: string | null; feed_source: string; interval_hours: number
  dropped_records: number; capture_started: string | null
}
export const usageApi = {
  read: async (days: number) => (await api.get<UsageReport>('/usage', {params:{days}})).data,
  prices: async (days: number) => (await api.post<UsageReport>('/usage/prices/refresh', null, {params:{days}})).data,
}
