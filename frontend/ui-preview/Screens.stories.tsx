import type { Meta, StoryObj } from '@storybook/react-vite'
import Scenario from './PreviewScenario'
import userEvent from '@testing-library/user-event'
import { waitFor } from '@testing-library/react'
import { syntheticBotTemplate } from './fixtures'

const meta = {
  title: 'Screens/실제 제품 화면',
  component: Scenario,
  args: { screen: 'dashboard', state: 'workqueue', incomingMessages: 0 },
  argTypes: {
    incomingMessages: { name: '새 메시지 수신 수 · 미리보기 전용', description: '수를 늘리면 합성 메시지가 도착합니다. 제품 기능이 아닙니다. 새로고침하면 초기화됩니다.', control: { type: 'number', min: 0, max: 10, step: 1 }, if: { arg: 'state', eq: 'timeline' } },
    screen: { control: 'select', options: ['browser-demonstration', 'memory-import', 'bot-library', 'apps', 'dashboard', 'chat', 'agents', 'connections', 'projects', 'usage', 'devices', 'bot-memory', 'skills'] },
    state: { control: 'select', options: ['readonly-failure-parked','readonly-failure-unknown','bot-reaction-ready','bot-reaction-changed','bot-reaction-cleared','bot-reaction-malformed','bot-reaction-owner','thread-nested','thread-depth-limit','thread-invalid-parent','thread-ready','thread-empty','thread-error','thread-more','reaction-ready','reaction-set','reaction-clear','reaction-unknown','reaction-conflict','persistent-reply-ready','persistent-reply-restored','persistent-reply-empty','persistent-reply-unknown','persistent-reply-unavailable','quote-reply-ready','quote-reply-conflict','quote-reply-ambiguous','routine-webhook-empty','routine-webhook-configured','routine-webhook-issued','routine-webhook-unknown','routine-webhook-rejected','notion-skills-ready','notion-skills-unknown','notion-skills-rejected','notion-oauth-preparing','notion-oauth-authorizing','notion-oauth-authorized','notion-oauth-unknown','notion-oauth-conflict','notion-oauth-failed','notion-oauth-unavailable','grant-scope-once','grant-scope-session','tool-review-retry-accepted','tool-review-retry-ready','tool-review-retry-effects','tool-review-retry-context','tool-review-retry-stale','tool-review-retry-unknown','tool-review-changed','tool-review-success','tool-review-failure','demo-recovery-conflict','demo-recovery','demo-ready','demo-permission','demo-interrupted','demo-unknown','chatgpt-image-option','message-reference','memory-import-message-ready','memory-import-message-long','memory-import-message-invalid','memory-import-provenance','memory-import-normal','memory-import-empty','memory-import-primary-unset','memory-import-conflict','memory-import-unknown','routine-receipt-run-failed','routine-receipt-run-completed','routine-receipt-run-mismatch','page-revision-ready','page-revision-unknown','page-revision-missing','page-revision-conflict','page-revision-failed','bot-library-ready','bot-library-screenshot','bot-library-empty','bot-library-error','bot-library-revoked','mcp-delegated-valid','mcp-delegated-missing','mcp-transport-diagnostic-known','mcp-transport-diagnostic-untrusted','mcp-discovery-poll-complete','mcp-discovery-poll-failed','mcp-discovery-poll-unknown','mcp-discovery-known-unknown','mcp-discovery-missing','mcp-many','mcp-routine-valid','mcp-routine-origin-changed','mcp-routine-proof-changed','mcp-approval','mcp-secret','mcp-secret-unknown','mcp-empty','mcp-normal','mcp-discovery-failure','mcp-unknown','mcp-conflict','mcp-disconnected','mcp-revoked','package-provenance','package-empty','package-preview','package-unknown','package-conflict','package-malformed','routine-receipt-saved','routine-receipt-queued','routine-receipt-unknown','routine-receipt-mismatch','routine-new','routine-empty','routine-normal','routine-paused','routine-running','routine-failed','routine-approval','routine-unknown','routine-conflict','skill-receipt-binding-changed','skill-receipt-binding-mismatch','skill-receipt-ready','skill-receipt-saved','skill-receipt-mismatch','skill-receipt-unknown','skill-receipt-history','skill-source-task','skill-ready','skill-empty','skill-error','skill-unknown','skill-conflict','skill-inactive','skill-source','skill-source-rejected','previous-cancelled','memory-sharing-chat','memory-sharing-chat-unknown','memory-sharing-chat-mismatch','memory-sharing-ready','memory-sharing-enabled','memory-sharing-empty','memory-sharing-error','memory-sharing-unknown','memory-sharing-conflict','checkin-off','checkin-on','checkin-saving','checkin-unknown','checkin-conflict','checkin-no-primary','checkin-failed','checkin-cancelled','checkin-changed-primary','chat-persistence-saved', 'chat-persistence-updated', 'chat-persistence-forgotten', 'chat-persistence-role', 'chat-persistence-role-busy', 'chat-persistence-empty', 'chat-persistence-failure', 'chat-persistence-unknown', 'chat-persistence-reference', 'workqueue', 'one-room', 'empty', 'setup', 'ready', 'duplicate-failure', 'error', 'recovery', 'unknown', 'timeline', 'comparison', 'teammates-pending', 'teammates-working', 'teammates-completed', 'teammates-failed', 'teammates-cancelled', 'teammates-unknown', 'teammates-alone', 'bot-message-pending', 'bot-message-working', 'bot-message-answered', 'bot-message-returning', 'bot-message-completed', 'bot-message-incoming', 'bot-message-failed', 'bot-message-cancelled', 'bot-message-interrupted', 'bot-message-busy-timeout', 'bot-message-return-failed', 'bot-message-return-cancelled', 'bot-message-return-interrupted', 'bot-message-return-busy-timeout', 'bot-message-retry-ready', 'bot-message-retry-blocked', 'bot-message-retry-read-error', 'bot-message-retry-changed', 'bot-message-retry-sending', 'bot-message-retry-unknown', 'bot-message-retry-owner-switch', 'root-approval-image-desktop','root-approval-image-browser','root-approval-process', 'root-approval-missing-proof', 'root-approval-ready', 'root-approval-external', 'root-approval-stale', 'root-approval-stop-unknown', 'root-approval-two-roots', 'bot-create-checking', 'bot-create-created', 'bot-create-collaborating', 'bot-create-failed', 'bot-create-replayed', 'bot-create-source-changed', 'bot-create-unknown', 'bot-create-unavailable', 'parallel-incoming', 'parallel-incoming-waiting', 'parallel-incoming-unavailable', 'parallel-pending', 'parallel-working', 'parallel-first-answer', 'parallel-all-answers', 'parallel-completed', 'parallel-partial', 'parallel-branch-stop', 'parallel-root-stop', 'bot-message-passed', 'bot-message-unknown', 'primary-unset', 'primary-ready', 'primary-error', 'primary-open-error', 'primary-conflict', 'primary-closed', 'primary-empty', 'primary-saving', 'primary-save-unknown', 'primary-owner-switch', 'bot-personal', 'bot-membership-error', 'bot-memory-ready', 'bot-memory-empty', 'bot-memory-read-error', 'bot-memory-saving', 'bot-memory-unknown', 'bot-memory-changed', 'bot-memory-busy'] },
  },
  render: (args, context) => <Scenario key={`${args.screen}:${args.state}:${context.globals.theme}:${context.globals.locale}`} {...args} />,
} satisfies Meta<typeof Scenario>
export default meta
type Story = StoryObj<typeof meta>

