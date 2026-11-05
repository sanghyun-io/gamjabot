import { expect, it } from 'vitest'
import { finalizeFixtureAuthority } from './finalizeFixture'

it('keeps the safe fixture action-free and the waiting-action fixture blocked', () => {
  expect(finalizeFixtureAuthority(false)).toEqual({
    hasWaitingAction: false, eligible: true, blockedReason: null, planHash: 'd'.repeat(64),
  })
  expect(finalizeFixtureAuthority(true)).toEqual({
    hasWaitingAction: true, eligible: false, blockedReason: 'action_unsettled', planHash: null,
  })
})
