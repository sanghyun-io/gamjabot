// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it } from 'vitest'
import ActionReviewDetails from './ActionReviewDetails'
import { I18nProvider } from '../i18n'

const reviewId = '550e8400-e29b-41d4-a716-446655440000'
const review = {
  version: 1, captured_at: '2026-10-15T17:00:00.123456Z',
  action: { action_id: 'action-a', room_id: 5, agent_id: 7 },
  authority: { owner_id: 1, room_id: 5, agent_id: 7, euid: 502, os_user_status: 'verified',
    proposer: { owner_id: 1, room_id: 5, agent_id: 7, connection_id: 4, provider_id: 'alibaba-coding-plan',
      model_id: 'qwen3.7-plus', config_revision: 2, auth_grant_revision: 1,
      owner_epoch: 'private-runtime-epoch' } },
  targets: [{ role: 'path', status: 'verified', path: '/owned/fixture.txt',
    parents: [{ component: 'private-parent-chain' }], credential_ref: 'NEVER_RENDER_CREDENTIAL_REF' }],
  external_context: { account: 'not_applicable', target_version: 'not_applicable' },
  unexpected_secret: 'NEVER_RENDER_UNKNOWN_FIELD',
}
const show = (value: unknown = review, token = reviewId) => render(<I18nProvider systemLanguages={() => ['en']}>
  <ActionReviewDetails review={value} reviewId={token} actionId="action-a" roomId={5} agentId={7} ownerId={1} />
</I18nProvider>)
beforeEach(() => localStorage.clear())
afterEach(cleanup)

it('shows the actual bound account, proposing connection, review ID and observed target without serializing internals', () => {
  const result = show()
  expect(screen.getByText('Owner #1 · Agent #7 · Room #5')).toBeTruthy()
  expect(screen.getByText('alibaba-coding-plan · #4')).toBeTruthy()
  expect(screen.getByText('qwen3.7-plus')).toBeTruthy()
  expect(screen.getByText(reviewId)).toBeTruthy()
  expect(screen.getByText('/owned/fixture.txt')).toBeTruthy()
  expect(screen.getByText('Not applicable')).toBeTruthy()
  for (const hidden of ['NEVER_RENDER_CREDENTIAL_REF', 'NEVER_RENDER_UNKNOWN_FIELD', 'private-runtime-epoch', 'private-parent-chain']) {
    expect(result.container.textContent).not.toContain(hidden)
  }
})

it.each([
  { ...review, version: 2 },
  { ...review, action: { ...review.action, room_id: 6 } },
  { ...review, authority: { ...review.authority, owner_id: 2 } },
  { ...review, authority: { ...review.authority, proposer: { ...review.authority.proposer, agent_id: 8 } } },
  { ...review, targets: [{ role: 'path', status: 'verified', path: 'private\nmisleading' }] },
  null,
])('does not paint unrelated, malformed or legacy snapshots as the current review', (value) => {
  show(value)
  expect(screen.getByText('Not recorded or unavailable')).toBeTruthy()
  expect(screen.queryByText(reviewId)).toBeNull()
  expect(screen.queryByText('qwen3.7-plus')).toBeNull()
})

it('labels opaque external account/version as unverified and never treats local OS observation as external proof', () => {
  show({ ...review, external_context: { account: 'unsupported', target_version: 'unsupported' } })
  expect(screen.getByText('Not verified')).toBeTruthy()
  expect(screen.queryByText('Not applicable')).toBeNull()
  expect(screen.getByText(/does not verify a program/)).toBeTruthy()
})

it('distinguishes a configured native channel from observed target or external-account verification', () => {
  show({ ...review, targets: [{ role: 'desktop_channel', status: 'configured', instance_id: 'PRIVATE_ADAPTER_ID' }],
    external_context: { account: 'unsupported', target_version: 'unsupported' } })
  expect(screen.getByText(/Adapter configured/)).toBeTruthy()
  expect(screen.getByText('Not verified')).toBeTruthy()
  expect(document.body.textContent).not.toContain('PRIVATE_ADAPTER_ID')
})

it('shows the observation date and local clock through seconds, with its exact instant retained', () => {
  const result = show()
  const time = result.container.querySelector('time')!
  expect(time.dateTime).toBe(review.captured_at)
  const expected = new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(review.captured_at))
  expect(time.textContent).toBe(expected)
})