export const DemoRecoveryConflict: Story = { name: '브라우저 시연 · 검토 저장 충돌', args: { screen: 'browser-demonstration', state: 'demo-recovery-conflict' } }
export const DemoRecovery: Story = { name: '브라우저 시연 · 저장된 검토 복구', args: { screen: 'browser-demonstration', state: 'demo-recovery' } }
export const DemoReady: Story = { name: '브라우저 시연 · 시작과 검토', args: { screen: 'browser-demonstration', state: 'demo-ready' } }
export const DemoPermission: Story = { name: '브라우저 시연 · 직접 제어 필요', args: { screen: 'browser-demonstration', state: 'demo-permission' } }
export const DemoInterrupted: Story = { name: '브라우저 시연 · 기록 중단', args: { screen: 'browser-demonstration', state: 'demo-interrupted' } }
export const DemoUnknown: Story = { name: '브라우저 시연 · 저장 결과 불명', args: { screen: 'browser-demonstration', state: 'demo-unknown' } }
export const HomeAttention: Story = { name: '홈 · 확인할 대화', args: { screen: 'dashboard', state: 'workqueue' } }
export const HomeOneRoom: Story = { name: '홈 · 대화 하나', args: { screen: 'dashboard', state: 'one-room' } }
export const HomeEmpty: Story = { name: '홈 · 빈 계정', args: { screen: 'dashboard', state: 'empty' } }
export const HomeSetup: Story = { name: '홈 · 연결 전', args: { screen: 'dashboard', state: 'setup' } }
export const HomeError: Story = { name: '홈 · 읽기 실패', args: { screen: 'dashboard', state: 'error' } }
export const PrimaryBotUnset: Story = { name: '대표 Bot · 선택 전', args: { screen: 'dashboard', state: 'primary-unset' } }
export const PrimaryBotReady: Story = { name: '대표 Bot · 바로 대화하기', args: { screen: 'dashboard', state: 'primary-ready' } }
export const PrimaryBotReadError: Story = { name: '대표 Bot · 선택 읽기 실패', args: { screen: 'dashboard', state: 'primary-error' } }
export const PrimaryBotOpenError: Story = { name: '대표 Bot · 대화 열기 실패', args: { screen: 'dashboard', state: 'primary-open-error' } }
export const PrimaryBotChanged: Story = { name: '대표 Bot · 다른 곳에서 선택 변경', args: { screen: 'dashboard', state: 'primary-conflict' } }
export const PrimaryBotClosed: Story = { name: '대표 Bot · 닫힌 개인 대화', args: { screen: 'dashboard', state: 'primary-closed' } }
export const PrimaryBotEmpty: Story = { name: '대표 Bot · 등록된 Bot 없음', args: { screen: 'dashboard', state: 'primary-empty' } }
export const PrimaryBotSaving: Story = { name: '대표 Bot · 선택 저장 중', args: { screen: 'dashboard', state: 'primary-saving' } }
export const PrimaryBotSaveUnknown: Story = { name: '대표 Bot · 저장 결과 불명', args: { screen: 'dashboard', state: 'primary-save-unknown' } }
export const PrimaryBotOwnerSwitch: Story = { name: '대표 Bot · 대화 열기 중 사용자 변경', args: { screen: 'dashboard', state: 'primary-owner-switch' } }
export const BotPersonalConversation: Story = { name: 'Bot · 개인 대화', args: { screen: 'chat', state: 'bot-personal' } }
export const BotMembershipUnknown: Story = { name: 'Bot · 대화 구성 읽기 실패', args: { screen: 'chat', state: 'bot-membership-error' } }
export const MessageReferenceNavigation: Story = { name: '대화 · 메시지 참조 열기', args: { screen: 'dashboard', state: 'message-reference' } }
export const MemoryImportMessageReady: Story = { name: '대화 · 가져온 정보', args: { screen: 'chat', state: 'memory-import-message-ready' } }
export const MemoryImportMessageLong: Story = { name: '대화 · 긴 가져온 정보', args: { screen: 'chat', state: 'memory-import-message-long' } }
export const MemoryImportMessageInvalid: Story = { name: '대화 · 알 수 없는 가져오기 형식', args: { screen: 'chat', state: 'memory-import-message-invalid' } }
export const MemoryImportProvenance: Story = { name: 'Bot 기억 · 가져온 출처와 오래된 순서', args: { screen: 'bot-memory', state: 'memory-import-provenance' } }
export const MemoryImportNormal: Story = { name: '기억 가져오기 · 대표 Bot', args: { screen: 'memory-import', state: 'memory-import-normal' } }
export const MemoryImportEmpty: Story = { name: '기억 가져오기 · 빈 입력', args: { screen: 'memory-import', state: 'memory-import-empty' } }
export const MemoryImportPrimaryUnset: Story = { name: '기억 가져오기 · 대표 Bot 없음', args: { screen: 'memory-import', state: 'memory-import-primary-unset' } }
export const MemoryImportConflict: Story = { name: '기억 가져오기 · 대표 변경', args: { screen: 'memory-import', state: 'memory-import-conflict' } }
export const MemoryImportUnknown: Story = { name: '기억 가져오기 · 전달 결과 불명', args: { screen: 'memory-import', state: 'memory-import-unknown' } }

