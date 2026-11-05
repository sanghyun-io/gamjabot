import type { Meta, StoryObj } from '@storybook/react-vite'
import { NotificationClickNotice } from '../src/components/NotificationClickNotice'

const meta: Meta<typeof NotificationClickNotice> = {
  title: 'Notifications/Click recovery', component: NotificationClickNotice,
  args: {locale: 'ko', onOpen: () => {}, onDismiss: () => {}},
}
export default meta
type Story = StoryObj<typeof meta>
export const Unresolved: Story = {args: {reason: 'unresolved'}}
export const ModalOpen: Story = {args: {reason: 'modal'}}
