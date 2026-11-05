import { useEffect, useId, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { Bot, Cable, Folder, Settings2, Smartphone, Wallet, BookOpen, Users } from 'lucide-react'
import { LocaleSelector, useI18n } from '../i18n'
import AnchoredPopover from './AnchoredPopover'
import './AppMenu.css'

/** Infrequent app utilities stay out of the conversation's primary action row. */
export default function AppMenu() {
  const { t, locale } = useI18n(), location = useLocation(), id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  useEffect(() => { setOpen(false) }, [location.pathname])
  const close = (restore = false) => {
    setOpen(false)
    if (restore && trigger.current?.isConnected) trigger.current.focus({ preventScroll: true })
  }
  const links = [
    { to: '/vault', label: t('vault.title'), icon: Wallet },
    { to: '/memory-import', label: t('memoryImport.title'), icon: BookOpen },
    { to: '/skills', label: t('skills.title'), icon: BookOpen },
    { to: '/apps', label: t('mcp.title'), icon: Cable },
    { to: '/agents', label: t('dashboard.manageAgents'), icon: Bot },
    { to: '/projects', label: t('dashboard.manageProjects'), icon: Folder },
    { to: '/teams', label: locale === 'ko' ? '팀과 공유 Bot' : 'Teams and shared Bots', icon: Users },
    { to: '/connections', label: t('dashboard.manageConnections'), icon: Cable },
    { to: '/usage', label: t('usage.title'), icon: Wallet },
    { to: '/devices', label: t('devices.mobileTitle'), icon: Smartphone },
  ]
  return <>
    <button ref={trigger} type="button" className="icon-button app-menu-trigger" aria-label={t('app.ui.settings')}
      title={t('app.ui.settings')} aria-expanded={open} aria-controls={open ? id : undefined}
      onClick={() => setOpen(value => !value)}><Settings2 size={19} aria-hidden="true" /></button>
    {open && <AnchoredPopover anchor={trigger.current} onClose={close} className="app-menu-popover" minWidth={244} maxWidth={300} maxHeight={440}>
      <div id={id} className="app-menu" onKeyDown={event => {
        if (event.key === 'Escape' && !event.nativeEvent.isComposing) { event.preventDefault(); event.stopPropagation(); close(true) }
      }}>
        <p className="app-menu__heading">{t('app.ui.navigation')}</p>
        <nav aria-label={t('app.ui.navigation')}>{links.map(({ to, label, icon: Icon }) =>
          <Link key={to} to={to} onClick={() => close()} aria-current={location.pathname === to ? 'page' : undefined}><Icon size={17} aria-hidden="true" /><span>{label}</span></Link>)}</nav>
        <div className="app-menu__preferences"><LocaleSelector /></div>
      </div>
    </AnchoredPopover>}
  </>
}
