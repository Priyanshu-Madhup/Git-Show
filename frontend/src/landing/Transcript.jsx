import { useEffect, useRef, useState } from 'react'

const AGENT_NAMES = {
  you: 'You',
  orchestrator: 'Orchestrator',
  planner: 'Planner',
  reader: 'Reader',
  writer: 'Writer',
  verifier: 'Verifier',
  memory: 'Memory',
}

const CHAR_MS = 16
const LINE_PAUSE_MS = 320

// Prints a Git Show activity transcript line by line, the way it scrolls
// past in the app while a request runs. It plays once, the first time its
// stage comes into view; with reduced motion it shows everything at once.
export default function Transcript({ lines, play, reducedMotion }) {
  const [shown, setShown] = useState(() => (reducedMotion ? { line: lines.length, chars: 0 } : { line: 0, chars: 0 }))
  const started = useRef(reducedMotion)

  useEffect(() => {
    if (!play || started.current) return
    started.current = true
    let line = 0
    let chars = 0
    let timer
    const tick = () => {
      if (line >= lines.length) return
      const text = lines[line].text
      if (chars < text.length) {
        chars = Math.min(text.length, chars + 2)
        setShown({ line, chars })
        timer = setTimeout(tick, CHAR_MS)
      } else {
        line += 1
        chars = 0
        setShown({ line, chars })
        timer = setTimeout(tick, LINE_PAUSE_MS)
      }
    }
    timer = setTimeout(tick, 250)
    return () => clearTimeout(timer)
  }, [play, lines])

  return (
    <div className="transcript">
      <p className="sr-only">
        {lines.map((l) => (AGENT_NAMES[l.who] ? `${AGENT_NAMES[l.who]}: ${l.text}` : l.text)).join('. ')}
      </p>
      <ol className="transcript-lines" aria-hidden="true">
        {lines.map((l, i) => {
          if (i > shown.line) return null
          const text = i < shown.line ? l.text : l.text.slice(0, shown.chars)
          const typing = i === shown.line && shown.line < lines.length
          return (
            <li key={i} className={`transcript-line transcript-${l.who} ${l.tone ? `transcript-${l.tone}` : ''}`}>
              {l.who !== 'diff' && <span className={`agent-badge agent-${l.who}`}>{AGENT_NAMES[l.who]}</span>}
              <span className="transcript-text">
                {text}
                {typing && <span className="transcript-caret" />}
              </span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}
