import type { Meta, StoryObj } from '@storybook/react-vite'
import Scenario from './PreviewScenario'

const meta = { title: 'Product/Message thread v2', component: Scenario, args: { screen: 'chat', state: 'thread-nested' } } satisfies Meta<typeof Scenario>
export default meta
type Story = StoryObj<typeof meta>
export const Nested: Story = { args: { screen: 'chat', state: 'thread-nested' } }
export const More: Story = { args: { screen: 'chat', state: 'thread-more' } }
export const InvalidParent: Story = { args: { screen: 'chat', state: 'thread-invalid-parent' } }
export const LateReply: Story = { args: { screen: 'chat', state: 'thread-late' } }
export const DepthLimit: Story = { args: { screen: 'chat', state: 'thread-depth-limit' } }