export const BotMemoryReady: Story = { name: 'Bot 기억 · 저장된 개인 기억', args: { screen: 'bot-memory', state: 'bot-memory-ready' } }
export const BotMemoryEmpty: Story = { name: 'Bot 기억 · 빈 기억', args: { screen: 'bot-memory', state: 'bot-memory-empty' } }
export const BotMemoryReadError: Story = { name: 'Bot 기억 · 읽기 실패', args: { screen: 'bot-memory', state: 'bot-memory-read-error' } }
export const BotMemorySaving: Story = { name: 'Bot 기억 · 저장 중', args: { screen: 'bot-memory', state: 'bot-memory-saving' } }
export const BotMemoryUnknown: Story = { name: 'Bot 기억 · 저장 결과 불명', args: { screen: 'bot-memory', state: 'bot-memory-unknown' } }
export const BotMemoryChanged: Story = { name: 'Bot 기억 · 다른 수정과 충돌', args: { screen: 'bot-memory', state: 'bot-memory-changed' } }
export const BotMemoryBusy: Story = { name: 'Bot 기억 · 개인 대화 작업 중', args: { screen: 'bot-memory', state: 'bot-memory-busy' } }
export const ChatTeammateIncomplete: Story = { name: '대화 · 동료 답변 전체 확인 실패', args: { screen: 'chat', state: 'teammate-incomplete-failure' } }
export const ChatTeammateChanged: Story = { name: '대화 · 동료 답변 변경', args: { screen: 'chat', state: 'teammate-changed-failure' } }
export const ChatReportedFailure: Story = { name: '대화 · 제보된 반복 오류', args: { screen: 'chat', state: 'duplicate-failure' } }
export const ToolReviewRetryAccepted: Story = { name: '도구 검토 변경 · 접수 기록과 대화', args: { screen: 'chat', state: 'tool-review-retry-accepted' } }
export const ToolReviewRetryReady: Story = { name: '도구 검토 변경 · 새 요청 검토', args: { screen: 'chat', state: 'tool-review-retry-ready' } }
export const ToolReviewRetryEffects: Story = { name: '도구 검토 변경 · 이전 실행으로 차단', args: { screen: 'chat', state: 'tool-review-retry-effects' } }
export const ToolReviewRetryContext: Story = { name: '도구 검토 변경 · 문맥 복구 불가', args: { screen: 'chat', state: 'tool-review-retry-context' } }
export const ToolReviewRetryStale: Story = { name: '도구 검토 변경 · 계획 변경', args: { screen: 'chat', state: 'tool-review-retry-stale' } }
export const ToolReviewRetryUnknown: Story = { name: '도구 검토 변경 · 전송 결과 불명', args: { screen: 'chat', state: 'tool-review-retry-unknown' } }
export const ToolReviewChanged: Story = { name: '대화 · 도구 검토 조건 변경', args: { screen: 'chat', state: 'tool-review-changed' } }
export const ToolReviewSuccess: Story = { name: '대화 · 도구 정상 결과', args: { screen: 'chat', state: 'tool-review-success' } }
export const ToolReviewFailure: Story = { name: '대화 · 다른 도구 실패', args: { screen: 'chat', state: 'tool-review-failure' } }
export const BotMessageReactionReady: Story = { name: 'Bot 반응 · 동료 확인', args: { screen: 'chat', state: 'bot-reaction-ready' } }
export const BotMessageReactionChanged: Story = { name: 'Bot 반응 · 본인 반응 변경', args: { screen: 'chat', state: 'bot-reaction-changed' } }
export const BotMessageReactionCleared: Story = { name: 'Bot 반응 · 본인 반응 취소', args: { screen: 'chat', state: 'bot-reaction-cleared' } }
export const BotMessageReactionMalformed: Story = { name: 'Bot 반응 · 잘못된 주체와 원문 제외', args: { screen: 'chat', state: 'bot-reaction-malformed' } }
export const BotMessageReactionOwner: Story = { name: 'Bot 반응 · 내 반응과 별도 표시', args: { screen: 'chat', state: 'bot-reaction-owner' } }
export const ReactionReady: Story = { name: '대화 반응 · 선택', args: { screen: 'chat', state: 'reaction-ready' } }
export const ReactionSet: Story = { name: '대화 반응 · 기존 반응 변경', args: { screen: 'chat', state: 'reaction-set' } }
export const ReactionClear: Story = { name: '대화 반응 · 같은 반응 취소', args: { screen: 'chat', state: 'reaction-clear' } }
export const ReactionUnknown: Story = { name: '대화 반응 · 결과 불명 확인', args: { screen: 'chat', state: 'reaction-unknown' } }
export const ReactionConflict: Story = { name: '대화 반응 · 다른 창과 충돌', args: { screen: 'chat', state: 'reaction-conflict' } }
export const PersistentReplyReady: Story = { name: '대화 · 원문에 답장 선택', args: { screen: 'chat', state: 'persistent-reply-ready' } }
export const PersistentReplyRestore: Story = { name: '대화 · 저장된 답장 복원', args: { screen: 'chat', state: 'persistent-reply-restored' } }
export const PersistentReplyEmpty: Story = { name: '대화 · 내용 없는 답장 초안', args: { screen: 'chat', state: 'persistent-reply-empty' } }
export const PersistentReplyUnknown: Story = { name: '대화 · 답장 전송 결과 확인', args: { screen: 'chat', state: 'persistent-reply-unknown' } }
export const PersistentReplyUnavailable: Story = { name: '대화 · 찾을 수 없는 답장 원문', args: { screen: 'chat', state: 'persistent-reply-unavailable' } }
export const QuoteReplyReady: Story = { name: '대화 · Bot에게 인용 답장과 기존 초안', args: { screen: 'chat', state: 'quote-reply-ready' } }
export const QuoteReplyConflict: Story = { name: '대화 · 다른 Bot 멘션과 인용 답장', args: { screen: 'chat', state: 'quote-reply-conflict' } }
export const QuoteReplyAmbiguous: Story = { name: '대화 · 이름이 같은 Bot 인용 답장', args: { screen: 'chat', state: 'quote-reply-ambiguous' } }
export const ChatReady: Story = { name: '대화 · 승인 대기와 작업 중', args: { screen: 'chat', state: 'ready' } }
export const ChatRecovery: Story = { name: '대화 · 결과 불명', args: { screen: 'chat', state: 'recovery' } }
export const ChatDisconnected: Story = { name: '대화 · 연결 불명', args: { screen: 'chat', state: 'unknown' } }
export const ChatEmpty: Story = { name: '대화 · 빈 방', args: { screen: 'chat', state: 'empty' } }
export const ChatLong: Story = { name: '대화 · 긴 기록과 새 메시지', args: { screen: 'chat', state: 'timeline' } }
export const TeammatesPending: Story = { name: '동료 · 요청 대기', args: { screen: 'chat', state: 'teammates-pending' } }
export const TeammatesWorking: Story = { name: '동료 · 부탁한 작업 진행 중', args: { screen: 'chat', state: 'teammates-working' } }
export const TeammatesCompleted: Story = { name: '동료 · 답변을 받아 이어가기', args: { screen: 'chat', state: 'teammates-completed' } }
export const TeammatesFailed: Story = { name: '동료 · 요청 실패', args: { screen: 'chat', state: 'teammates-failed' } }
export const TeammatesCancelled: Story = { name: '동료 · 요청 중지', args: { screen: 'chat', state: 'teammates-cancelled' } }
export const TeammatesUnknown: Story = { name: '동료 · 현재 상태 불명', args: { screen: 'chat', state: 'teammates-unknown' } }
export const TeammatesAlone: Story = { name: '동료 · 혼자 있는 대화', args: { screen: 'chat', state: 'teammates-alone' } }
export const Agents: Story = { name: '관리 · Agent', args: { screen: 'agents', state: 'ready' } }
export const BotDirectoryReady: Story = { name:'Bot 정리 · 고정과 섹션', args:{screen:'agents',state:'bot-directory-ready'} }
export const BotDirectoryHidden: Story = { name:'Bot 정리 · 숨김 복원', args:{screen:'agents',state:'bot-directory-hidden'} }
export const BotDirectoryAllHidden: Story = { name:'Bot 정리 · 모두 숨김', args:{screen:'agents',state:'bot-directory-all-hidden'} }
export const BotDirectoryReadError: Story = { name:'Bot 정리 · 불러오기 실패', args:{screen:'agents',state:'bot-directory-read-error'} }
export const BotDirectoryUnknown: Story = { name:'Bot 정리 · 저장 결과 불명', args:{screen:'agents',state:'bot-directory-unknown'} }
export const BotDirectoryMissing: Story = { name:'Bot 정리 · 접수 기록 없음', args:{screen:'agents',state:'bot-directory-missing'} }
export const BotDirectoryConflict: Story = { name:'Bot 정리 · 변경 충돌', args:{screen:'agents',state:'bot-directory-conflict'} }
export const BotDirectoryHome: Story = { name:'Bot 정리 · 홈과 숨긴 Bot의 승인', args:{screen:'dashboard',state:'bot-directory-home'} }
export const BotDuplicateReady: Story = { name:'Bot 복제 · 검토와 완료', args:{screen:'agents',state:'bot-duplicate-ready'} }
export const BotDuplicatePreviewError: Story = { name:'Bot 복제 · 검토 읽기 실패', args:{screen:'agents',state:'bot-duplicate-preview-error'} }
export const BotDuplicateStale: Story = { name:'Bot 복제 · 원본 변경', args:{screen:'agents',state:'bot-duplicate-stale'} }
export const BotDuplicateRejected: Story = { name:'Bot 복제 · 저장 전 거절', args:{screen:'agents',state:'bot-duplicate-rejected'} }
export const BotDuplicateUnknown: Story = { name:'Bot 복제 · 결과 불명', args:{screen:'agents',state:'bot-duplicate-unknown'} }
export const BotDuplicateMissing: Story = { name:'Bot 복제 · 접수 기록 없음', args:{screen:'agents',state:'bot-duplicate-missing'} }

const selectTemplateFile=async({canvasElement}:{canvasElement:HTMLElement})=>{
 const open=await waitFor(()=>{const button=canvasElement.querySelector<HTMLButtonElement>('[data-bot-template-import]');if(!button)throw Error('Synthetic import entry unavailable');return button})
 await userEvent.click(open)
 const input=await waitFor(()=>{const element=canvasElement.querySelector<HTMLInputElement>('input[type=file]');if(!element)throw Error('Synthetic file input unavailable');return element})
 await userEvent.upload(input,new File([JSON.stringify(syntheticBotTemplate)],'synthetic-template.json',{type:'application/json'}))
}
const selectInvalidTemplate=async({canvasElement}:{canvasElement:HTMLElement})=>{
 const open=await waitFor(()=>{const button=canvasElement.querySelector<HTMLButtonElement>('[data-bot-template-import]');if(!button)throw Error('Synthetic import entry unavailable');return button})
 await userEvent.click(open)
 const input=await waitFor(()=>{const element=canvasElement.querySelector<HTMLInputElement>('input[type=file]');if(!element)throw Error('Synthetic file input unavailable');return element})
 await userEvent.upload(input,new File(['{invalid'],'invalid-template.json',{type:'application/json'}))
}
export const BotTemplateExport: Story = { name:'Bot 템플릿 · 파일 검토와 저장',args:{screen:'agents',state:'bot-template-export'} }
export const BotTemplateExportError: Story = { name:'Bot 템플릿 · 원본 읽기 실패',args:{screen:'agents',state:'bot-template-export-error'} }
export const BotTemplateImport: Story = { name:'Bot 템플릿 · 파일 가져오기',args:{screen:'agents',state:'bot-template-import'},play:selectTemplateFile }
export const BotTemplateImportManual: Story = { name:'Bot 템플릿 · 파일 직접 선택',args:{screen:'agents',state:'bot-template-import'} }
export const BotTemplateInvalid: Story = { name:'Bot 템플릿 · 잘못된 파일',args:{screen:'agents',state:'bot-template-invalid'},play:selectInvalidTemplate }
export const BotTemplateReadError: Story = { name:'Bot 템플릿 · 검토 실패',args:{screen:'agents',state:'bot-template-read-error'},play:selectTemplateFile }
export const BotTemplateStale: Story = { name:'Bot 템플릿 · 설정 변경',args:{screen:'agents',state:'bot-template-stale'},play:selectTemplateFile }
export const BotTemplateUnknown: Story = { name:'Bot 템플릿 · 결과 불명',args:{screen:'agents',state:'bot-template-unknown'},play:selectTemplateFile }
export const BotTemplateMissing: Story = { name:'Bot 템플릿 · 접수 기록 없음',args:{screen:'agents',state:'bot-template-missing'},play:selectTemplateFile }
export const BotTemplateMissingManual: Story = { name:'Bot 템플릿 · 미접수 파일 직접 확인',args:{screen:'agents',state:'bot-template-missing'} }
export const BotTemplateRejected: Story = { name:'Bot 템플릿 · 저장 전 거절',args:{screen:'agents',state:'bot-template-rejected'},play:selectTemplateFile }

