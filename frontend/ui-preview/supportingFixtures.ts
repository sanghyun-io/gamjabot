import { connectionsApi, type ProviderCatalog, type ProviderDescriptor } from '../src/api/connections'
import { usageApi, type UsageReport } from '../src/api/usage'
import { mobileAccessApi, remoteApi } from '../src/api/remote'
import { roomMemoriesApi } from '../src/api/roomMemories'
import { roomResultsApi } from '../src/api/roomResults'

// Display fixtures only. No credential, real host, provider request or native effect.
export function installSupportingFixtures(state: string) {
  const providers: ProviderCatalog = { version: 1, providers: ['compatible', 'anthropic'].map<ProviderDescriptor>(id => ({
    provider_id: id, label_key: `connections.providers.${id}`, icon: id,
    protocols: [id === 'anthropic' ? 'anthropic_messages' : 'openai_chat_completions'],
    fields: id === 'compatible' ? [{ field_id: 'base_url', kind: 'url', required: true }] : [],
    model_source: { strategy: 'configured', manual_id: true, static_model_ids: id === 'anthropic' ? ['claude-sonnet'] : ['qwen3.7-plus'] },
    methods: [{ method_id: 'api_key', code_state: 'implemented', delivery_phase: 'D1', external_eligibility: 'unknown', selectable: true }],
    technical_compatibility: 'supported', usage_classification: 'unknown', default_base_url: null,
  })) }
  if (state === 'chatgpt-image-option') providers.providers.push({
    provider_id: 'chatgpt', label_key: 'connections.providers.chatgpt', icon: 'chatgpt', protocols: ['openai_responses'], fields: [],
    model_source: { strategy: 'discovery', manual_id: true, static_model_ids: ['gpt-6.1-terra'] },
    methods: [{ method_id: 'oauth_browser', code_state: 'implemented', delivery_phase: 'D2', external_eligibility: 'verified', selectable: true }],
    technical_compatibility: 'supported', usage_classification: 'unknown', default_base_url: 'https://api.openai.com/v1',
  })
  connectionsApi.catalog = async () => providers
  const report: UsageReport = {
    days: 30, since: '2026-10-01T00:00:00Z', groups: state === 'empty' ? [] : [
      { connection_id: 1, provider: 'compatible', model: 'qwen3.7-plus', auth: 'api_key', billing: 'subscription',
        attempts: 3, input_reported: 3, output_reported: 3, input_tokens: 2480, output_tokens: 720,
        cache_read: null, cache_write: null, reasoning: null, priced: 0, estimate_usd: null, assumed_cache: 0, price_at: null,
      },
      { connection_id: 2, provider: 'local', model: 'local-demo', auth: 'local_none', billing: 'local',
        attempts: 2, input_reported: 2, output_reported: 2, input_tokens: 530, output_tokens: 160,
        cache_read: null, cache_write: null, reasoning: null, priced: 0, estimate_usd: null, assumed_cache: 0, price_at: null,
      },
    ], prices: [], last_attempt: null, last_success: null, last_error: null,
    feed_source: 'synthetic-preview', interval_hours: 24, dropped_records: 0, capture_started: '2026-10-01T00:00:00Z',
  }
  usageApi.read = async days => ({ ...report, days })
  usageApi.prices = async days => ({ ...report, days })
  mobileAccessApi.status = async () => ({ enabled: state === 'paired', listening: state === 'paired',
    origin: state === 'paired' ? 'https://example.invalid' : '', loopback_port: 8432,
    devices: state === 'paired' ? [{ id: 'synthetic-phone', name: '내 휴대폰 (예시)', expires_at: 1893456000, last_seen_at: null }] : [],
  })
  remoteApi.listDevices = async () => []
  roomMemoriesApi.list = async room => ({ room_id: room, revision: 0, common_context_after_seq: 0, items: [], agents: [] })
  roomResultsApi.list = async room => ({ room_id: room, items: [], next_cursor: null })
}
