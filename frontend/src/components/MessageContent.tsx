import { Children, createContext, isValidElement, useContext, useEffect, useId, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import Markdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Link } from 'react-router-dom'
import { Check, Copy } from 'lucide-react'
import { useI18n } from '../i18n'
import { assistantVisibleMarkdown } from '../lib/assistantContent'
import type { Element } from 'hast'
import { messageSections, sectionFragment } from '../lib/messageSections'
import ClipboardTextFallback from './ClipboardTextFallback'

function safeHref(value: string): string {
  if (/^\/chat\/\d+(?:[?#].*)?$/.test(value) || sectionFragment(value)) return value
  try {
    return ['https:', 'http:', 'mailto:'].includes(new URL(value).protocol) ? value : ''
  } catch { return '' }
}

const MessageCopyContext = createContext<{ revision: object; allowed: () => boolean } | null>(null)

function CodeBlock({ children }: { children?: ReactNode }) {
  const { t } = useI18n(), context = useContext(MessageCopyContext), manualId = useId(), codeId = useId(), helpId = useId()
  const [outcome,setOutcome]=useState<{scope:object;status:'copied'|'failed'}|null>(null)
  const [pending,setPending]=useState(false), [manualScope,setManualScope]=useState<object|null>(null)
  const mounted=useRef(false), lock=useRef(false), manualIntent=useRef(0), figure=useRef<HTMLElement>(null), manualToggle=useRef<HTMLButtonElement>(null)
  const code = Children.toArray(children).find(isValidElement<{ children?: string; className?: string }>)
  const value = code && isValidElement<{ children?: string; className?: string }>(code)
    ? String(code.props.children ?? '').replace(/\n$/, '') : ''
  const language = code && isValidElement<{ className?: string }>(code)
    ? /(?:^|\s)language-([\w+-]+)/.exec(code.props.className ?? '')?.[1] : undefined
  const scope=useMemo(()=>({}),[context?.revision,value]), live=useRef({scope,allowed:context?.allowed})
  live.current={scope,allowed:context?.allowed}
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
  useEffect(()=>{setOutcome(null);setManualScope(null)},[scope])
  const valid=(expected=scope)=>mounted.current&&live.current.scope===expected&&(!live.current.allowed||live.current.allowed())&&!!figure.current?.isConnected&&figure.current.getClientRects().length>0
  const status=outcome?.scope===scope?outcome.status:'idle', manual=manualScope===scope
  const copy=async()=>{
    if(!valid()||lock.current)return
    const original=scope,manualAtStart=manualIntent.current;lock.current=true;setPending(true);setOutcome(null)
    try{
      if(!navigator.clipboard?.writeText)throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(value)
      if(valid(original))setOutcome({scope:original,status:'copied'})
    }catch{if(valid(original)){setOutcome({scope:original,status:'failed'});if(manualIntent.current===manualAtStart)setManualScope(original)}}
    finally{lock.current=false;if(mounted.current)setPending(false)}
  }
  const closeManual=()=>{
    if(!valid())return
    // Restore focus before removing the explicit close button, without stealing
    // focus from another window or a newer message/context.
    if(document.visibilityState==='visible'&&document.hasFocus()&&manualToggle.current?.isConnected
      &&figure.current?.contains(document.activeElement))manualToggle.current.focus({preventScroll:true})
    manualIntent.current++;setManualScope(null)
  }
  return <figure ref={figure} className="message-code">
    <div className="message-code__toolbar">
      <span id={codeId} className="metadata">{language || t('chat.code.region')}</span><div className="page-actions">
        <button type="button" className="button button--ghost button--compact" disabled={pending} aria-busy={pending}
          onClick={()=>void copy()} aria-label={t('chat.code.copy')}>
          {status==='copied'?<Check size={14} aria-hidden="true"/>:<Copy size={14} aria-hidden="true"/>}
          {t(pending?'common.loading':status==='copied'?'chat.code.copied':'chat.code.copy')}
        </button>
        <button ref={manualToggle} type="button" className="text-link" aria-expanded={manual} aria-controls={manual?manualId:undefined} onClick={()=>{if(valid()){manualIntent.current++;setManualScope(manual?null:scope)}}}>{t('chat.code.manualAction')}</button>
      </div>
    </div>
    <span className="sr-only" role="status">{status==='copied'?t('chat.code.copied'):''}</span>
    {status==='failed'&&<p role="alert" className="metadata">{t('chat.code.manualFailure')}</p>}
    {manual&&<div id={manualId} className="stack stack--tight" style={{padding:'.75rem'}}>
      <ClipboardTextFallback rows={6} text={value} label={t('chat.code.manualText')} caption={t('chat.code.manualHelp')}
        canUse={()=>valid()&&manual} onClose={closeManual} onInteract={()=>{manualIntent.current++}} busy={pending} onRetry={()=>{void copy()}}/>
    </div>}
    <p id={helpId} className="sr-only">{t('chat.code.keyboard')}</p>
    <pre tabIndex={0} role="region" aria-label={t('chat.code.region')} aria-describedby={helpId}>{children}</pre>
  </figure>
}

const MessageSectionContext = createContext<{ root: RefObject<HTMLDivElement | null>; allowed: () => boolean } | null>(null)
const sectionPlugins = [messageSections]

function MessageLink({ href, children, node }: { href?: string; children?: ReactNode; node?: Element }) {
  const section = useContext(MessageSectionContext), { t } = useI18n()
  if (!href) return <span>{children}</span>
  if (href.startsWith('/chat/')) return <Link to={href}>{children}</Link>
  if (!href.startsWith('#')) return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
  const slug = node?.properties['data-message-target']
  if (typeof slug !== 'string') return <span>{children} <span className="metadata">({t('chat.section.missing')})</span></span>
  return <button type="button" className="text-link message-section-link" onClick={() => {
    const root = section?.root.current
    if (!root?.isConnected || !section?.allowed() || document.visibilityState !== 'visible' || !document.hasFocus()
      || !root.getClientRects().length || root.closest('[hidden],[inert],[aria-hidden="true"]')) return
    // Compare tokens within this renderer; never interpolate Markdown into a
    // CSS selector or resolve another message's matching heading globally.
    const heading = Array.from(root.querySelectorAll<HTMLElement>('[data-message-heading]'))
      .find(item => item.dataset.messageHeading === slug)
    if (!heading?.isConnected || !heading.getClientRects().length || heading.closest('[hidden],[inert],[aria-hidden="true"]')) return
    heading.focus({ preventScroll: true }); heading.scrollIntoView({ block: 'center', inline: 'nearest' })
  }}>{children}</button>
}

function MarkdownTable({ children, node }: { children?: ReactNode; node?: Element }) {
  const { t } = useI18n(), captionId = useId(), helpId = useId()
  const ordinal = node?.properties['data-message-table']
  const number = typeof ordinal === 'number' && Number.isSafeInteger(ordinal) && ordinal > 0 ? ordinal : 1
  return <div className="message-table" role="region" tabIndex={0} aria-labelledby={captionId} aria-describedby={helpId}>
    <span id={helpId} className="sr-only">{t('chat.table.keyboard')}</span>
    <table><caption id={captionId} className="sr-only">{t('chat.table.label', { number })}</caption>{children}</table>
  </div>
}

// Stable component identities preserve selected text, anchors and copy state on refresh.
const components: Components = {
  pre: CodeBlock,
  table: MarkdownTable,
  th: ({ children, style }) => <th scope="col" style={style}>{children}</th>,
  p: ({ children }) => <p className="conversation-event__body">{children}</p>,
  img: ({ alt }) => <span className="metadata">{alt ? `[${alt}]` : ''}</span>,
  a: MessageLink,
}

/** Rendering only: canonical message text and exports retain their original bytes. */
export default function MessageContent({ content, assistant = false, copyScope = '', copyAllowed }: { content: string; assistant?: boolean; copyScope?: string; copyAllowed?: () => boolean }) {
  const root=useRef<HTMLDivElement>(null)
  const revision=useMemo(()=>({}),[assistant,copyScope]), gate=useRef(copyAllowed)
  gate.current=copyAllowed
  // Keep the live copy gate current even when the unchanged Markdown subtree
  // is reused. Scope changes still propagate to CodeBlock through context.
  const copyContext = useMemo(() => ({ revision, allowed: () => !gate.current || gate.current() }), [revision])
  const sectionContext = useMemo(() => ({ root, allowed: copyContext.allowed }), [copyContext])
  const body = useMemo(() => assistant ? assistantVisibleMarkdown(content) : content, [assistant, content])
  const rendered = useMemo(() => <Markdown remarkPlugins={[remarkGfm]} rehypePlugins={sectionPlugins} skipHtml
    urlTransform={(value, key) => key === 'href' ? safeHref(value) : ''}
    components={components}>{body}</Markdown>, [body])
  return <MessageCopyContext.Provider value={copyContext}><MessageSectionContext.Provider value={sectionContext}><div ref={root} className="message-content">
    {rendered}
  </div></MessageSectionContext.Provider></MessageCopyContext.Provider>
}