export const BotTemplateLinkReady: Story = { name:'Bot 템플릿 링크 · 검토 열기 후',args:{screen:'agents',state:'bot-template-link-ready'} }
export const BotTemplateLinkUnavailable: Story = { name:'Bot 템플릿 링크 · 비공개 또는 삭제',args:{screen:'agents',state:'bot-template-link-unavailable'} }
export const BotTemplateLinkInvalid: Story = { name:'Bot 템플릿 링크 · 잘못된 주소',args:{screen:'agents',state:'bot-template-link-invalid'} }
export const BotTemplateLinkUnknown: Story = { name:'Bot 템플릿 링크 · 결과 불명',args:{screen:'agents',state:'bot-template-link-unknown'} }
export const BotTemplateLinkChanged: Story = { name:'Bot 템플릿 링크 · 원본 변경 복구',args:{screen:'agents',state:'bot-template-link-changed'} }




export const ChatGPTImageOption: Story = { name: '모델 연결 · ChatGPT 이미지 선언', args: { screen: 'connections', state: 'chatgpt-image-option' } }
export const Connections: Story = { name: '관리 · 모델 연결', args: { screen: 'connections', state: 'ready' } }
export const Projects: Story = { name: '관리 · 프로젝트', args: { screen: 'projects', state: 'projects-uneven' } }

export const UsageSummary: Story = { name: '사용량 · 구독형과 로컬', args: { screen: 'usage', state: 'ready' } }
export const UsageEmpty: Story = { name: '사용량 · 빈 기록', args: { screen: 'usage', state: 'empty' } }
export const MobilePaired: Story = { name: '모바일 · 연결된 기기', args: { screen: 'devices', state: 'paired' } }
export const MobileOff: Story = { name: '모바일 · 연결 전', args: { screen: 'devices', state: 'ready' } }

export const BotMessagePending: Story = { name: '방밖 협업 · 다른 방에 요청 대기', args: { screen: 'chat', state: 'bot-message-pending' } }
export const BotMessageWorking: Story = { name: '방밖 협업 · 동료가 작업 중', args: { screen: 'chat', state: 'bot-message-working' } }
export const BotMessageAnswered: Story = { name: '방밖 협업 · 동료 답변 도착 · 이어쓰기 대기', args: { screen: 'chat', state: 'bot-message-answered' } }
export const BotMessageReturning: Story = { name: '방밖 협업 · 원래 Bot 이어쓰기 중', args: { screen: 'chat', state: 'bot-message-returning' } }
export const BotMessageCompleted: Story = { name: '방밖 협업 · 답변 반영 완료', args: { screen: 'chat', state: 'bot-message-completed' } }
export const BotMessageIncoming: Story = { name: '방밖 협업 · 받는 방 · 요청과 답변', args: { screen: 'chat', state: 'bot-message-incoming' } }
export const BotMessageFailed: Story = { name: '방밖 협업 · 동료 작업 실패', args: { screen: 'chat', state: 'bot-message-failed' } }
export const BotMessageCancelled: Story = { name: '방밖 협업 · 요청 중지', args: { screen: 'chat', state: 'bot-message-cancelled' } }
export const BotMessageInterrupted: Story = { name: '방밖 협업 · 중단 · 답변 확인 불가', args: { screen: 'chat', state: 'bot-message-interrupted' } }
export const BotMessageBusyTimeout: Story = { name: '방밖 협업 · 동료 바쁨 · 만료', args: { screen: 'chat', state: 'bot-message-busy-timeout' } }
export const BotMessageReturnFailed: Story = { name: '방밖 협업 · 답변 반영 실패', args: { screen: 'chat', state: 'bot-message-return-failed' } }
export const BotMessageReturnCancelled: Story = { name: '방밖 협업 · 답변 반영 중지', args: { screen: 'chat', state: 'bot-message-return-cancelled' } }
export const BotMessagePassed: Story = { name: '방밖 협업 · 동료가 답변하지 않음', args: { screen: 'chat', state: 'bot-message-passed' } }
export const BotMessageUnknown: Story = { name: '방밖 협업 · 현재 상태 읽기 실패', args: { screen: 'chat', state: 'bot-message-unknown' } }

export const BotMessageReturnInterrupted: Story = { name: '방밖 협업 · 이어쓰기 중단 · 후속 답변 불명', args: { screen: 'chat', state: 'bot-message-return-interrupted' } }
export const BotMessageReturnBusyTimeout: Story = { name: '방밖 협업 · 원래 Bot 바쁨 · 이어쓰기 만료', args: { screen: 'chat', state: 'bot-message-return-busy-timeout' } }

export const BotMessageRetryReady: Story = { name: '방밖 협업 재시도 · 새 요청 검토', args: { screen: 'chat', state: 'bot-message-retry-ready' } }
export const BotMessageRetryBlocked: Story = { name: '방밖 협업 재시도 · 완료 효과로 재시도 차단', args: { screen: 'chat', state: 'bot-message-retry-blocked' } }
export const BotMessageRetryReadError: Story = { name: '방밖 협업 재시도 · 가능 여부 읽기 실패', args: { screen: 'chat', state: 'bot-message-retry-read-error' } }
export const BotMessageRetryChanged: Story = { name: '방밖 협업 재시도 · 조건 변경 · 다시 확인', args: { screen: 'chat', state: 'bot-message-retry-changed' } }
export const BotMessageRetrySending: Story = { name: '방밖 협업 재시도 · 새 요청 전송 중', args: { screen: 'chat', state: 'bot-message-retry-sending' } }
export const BotMessageRetryUnknown: Story = { name: '방밖 협업 재시도 · 접수 응답 유실 · 닫기 후 복구', args: { screen: 'chat', state: 'bot-message-retry-unknown' } }
export const BotMessageRetryOwnerSwitch: Story = { name: '방밖 협업 재시도 · 전송 중 사용자 변경', args: { screen: 'chat', state: 'bot-message-retry-owner-switch' } }

export const CaptureImageDesktop: Story = { name: '캡처 승인 · Mac 주 화면과 이미지 전달', args: { screen: 'chat', state: 'root-approval-image-desktop' } }
export const CaptureImageBrowser: Story = { name: '캡처 승인 · Bot 브라우저와 이미지 전달', args: { screen: 'chat', state: 'root-approval-image-browser' } }
export const GrantScopeOnce: Story = { name: '도구 승인 · 한 번만 허용', args: { screen: 'chat', state: 'grant-scope-once' } }
export const GrantScopeSession: Story = { name: '도구 승인 · 한 번과 계속 허용', args: { screen: 'chat', state: 'grant-scope-session' } }
export const RootApprovalReady: Story = { name: '위임 작업 승인 · 원방의 승인 집계 · 실제 실행 Bot', args: { screen: 'chat', state: 'root-approval-ready' } }
export const RootApprovalExternal: Story = { name: '위임 작업 승인 · 외부 화면 직접 확인 · 1회 승인', args: { screen: 'chat', state: 'root-approval-external' } }
export const RootApprovalStale: Story = { name: '위임 작업 승인 · 검토 후 실행 담당 범위 변경', args: { screen: 'chat', state: 'root-approval-stale' } }
export const RootApprovalStopUnknown: Story = { name: '위임 작업 승인 · 중지 응답 · 실행 중지 확인 불명', args: { screen: 'chat', state: 'root-approval-stop-unknown' } }
export const RootApprovalTwoRoots: Story = { name: '위임 작업 승인 · 두 원래 요청 · 중첩 Bot · A만 중지', args: { screen: 'chat', state: 'root-approval-two-roots' } }

export const ParallelPending: Story = { name: '함께 진행 · 동료 작업 대기', args: { screen: 'chat', state: 'parallel-pending' } }
export const ParallelWorking: Story = { name: '함께 진행 · 여러 동료와 진행 중', args: { screen: 'chat', state: 'parallel-working' } }
export const ParallelFirstAnswer: Story = { name: '함께 진행 · 첫 답변 도착 · 남은 답변 대기', args: { screen: 'chat', state: 'parallel-first-answer' } }
export const ParallelAllAnswers: Story = { name: '함께 진행 · 동료 답변 도착 · 원래 Bot 정리 대기', args: { screen: 'chat', state: 'parallel-all-answers' } }
export const ParallelCompleted: Story = { name: '함께 진행 · 실제 종합 답변 도착', args: { screen: 'chat', state: 'parallel-completed' } }
export const ParallelPartial: Story = { name: '함께 진행 · 일부 답변 실패', args: { screen: 'chat', state: 'parallel-partial' } }
export const ParallelBranchStop: Story = { name: '함께 진행 · 한 동료 중지 · 나머지 답변 보존', args: { screen: 'chat', state: 'parallel-branch-stop' } }
export const ParallelRootStop: Story = { name: '함께 진행 · 원래 요청 중지 · 앞선 답변 보존', args: { screen: 'chat', state: 'parallel-root-stop' } }

