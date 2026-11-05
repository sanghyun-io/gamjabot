import { useEffect, useRef, useState } from 'react'
import { createPreviewScenario, type PreviewScenarioOptions } from './fixtures'

export default function Scenario({ incomingMessages = 0, ...options }: PreviewScenarioOptions & { incomingMessages?: number }) {
  const [scenario] = useState(() => createPreviewScenario(options))
  const delivered = useRef(0)
  useEffect(() => {
    const count = Number.isSafeInteger(incomingMessages) ? Math.max(0, Math.min(10, incomingMessages)) : 0
    const previous = delivered.current
    delivered.current = Math.max(previous, count)
    for (let index = previous; index < count; index += 1) scenario.addIncomingMessage()
  }, [scenario, incomingMessages])
  useEffect(() => () => scenario.dispose(), [scenario])
  return scenario.element
}
