import type { Preview } from '@storybook/react-vite'
import '../ui-preview/networkGuard'
import '../src/index.css'
import '../ui-preview/preview.css'

const preview: Preview = {
  parameters: {
    layout: 'fullscreen', controls: { expanded: false },
    options: { storySort: { order: ['Screens', 'Components'] } },
    viewport: { options: {
      desktop: { name: 'Desktop · 1200 × 800', styles: { width: '1200px', height: '800px' }, type: 'desktop' },
      narrow: { name: 'Narrow · 820 × 900', styles: { width: '820px', height: '900px' }, type: 'tablet' },
      phone: { name: 'Phone · 390 × 844', styles: { width: '390px', height: '844px' }, type: 'mobile' },
    } },
  },
  initialGlobals: { theme: 'dark', locale: 'ko' },
  globalTypes: {
    theme: { description: '화면 테마', toolbar: { title: 'Theme', icon: 'contrast', items: ['light', 'dark'] } },
    locale: { toolbar: { title: 'Language', icon: 'globe', items: ['ko', 'en', 'ja', 'de'] } },
  },
  decorators: [(Story, context) => {
    document.documentElement.dataset.previewTheme = context.globals.theme
    localStorage.setItem('gamja.locale', context.globals.locale)
    return Story()
  }],
}
export default preview