export const BotCreateChecking: Story = { name: 'Bot 생성 · 결과 확인 중', args: { screen: 'chat', state: 'bot-create-checking' } }
export const BotCreateCreated: Story = { name: 'Bot 생성 · 생성 완료', args: { screen: 'chat', state: 'bot-create-created' } }
export const BotCreateCollaborating: Story = { name: 'Bot 생성 · 바로 동료 협업', args: { screen: 'chat', state: 'bot-create-collaborating' } }
export const BotCreateFailed: Story = { name: 'Bot 생성 · 실패', args: { screen: 'chat', state: 'bot-create-failed' } }
export const BotCreateReplayed: Story = { name: 'Bot 생성 · 같은 생성 결과 재사용', args: { screen: 'chat', state: 'bot-create-replayed' } }
export const BotCreateSourceChanged: Story = { name: 'Bot 생성 · 기준 모델 변경', args: { screen: 'chat', state: 'bot-create-source-changed' } }
export const BotCreateUnknown: Story = { name: 'Bot 생성 · 결과 읽기 실패와 복구', args: { screen: 'chat', state: 'bot-create-unknown' } }
export const BotCreateUnavailable: Story = { name: 'Bot 생성 · 생성 뒤 설정 변경', args: { screen: 'chat', state: 'bot-create-unavailable' } }

export const ParallelIncoming: Story = { name: '함께 진행 · 수신방의 실제 동료 답변', args: { screen: 'chat', state: 'parallel-incoming' } }
export const ParallelIncomingWaiting: Story = { name: '함께 진행 · 내 답변 도착, 다른 동료 진행 중', args: { screen: 'chat', state: 'parallel-incoming-waiting' } }
export const ParallelIncomingUnavailable: Story = { name: '함께 진행 · 수신방 상태 읽기 실패', args: { screen: 'chat', state: 'parallel-incoming-unavailable' } }

export const RootApprovalProcess: Story = { name: '원래 요청 승인 · 실행방의 완전한 증명', args: { screen: 'chat', state: 'root-approval-process' } }
export const RootApprovalMissingProof: Story = { name: '원래 요청 승인 · 실행방 증명 누락은 차단', args: { screen: 'chat', state: 'root-approval-missing-proof' } }

export const ChatPersistenceSaved: Story = { name: '대화 기억 · 저장됨', args: { screen: 'chat', state: 'chat-persistence-saved' } }
export const ChatPersistenceUpdated: Story = { name: '대화 기억 · 변경됨', args: { screen: 'chat', state: 'chat-persistence-updated' } }
export const ChatPersistenceForgotten: Story = { name: '대화 기억 · 앞으로 참고하지 않음', args: { screen: 'chat', state: 'chat-persistence-forgotten' } }
export const ChatPersistenceRole: Story = { name: '대화 역할 · 다음 요청부터 적용', args: { screen: 'chat', state: 'chat-persistence-role' } }
export const ChatPersistenceEmpty: Story = { name: '대화 기억 · 빈 대화', args: { screen: 'chat', state: 'chat-persistence-empty' } }
export const ChatPersistenceFailure: Story = { name: '대화 기억 · 저장 충돌', args: { screen: 'chat', state: 'chat-persistence-failure' } }
export const ChatPersistenceUnknown: Story = { name: '대화 기억 · 저장 결과 확인 불명', args: { screen: 'chat', state: 'chat-persistence-unknown' } }
export const ChatPersistenceReference: Story = { name: '대화 기억 · 동료 인용', args: { screen: 'chat', state: 'chat-persistence-reference' } }
export const ChatPersistenceRoleBusy: Story = { name: '대화 역할 · 다른 작업 중 미적용', args: { screen: 'chat', state: 'chat-persistence-role-busy' } }

export const CheckinOff: Story = { name: '먼저 확인하기 · 꺼짐', args: { screen: 'dashboard', state: 'checkin-off' } }
export const CheckinOn: Story = { name: '먼저 확인하기 · 켜짐', args: { screen: 'dashboard', state: 'checkin-on' } }
export const CheckinSaving: Story = { name: '먼저 확인하기 · 저장 중', args: { screen: 'dashboard', state: 'checkin-saving' } }
export const CheckinUnknown: Story = { name: '먼저 확인하기 · 저장 결과 불명', args: { screen: 'dashboard', state: 'checkin-unknown' } }
export const CheckinConflict: Story = { name: '먼저 확인하기 · 설정 충돌', args: { screen: 'dashboard', state: 'checkin-conflict' } }
export const CheckinNoPrimary: Story = { name: '먼저 확인하기 · 대표 Bot 없음', args: { screen: 'dashboard', state: 'checkin-no-primary' } }
export const CheckinFailed: Story = { name: '먼저 확인하기 · 실패', args: { screen: 'dashboard', state: 'checkin-failed' } }
export const CheckinCancelled: Story = { name: '먼저 확인하기 · 취소', args: { screen: 'dashboard', state: 'checkin-cancelled' } }
export const CheckinChangedPrimary: Story = { name: '먼저 확인하기 · 대표 변경', args: { screen: 'dashboard', state: 'checkin-changed-primary' } }
export const CheckinChat: Story = { name: '먼저 확인하기 · 대화 출처', args: { screen: 'chat', state: 'checkin-on' } }

export const MemorySharingReady: Story = { name: '기억 범위 · 개인 대화만', args: { screen: 'bot-memory', state: 'memory-sharing-ready' } }
export const MemorySharingEnabled: Story = { name: '기억 범위 · 다른 내 대화에서도', args: { screen: 'bot-memory', state: 'memory-sharing-enabled' } }
export const MemorySharingEmpty: Story = { name: '기억 범위 · 공통 정보 없음', args: { screen: 'bot-memory', state: 'memory-sharing-empty' } }
export const MemorySharingError: Story = { name: '기억 범위 · 읽기 실패', args: { screen: 'bot-memory', state: 'memory-sharing-error' } }
export const MemorySharingUnknown: Story = { name: '기억 범위 · 저장 결과 불명', args: { screen: 'bot-memory', state: 'memory-sharing-unknown' } }
export const MemorySharingConflict: Story = { name: '기억 범위 · 변경 충돌', args: { screen: 'bot-memory', state: 'memory-sharing-conflict' } }

export const MemorySharingChat: Story = { name: '기억 범위 · 함께하는 대화의 공통 정보 저장', args: { screen: 'chat', state: 'memory-sharing-chat' } }
export const MemorySharingChatUnknown: Story = { name: '기억 범위 · 저장 결과 읽기 실패', args: { screen: 'chat', state: 'memory-sharing-chat-unknown' } }
export const MemorySharingChatMismatch: Story = { name: '기억 범위 · 결과 불일치', args: { screen: 'chat', state: 'memory-sharing-chat-mismatch' } }

export const PreviousCancelled: Story = { name: '대화 · 이전 취소 후 대기', args: { screen: 'chat', state: 'previous-cancelled' } }

export const SkillReady: Story = { name: 'Skill · 라이브러리', args: { screen: 'skills', state: 'skill-ready' } }
export const SkillEmpty: Story = { name: 'Skill · 비어 있음', args: { screen: 'skills', state: 'skill-empty' } }
export const SkillError: Story = { name: 'Skill · 읽기 실패', args: { screen: 'skills', state: 'skill-error' } }
export const SkillUnknown: Story = { name: 'Skill · 생성 결과 불명', args: { screen: 'skills', state: 'skill-unknown' } }
export const SkillConflict: Story = { name: 'Skill · 편집 충돌', args: { screen: 'skills', state: 'skill-conflict' } }
export const SkillInactive: Story = { name: 'Skill · 사용 중지', args: { screen: 'skills', state: 'skill-inactive' } }
export const SkillSource: Story = { name: 'Skill · 완료 업무에서 초안', args: { screen: 'skills', state: 'skill-source' } }
export const SkillSourceRejected: Story = { name: 'Skill · 출처 확인 거절', args: { screen: 'skills', state: 'skill-source-rejected' } }
export const SkillAssignments: Story = { name: 'Skill · Bot별 선택', args: { screen: 'agents', state: 'skill-ready' } }
export const SkillSourceTask: Story = { name: 'Skill · 완료 업무에서 진입', args: { screen: 'chat', state: 'skill-source-task' } }

