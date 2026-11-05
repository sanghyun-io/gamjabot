import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from 'react'
import { ArrowLeft, Bot, Circle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useI18n } from '../i18n'
import { HeaderControlsSlot, useHostedHeader } from './AppHeaderFrame'

export function AppShell({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`app-shell ${className}`}>{children}</div>
}

export function Brand() {
  const { t } = useI18n()
  return <span className="brand"><span className="brand-mark" aria-hidden="true"><Bot size={18} /></span>{t('common.appName')}</span>
}

export function PageHeader({ title, subtitle, backTo, actions, width = 'wide', className = '' }: {
  title: ReactNode
  subtitle?: ReactNode
  backTo?: string
  actions?: ReactNode
  width?: 'wide' | 'medium' | 'narrow'
  className?: string
}) {
  const { t } = useI18n()
  const hosted = useHostedHeader()
  return <header className={`page-header ${className}`}><div className={`page-frame page-frame--${hosted ? 'wide' : width} page-header__inner`}>
    <div className="page-header__identity">
      {backTo && <Link to={backTo} className="icon-button" aria-label={t('common.back')}><ArrowLeft size={20} /></Link>}
      <div className="page-header__title-block"><h1 className="page-title">{title}</h1>{subtitle && <p className="page-subtitle">{subtitle}</p>}</div>
    </div>
    {actions && <div className="page-actions">{actions}</div>}
    <HeaderControlsSlot />
  </div></header>
}

export function Surface({ children, className = '', as = 'section', ...props }: {
  children: ReactNode
  className?: string
  as?: 'section' | 'article' | 'div'
} & HTMLAttributes<HTMLElement>) {
  const Element = as
  return <Element className={`surface ${className}`} {...props}>{children}</Element>
}

export function Button({ variant = 'secondary', size = 'default', className = '', children, ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'danger' | 'ghost'; size?: 'default' | 'compact' }) {
  return <button className={`button button--${variant}${size === 'compact' ? ' button--compact' : ''} ${className}`} {...props}>{children}</button>
}

export function Avatar({ name, tone = 'blue', size = 'medium' }: {
  name: string
  tone?: 'blue' | 'teal' | 'amber' | 'violet' | 'neutral'
  size?: 'small' | 'medium'
}) {
  const initial = Array.from(name.trim())[0] || '?'
  return <span className={`avatar avatar--${tone} avatar--${size}`} aria-hidden="true">{initial}</span>
}

export function StatusText({ children, tone = 'neutral', className = '' }: {
  children: ReactNode
  tone?: 'neutral' | 'busy' | 'warning' | 'error' | 'success'
  className?: string
}) {
  return <span className={`status-text status-text--${tone} ${className}`}><Circle size={9} fill="currentColor" aria-hidden="true" />{children}</span>
}

export function InlineNotice({ children, tone = 'info', className = '', ...props }: {
  children: ReactNode
  tone?: 'info' | 'warning' | 'danger' | 'success'
  className?: string
} & HTMLAttributes<HTMLDivElement>) {
  return <div className={`notice notice--${tone} ${className}`} {...props}>{children}</div>
}