export const SkillReceiptSaved: Story = { name: '대화 Skill · 저장된 초안', args: { screen: 'chat', state: 'skill-receipt-saved' } }
export const SkillReceiptMismatch: Story = { name: '대화 Skill · 영수증 불일치', args: { screen: 'chat', state: 'skill-receipt-mismatch' } }
export const SkillReceiptUnknown: Story = { name: '대화 Skill · 결과 읽기 실패', args: { screen: 'chat', state: 'skill-receipt-unknown' } }
export const SkillReceiptHistory: Story = { name: '대화 Skill · 이후 변경된 Skill', args: { screen: 'chat', state: 'skill-receipt-history' } }
export const SkillReceiptReady: Story = { name: '대화 Skill · 이 Bot에서 사용 준비됨', args: { screen: 'chat', state: 'skill-receipt-ready' } }

export const SkillReceiptBindingChanged: Story = { name: '대화 Skill · 저장 후 Bot 사용 설정 변경', args: { screen: 'chat', state: 'skill-receipt-binding-changed' } }
export const SkillReceiptBindingMismatch: Story = { name: '대화 Skill · Bot 사용 설정 불일치', args: { screen: 'chat', state: 'skill-receipt-binding-mismatch' } }

export const RoutineNew: Story = { name: '반복 루틴 · 새로 만들기', args: { screen: 'agents', state: 'routine-new' } }
export const RoutineEmpty: Story = { name: '반복 루틴 · 빈 상태', args: { screen: 'agents', state: 'routine-empty' } }
export const RoutineNormal: Story = { name: '반복 루틴 · 예약됨', args: { screen: 'agents', state: 'routine-normal' } }
export const RoutinePaused: Story = { name: '반복 루틴 · 일시정지', args: { screen: 'agents', state: 'routine-paused' } }
export const RoutineWebhookEmpty: Story = { name: '웹훅 루틴 · 새로 만들기', args: { screen: 'agents', state: 'routine-webhook-empty' } }
export const RoutineWebhookConfigured: Story = { name: '웹훅 루틴 · 키 설정됨', args: { screen: 'agents', state: 'routine-webhook-configured' } }
export const RoutineWebhookIssued: Story = { name: '웹훅 루틴 · 키 발급과 가림', args: { screen: 'agents', state: 'routine-webhook-issued' } }
export const RoutineWebhookUnknown: Story = { name: '웹훅 루틴 · 발급 결과 불명', args: { screen: 'agents', state: 'routine-webhook-unknown' } }
export const RoutineWebhookRejected: Story = { name: '웹훅 루틴 · 발급 거부', args: { screen: 'agents', state: 'routine-webhook-rejected' } }
export const RoutineRunning: Story = { name: '반복 루틴 · 실행 중', args: { screen: 'agents', state: 'routine-running' } }
export const RoutineFailed: Story = { name: '반복 루틴 · 실패', args: { screen: 'agents', state: 'routine-failed' } }
export const RoutineUnknown: Story = { name: '반복 루틴 · 결과 불명', args: { screen: 'agents', state: 'routine-unknown' } }
export const RoutineConflict: Story = { name: '반복 루틴 · 변경 충돌', args: { screen: 'agents', state: 'routine-conflict' } }
export const RoutineApproval: Story = { name: '반복 루틴 · 승인 대기', args: { screen: 'agents', state: 'routine-approval' } }

export const RoutineReceiptRunFailed: Story = { name: '루틴 시험 · 실제 실패 확인', args: { screen: 'chat', state: 'routine-receipt-run-failed' } }
export const RoutineReceiptRunCompleted: Story = { name: '루틴 시험 · 실제 완료 확인', args: { screen: 'chat', state: 'routine-receipt-run-completed' } }
export const RoutineReceiptRunMismatch: Story = { name: '루틴 시험 · 결과 참조 불일치', args: { screen: 'chat', state: 'routine-receipt-run-mismatch' } }

export const RoutineReceiptSaved: Story = { name: '반복 루틴 대화 · 저장 확인', args: { screen: 'chat', state: 'routine-receipt-saved' } }
export const RoutineReceiptQueued: Story = { name: '반복 루틴 대화 · 시험 접수', args: { screen: 'chat', state: 'routine-receipt-queued' } }
export const RoutineReceiptUnknown: Story = { name: '반복 루틴 대화 · 결과 불명', args: { screen: 'chat', state: 'routine-receipt-unknown' } }
export const RoutineReceiptMismatch: Story = { name: '반복 루틴 대화 · 결과 불일치', args: { screen: 'chat', state: 'routine-receipt-mismatch' } }

export const PackageEmpty: Story = { name: 'Skill 패키지 · 빈 목록', args: { screen: 'skills', state: 'package-empty' } }
export const PackagePreview: Story = { name: 'Skill 패키지 · 미리보기와 설치', args: { screen: 'skills', state: 'package-preview' } }
export const PackageUnknown: Story = { name: 'Skill 패키지 · 결과 불명', args: { screen: 'skills', state: 'package-unknown' } }
export const PackageConflict: Story = { name: 'Skill 패키지 · 설치 충돌', args: { screen: 'skills', state: 'package-conflict' } }
export const PackageMalformed: Story = { name: 'Skill 패키지 · 잘못된 내용', args: { screen: 'skills', state: 'package-malformed' } }
export const PackageProvenance: Story = { name: 'Skill 패키지 · 수정된 출처', args: { screen: 'skills', state: 'package-provenance' } }

export const PageRevisionReady: Story = { name: '보고서 · 수정 요청', args: { screen: 'chat', state: 'page-revision-ready' } }
export const PageRevisionUnknown: Story = { name: '보고서 · 요청 결과 확인', args: { screen: 'chat', state: 'page-revision-unknown' } }
export const PageRevisionMissing: Story = { name: '보고서 · 요청 기록 없음', args: { screen: 'chat', state: 'page-revision-missing' } }
export const PageRevisionConflict: Story = { name: '보고서 · 버전 충돌', args: { screen: 'chat', state: 'page-revision-conflict' } }
export const PageRevisionFailed: Story = { name: '보고서 · 수정 실패', args: { screen: 'chat', state: 'page-revision-failed' } }

export const BotLibraryReady: Story = { name: 'Bot 보관함 · 저장된 결과', args: { screen: 'bot-library', state: 'bot-library-ready' } }
export const BotLibraryScreenshot: Story = { name: 'Bot 보관함 · 스크린샷 결과', args: { screen: 'bot-library', state: 'bot-library-screenshot' } }
export const BotLibraryEmpty: Story = { name: 'Bot 보관함 · 빈 상태', args: { screen: 'bot-library', state: 'bot-library-empty' } }
export const BotLibraryError: Story = { name: 'Bot 보관함 · 읽기 오류', args: { screen: 'bot-library', state: 'bot-library-error' } }
export const BotLibraryRevoked: Story = { name: 'Bot 보관함 · 접근 불가', args: { screen: 'bot-library', state: 'bot-library-revoked' } }

export const NotionSkillsReady: Story = { name: 'Notion 스킬 · 독립 지침 미리보기와 설치', args: { screen: 'apps', state: 'notion-skills-ready' } }
export const NotionSkillsUnknown: Story = { name: 'Notion 스킬 · 설치 결과 확인', args: { screen: 'apps', state: 'notion-skills-unknown' } }
export const NotionSkillsRejected: Story = { name: 'Notion 스킬 · 설치 거부', args: { screen: 'apps', state: 'notion-skills-rejected' } }
export const NotionOAuthPreparing: Story = { name: 'Notion 로그인 · 준비 중', args: { screen: 'apps', state: 'notion-oauth-preparing' } }
export const NotionOAuthAuthorizing: Story = { name: 'Notion 로그인 · 창 열기', args: { screen: 'apps', state: 'notion-oauth-authorizing' } }
export const NotionOAuthAuthorized: Story = { name: 'Notion 로그인 · 인증 후 연결', args: { screen: 'apps', state: 'notion-oauth-authorized' } }
export const NotionOAuthUnknown: Story = { name: 'Notion 로그인 · 결과 불명', args: { screen: 'apps', state: 'notion-oauth-unknown' } }
export const NotionOAuthConflict: Story = { name: 'Notion 로그인 · 연결 충돌', args: { screen: 'apps', state: 'notion-oauth-conflict' } }
export const NotionOAuthFailed: Story = { name: 'Notion 로그인 · 실패', args: { screen: 'apps', state: 'notion-oauth-failed' } }
export const NotionOAuthUnavailable: Story = { name: 'Notion 로그인 · 사용 불가', args: { screen: 'apps', state: 'notion-oauth-unavailable' } }
export const McpEmpty: Story = { name: '앱 연결 · 빈 목록', args: { screen: 'apps', state: 'mcp-empty' } }
export const McpNormal: Story = { name: '앱 연결 · 대상 확인과 발견', args: { screen: 'apps', state: 'mcp-normal' } }
export const McpTransportDiagnosticKnown: Story = { name: 'Notion 도구 확인 · 허용된 연결 진단', args: { screen: 'apps', state: 'mcp-transport-diagnostic-known' } }
export const McpTransportDiagnosticUntrusted: Story = { name: 'Notion 도구 확인 · 알 수 없는 진단 제외', args: { screen: 'apps', state: 'mcp-transport-diagnostic-untrusted' } }
export const McpDiscoveryPollComplete: Story = { name: 'Notion 도구 확인 · 진행 후 완료', args: { screen: 'apps', state: 'mcp-discovery-poll-complete' } }
export const McpDiscoveryPollFailed: Story = { name: 'Notion 도구 확인 · 진행 후 실패', args: { screen: 'apps', state: 'mcp-discovery-poll-failed' } }
export const McpDiscoveryPollUnknown: Story = { name: 'Notion 도구 확인 · 결과 불명 직접 확인', args: { screen: 'apps', state: 'mcp-discovery-poll-unknown' } }
export const McpDiscoveryKnownUnknown: Story = { name: '앱 연결 · 확인 후 결과 불명', args: { screen: 'apps', state: 'mcp-discovery-known-unknown' } }
export const McpDiscoveryMissing: Story = { name: '앱 연결 · 확인 기록 없음', args: { screen: 'apps', state: 'mcp-discovery-missing' } }
export const McpDiscoveryFailure: Story = { name: '앱 연결 · 발견 실패', args: { screen: 'apps', state: 'mcp-discovery-failure' } }
export const McpUnknown: Story = { name: '앱 연결 · 저장 결과 불명', args: { screen: 'apps', state: 'mcp-unknown' } }
export const McpConflict: Story = { name: '앱 연결 · 설정 충돌', args: { screen: 'apps', state: 'mcp-conflict' } }
export const McpDisconnected: Story = { name: '앱 연결 · 사용 중지', args: { screen: 'apps', state: 'mcp-disconnected' } }
export const McpRevoked: Story = { name: '앱 연결 · Bot 사용 회수', args: { screen: 'agents', state: 'mcp-revoked' } }
export const McpSecret: Story = { name: '앱 연결 · 비밀 설정', args: { screen: 'apps', state: 'mcp-secret' } }
export const McpSecretUnknown: Story = { name: '앱 연결 · 비밀 결과 불명', args: { screen: 'apps', state: 'mcp-secret-unknown' } }
export const McpDelegatedValid: Story = { name: '앱 연결 · 동료 Bot 승인', args: { screen: 'chat', state: 'mcp-delegated-valid' } }
export const McpDelegatedMissing: Story = { name: '앱 연결 · 동료 승인 확인 불가', args: { screen: 'chat', state: 'mcp-delegated-missing' } }
export const McpRoutineApproval: Story = { name: '루틴 앱 도구 · 일회성 승인', args: { screen: 'chat', state: 'mcp-routine-valid' } }
export const McpRoutineOriginChanged: Story = { name: '루틴 앱 도구 · 실행 출처 변경', args: { screen: 'chat', state: 'mcp-routine-origin-changed' } }
export const McpRoutineProofChanged: Story = { name: '루틴 앱 도구 · 실행 증명 변경', args: { screen: 'chat', state: 'mcp-routine-proof-changed' } }
export const McpApproval: Story = { name: '앱 연결 · 도구 실행 승인', args: { screen: 'chat', state: 'mcp-approval' } }
export const McpMany: Story = { name: '앱 연결 · 여러 도구 중 선택', args: { screen: 'agents', state: 'mcp-many' } }

export const McpDraftReady: Story = { name: '앱 승인 편집 · 수신자와 본문', args: { screen: 'chat', state: 'mcp-draft-ready' } }
export const McpDraftEmpty: Story = { name: '앱 승인 편집 · 편집 항목 없음', args: { screen: 'chat', state: 'mcp-draft-empty' } }
export const McpDraftSaving: Story = { name: '앱 승인 편집 · 저장 중', args: { screen: 'chat', state: 'mcp-draft-saving' } }
export const McpDraftApplied: Story = { name: '앱 승인 편집 · 저장 후 새 검토', args: { screen: 'chat', state: 'mcp-draft-applied' } }
export const McpDraftUnknown: Story = { name: '앱 승인 편집 · 결과 불명과 조회', args: { screen: 'chat', state: 'mcp-draft-unknown' } }
export const McpDraftConflict: Story = { name: '앱 승인 편집 · 충돌', args: { screen: 'chat', state: 'mcp-draft-conflict' } }
export const McpDraftRedacted: Story = { name: '앱 승인 편집 · 민감 항목 제외', args: { screen: 'chat', state: 'mcp-draft-redacted' } }
export const McpDraftStale: Story = { name: '앱 승인 편집 · 사용자 또는 요청 변경', args: { screen: 'chat', state: 'mcp-draft-stale' } }

export const MessageThreadReady: Story = { name: '대화 · 답장 모아보기 정상', args: { screen: 'chat', state: 'thread-ready' } }
export const MessageThreadEmpty: Story = { name: '대화 · 답장 모아보기 빈 목록', args: { screen: 'chat', state: 'thread-empty' } }
export const MessageThreadError: Story = { name: '대화 · 답장 모아보기 읽기 실패', args: { screen: 'chat', state: 'thread-error' } }
export const MessageThreadMore: Story = { name: '대화 · 답장 모아보기 더 보기', args: { screen: 'chat', state: 'thread-more' } }
export const MessageThreadNested: Story = { name: '대화 · 답장에 이어진 답변', args: { screen: 'chat', state: 'thread-nested' } }
export const MessageThreadDepthLimit: Story = { name: '대화 · 깊이 이어진 답장', args: { screen: 'chat', state: 'thread-depth-limit' } }
export const MessageThreadInvalidParent: Story = { name: '대화 · 잘못 연결된 답장 거부', args: { screen: 'chat', state: 'thread-invalid-parent' } }

export const PageWorkspaceReady: Story = { name: '페이지 · 대화와 필터', args: { screen: 'chat', state: 'page-workspace-ready' } }
export const PageWorkspaceUnknown: Story = { name: '페이지 · 요청 결과 확인', args: { screen: 'chat', state: 'page-workspace-unknown' } }
export const PageWorkspaceError: Story = { name: '페이지 · 대화 불러오기 실패', args: { screen: 'chat', state: 'page-workspace-error' } }
export const PageCreated: Story = { name: '페이지 · 생성 결과 카드', args: { screen: 'chat', state: 'page-created' } }
export const PageWorkspaceApproval: Story = { name: '페이지 · 해당 요청의 승인 대기', args: { screen: 'chat', state: 'page-workspace-approval' } }
export const PageWorkspaceFinished: Story = { name: '페이지 · 완료된 요청 상태 갱신', args: { screen: 'chat', state: 'page-workspace-finished' } }

export const BotLibrarySearch: Story = { name: 'Bot 보관함 · 전체 제목 검색', args: { screen: 'bot-library', state: 'bot-library-search-ready' } }
export const BotLibrarySearchError: Story = { name: 'Bot 보관함 · 검색 읽기 오류', args: { screen: 'bot-library', state: 'bot-library-search-error' } }
export const BotLibrarySearchStale: Story = { name: 'Bot 보관함 · 오래된 검색 위치', args: { screen: 'bot-library', state: 'bot-library-search-stale' } }
export const BotLibrarySearchLoading: Story = { name: 'Bot 보관함 · 검색 중', args: { screen: 'bot-library', state: 'bot-library-search-loading' } }

export const PageRenameReady: Story = { name: '페이지 이름 · 직접 변경', args: { screen: 'bot-library', state: 'bot-library-search-rename' } }
export const PageRenameUnknown: Story = { name: '페이지 이름 · 결과 확인', args: { screen: 'bot-library', state: 'bot-library-search-rename-unknown' } }
export const PageRenameConflict: Story = { name: '페이지 이름 · 버전 변경', args: { screen: 'bot-library', state: 'bot-library-search-rename-conflict' } }
export const PageRenameMissing: Story = { name: '페이지 이름 · 같은 요청 복구', args: { screen: 'bot-library', state: 'bot-library-search-rename-missing' } }
export const PageRenameSource: Story = { name: '페이지 이름 · Owner 변경 기록', args: { screen: 'chat', state: 'page-owner-rename-source' } }
export const PageWorkspaceFailure: Story = { name: '페이지 · 도구 완료 후 응답 실패', args: { screen: 'chat', state: 'page-workspace-failure' } }
export const PageWorkspaceFailureGeneric: Story = { name: '페이지 · 저장 검증 실패', args: { screen: 'chat', state: 'page-workspace-failure-generic' } }

export const BotPublishConnect: Story = { name:'템플릿 게시 · 서비스 연결',args:{screen:'agents',state:'bot-publish-connect'} }
export const BotPublishReady: Story = { name:'템플릿 게시 · 초안과 공개',args:{screen:'agents',state:'bot-publish-ready'} }
export const BotPublishUpdate: Story = { name:'템플릿 게시 · 수정',args:{screen:'agents',state:'bot-publish-update'} }
export const BotPublishRevoke: Story = { name:'템플릿 게시 · 폐기',args:{screen:'agents',state:'bot-publish-revoke'} }
export const BotPublishUnknown: Story = { name:'템플릿 게시 · 결과 불명',args:{screen:'agents',state:'bot-publish-unknown'} }
export const BotPublishMissing: Story = { name:'템플릿 게시 · 미접수',args:{screen:'agents',state:'bot-publish-missing'} }
export const BotPublishConflict: Story = { name:'템플릿 게시 · 변경 충돌',args:{screen:'agents',state:'bot-publish-conflict'} }
export const BotPublishReadError: Story = { name:'템플릿 게시 · 상태 읽기 실패',args:{screen:'agents',state:'bot-publish-error'} }

export const TeammatePageCreated: Story = { name: '동료 · 같은 방 위임 페이지 생성 (합성)', args: { screen: 'chat', state: 'teammate-page-created' } }

export const VaultReady: Story = { name: '보관함 · 저장됨', args: { screen:'vault', state:'vault-ready' } }
export const VaultNoSaved: Story = { name: '보관함 · 비밀번호 없음', args: { screen:'vault', state:'vault-no-saved' } }
export const VaultMismatch: Story = { name: '보관함 · 정보 변경', args: { screen:'vault', state:'vault-mismatch' } }
export const VaultCancelled: Story = { name: '보관함 · 입력 취소', args: { screen:'vault', state:'vault-cancelled' } }
export const VaultUnknown: Story = { name: '보관함 · 결과 불명', args: { screen:'vault', state:'vault-unknown' } }
export const VaultConflict: Story = { name: '보관함 · 변경 충돌', args: { screen:'vault', state:'vault-conflict' } }
export const VaultUnavailable: Story = { name: '보관함 · 연결 불가', args: { screen:'vault', state:'vault-unavailable' } }
export const VaultOtherOwner: Story = { name: '보관함 · 다른 사용자', args: { screen:'vault', state:'vault-other-owner' } }

export const VaultFillReady: Story = { name: '보관함 · 전용 브라우저 입력', args: { screen:'vault', state:'vault-fill-ready' } }
export const VaultFillCancelled: Story = { name: '보관함 · 전용 입력 취소', args: { screen:'vault', state:'vault-fill-cancelled' } }
export const VaultFillFailed: Story = { name: '보관함 · 전용 입력 실패', args: { screen:'vault', state:'vault-fill-failed' } }
export const VaultFillUnknown: Story = { name: '보관함 · 전용 입력 결과 불명', args: { screen:'vault', state:'vault-fill-unknown' } }

export const VaultFillRetry: Story = { name: '보관함 · 같은 탭에서 입력 다시 선택', args: { screen:'vault', state:'vault-fill-retry' } }

export const VaultFillPasswordOnly: Story = { name: '보관함 · 암호만 입력됨', args: { screen:'vault', state:'vault-fill-password-only' } }

export const VaultCsvReady: Story = { name: '보관함 CSV · 검토와 저장', args: { screen:'vault', state:'vault-csv-ready' } }
export const VaultCsvDuplicate: Story = { name: '보관함 CSV · 중복 건너뛰기', args: { screen:'vault', state:'vault-csv-duplicate' } }
export const VaultCsvInvalid: Story = { name: '보관함 CSV · 잘못된 행', args: { screen:'vault', state:'vault-csv-invalid' } }
export const VaultCsvUnknown: Story = { name: '보관함 CSV · 결과 불명 조회', args: { screen:'vault', state:'vault-csv-unknown' } }
export const VaultCsvRejected: Story = { name: '보관함 CSV · 저장 거부', args: { screen:'vault', state:'vault-csv-rejected' } }
export const VaultCsvCancelled: Story = { name: '보관함 CSV · 파일 선택 취소', args: { screen:'vault', state:'vault-csv-cancelled' } }

export const VaultCsvBindUnavailable: Story = { name: '보관함 CSV · 저장 준비 불가', args: { screen:'vault', state:'vault-csv-bind-unavailable' } }

export const VaultCsvBatch: Story = { name: '보관함 CSV · 검토 후 일괄 가져오기', args: { screen:'vault', state:'vault-csv-batch' } }
export const VaultCsvBatchUnknown: Story = { name: '보관함 CSV · 일괄 결과 불명 중단', args: { screen:'vault', state:'vault-csv-batch-unknown' } }

export const VaultContactsReady: Story = { name: '보관함 연락처 · 목록과 국제 주소', args: { screen:'vault', state:'vault-contacts-ready' } }
export const VaultContactsEmpty: Story = { name: '보관함 연락처 · 빈 목록', args: { screen:'vault', state:'vault-contacts-empty' } }
export const VaultContactsError: Story = { name: '보관함 연락처 · 읽기 실패', args: { screen:'vault', state:'vault-contacts-error' } }
export const VaultContactsConflict: Story = { name: '보관함 연락처 · 충돌', args: { screen:'vault', state:'vault-contacts-conflict' } }
export const VaultContactsUnknown: Story = { name: '보관함 연락처 · 결과 불명', args: { screen:'vault', state:'vault-contacts-unknown' } }

export const VaultCardReady: Story = { name: '보관함 카드 · 저장 정보', args: { screen:'vault', state:'vault-card-ready' } }
export const VaultCardMissing: Story = { name: '보관함 카드 · 미저장', args: { screen:'vault', state:'vault-card-missing' } }
export const VaultCardMismatch: Story = { name: '보관함 카드 · 정보 불일치', args: { screen:'vault', state:'vault-card-mismatch' } }
export const VaultCardUnavailable: Story = { name: '보관함 카드 · 연결 불가', args: { screen:'vault', state:'vault-card-unavailable' } }
export const VaultCardCancelled: Story = { name: '보관함 카드 · 입력 취소', args: { screen:'vault', state:'vault-card-cancelled' } }
export const VaultCardUnknown: Story = { name: '보관함 카드 · 저장 결과 불명', args: { screen:'vault', state:'vault-card-unknown' } }
export const VaultCardConflict: Story = { name: '보관함 카드 · 충돌', args: { screen:'vault', state:'vault-card-conflict' } }
export const VaultCardEmpty: Story = { name: '보관함 카드 · 빈 목록', args: { screen:'vault', state:'vault-card-empty' } }

export const VaultCardAuto: Story = { name: '보관함 카드 · 저장과 삭제', args: { screen:'vault', state:'vault-card-auto' } }
export const VaultCardNotStarted: Story = { name: '보관함 카드 · 입력 시작 전 중단', args: { screen:'vault', state:'vault-card-not-started' } }

export const VaultCardInvalidInput: Story = { name: '보관함 카드 · 입력 형식 확인', args: { screen:'vault', state:'vault-card-invalid-input' } }

export const VaultCardFillReady: Story = { name: '보관함 카드 입력 · 정상', args: { screen:'vault', state:'vault-card-fill-ready' } }
export const VaultCardFillRetry: Story = { name: '보관함 카드 입력 · 같은 탭 이동', args: { screen:'vault', state:'vault-card-fill-retry' } }
export const VaultCardFillUnknown: Story = { name: '보관함 카드 입력 · 결과 불명', args: { screen:'vault', state:'vault-card-fill-unknown' } }
export const VaultCardFillCancelled: Story = { name: '보관함 카드 입력 · 취소', args: { screen:'vault', state:'vault-card-fill-cancelled' } }
export const VaultCardFillUnavailable: Story = { name: '보관함 카드 입력 · 준비 불가', args: { screen:'vault', state:'vault-card-fill-unavailable' } }

export const ReadonlyFailureParked: Story = { name: 'Bot 답변 실패 · 브라우저 유지 확인 (합성)', args: { screen:'chat', state:'readonly-failure-parked' } }
export const ReadonlyFailureUnknown: Story = { name: 'Bot 답변 실패 · 브라우저 상태 확인 필요 (합성)', args: { screen:'chat', state:'readonly-failure-unknown' } }
