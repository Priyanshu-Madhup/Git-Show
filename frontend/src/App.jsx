import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import logo from './assets/logo.png'
import './App.css'

const EXAMPLE_QUERIES = [
  'What changed in the last release',
  'Who has touched this file most',
  'Summarize the open pull requests',
]

const DOCK_TRANSITION = 'transform 620ms cubic-bezier(0.22, 1, 0.36, 1)'

const GitHubMark = () => (
  <svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-hidden="true">
    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
  </svg>
)

const escapeHtml = (text) =>
  text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')

const formatInline = (text) =>
  text
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>')
    .replace(/`(.+?)`/g, '<code>$1</code>')

const isTableRow = (line) => /^\s*\|.*\|\s*$/.test(line)
const isTableSeparator = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(line)

const parseTableCells = (line) => {
  let trimmed = line.trim()
  if (trimmed.startsWith('|')) trimmed = trimmed.slice(1)
  if (trimmed.endsWith('|')) trimmed = trimmed.slice(0, -1)
  return trimmed.split('|').map((cell) => cell.trim())
}

const isBullet = (line) => /^[-*]\s+/.test(line)
const isNumbered = (line) => /^\d+\.\s+/.test(line)
const isHeading = (line) => /^(#{1,6})\s+(.*)/.test(line)

// Groups *consecutive* lines of the same kind into one block, regardless of
// whether a blank line separates them from the next block — a heading
// immediately followed by a list (no blank line) is common LLM output and
// needs to render as <h4> then <ul>, not leak through as raw "### "/"* ".
const formatMessage = (raw) => {
  const lines = escapeHtml(raw).split('\n')
  const html = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    if (line.trim() === '') {
      i++
      continue
    }

    if (isTableRow(line) && isTableSeparator(lines[i + 1] || '')) {
      const headerCells = parseTableCells(line)
      const bodyRows = []
      i += 2
      while (i < lines.length && isTableRow(lines[i])) {
        bodyRows.push(parseTableCells(lines[i]))
        i++
      }
      const thead = `<thead><tr>${headerCells
        .map((cell) => `<th>${formatInline(cell)}</th>`)
        .join('')}</tr></thead>`
      const tbody = `<tbody>${bodyRows
        .map((row) => `<tr>${row.map((cell) => `<td>${formatInline(cell)}</td>`).join('')}</tr>`)
        .join('')}</tbody>`
      html.push(`<div class="table-wrap"><table>${thead}${tbody}</table></div>`)
      continue
    }

    const heading = line.match(/^(#{1,6})\s+(.*)/)
    if (heading) {
      html.push(`<h4>${formatInline(heading[2])}</h4>`)
      i++
      continue
    }

    if (isBullet(line)) {
      const items = []
      while (i < lines.length && isBullet(lines[i])) {
        items.push(`<li>${formatInline(lines[i].replace(/^[-*]\s+/, ''))}</li>`)
        i++
      }
      html.push(`<ul>${items.join('')}</ul>`)
      continue
    }

    if (isNumbered(line)) {
      const items = []
      while (i < lines.length && isNumbered(lines[i])) {
        items.push(`<li>${formatInline(lines[i].replace(/^\d+\.\s+/, ''))}</li>`)
        i++
      }
      html.push(`<ol>${items.join('')}</ol>`)
      continue
    }

    const paragraph = []
    while (
      i < lines.length &&
      lines[i].trim() !== '' &&
      !isHeading(lines[i]) &&
      !isBullet(lines[i]) &&
      !isNumbered(lines[i]) &&
      !(isTableRow(lines[i]) && isTableSeparator(lines[i + 1] || ''))
    ) {
      paragraph.push(lines[i])
      i++
    }
    html.push(`<p>${paragraph.map(formatInline).join('<br />')}</p>`)
  }

  return html.join('')
}

function App() {
  const [chatId] = useState(() => crypto.randomUUID())
  const [query, setQuery] = useState('')
  const [messages, setMessages] = useState([])
  const [hasStarted, setHasStarted] = useState(false)
  const [isSending, setIsSending] = useState(false)
  const [isStreaming, setIsStreaming] = useState(false)
  const [longWait, setLongWait] = useState(false)
  const [liveSteps, setLiveSteps] = useState([])
  const [user, setUser] = useState(null)
  const [repos, setRepos] = useState([])
  const [selectedRepo, setSelectedRepo] = useState('')

  useEffect(() => {
    fetch('/api/auth/me', { credentials: 'include' })
      .then((res) => (res.ok ? res.json() : null))
      .then(setUser)
      .catch(() => setUser(null))
  }, [])

  useEffect(() => {
    if (!user) {
      setRepos([])
      setSelectedRepo('')
      return
    }
    fetch('/api/github/repos', { credentials: 'include' })
      .then((res) => (res.ok ? res.json() : { repos: [] }))
      .then((data) => setRepos(data.repos || []))
      .catch(() => setRepos([]))
  }, [user])

  const handleLogout = async () => {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' })
    setUser(null)
  }

  const searchFieldRef = useRef(null)
  const flipFromRect = useRef(null)
  const messagesRef = useRef(null)
  const textareaRef = useRef(null)
  const streamTimerRef = useRef(null)

  useEffect(() => () => clearInterval(streamTimerRef.current), [])

  useLayoutEffect(() => {
    const el = textareaRef.current
    if (!el || !hasStarted) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [query, hasStarted])

  const streamAssistantReply = (fullText, pendingAction = null, steps = []) => {
    const totalChars = fullText.length
    const tickMs = 16
    const duration = Math.min(2600, Math.max(350, totalChars * 12))
    const totalTicks = Math.max(1, Math.round(duration / tickMs))
    const charsPerTick = Math.max(1, Math.ceil(totalChars / totalTicks))

    setMessages((prev) => [...prev, { role: 'assistant', content: '', pendingAction, steps }])
    setIsStreaming(true)

    let revealed = 0
    clearInterval(streamTimerRef.current)
    streamTimerRef.current = setInterval(() => {
      revealed = Math.min(totalChars, revealed + charsPerTick)
      setMessages((prev) => {
        const next = prev.slice()
        next[next.length - 1] = {
          role: 'assistant',
          content: fullText.slice(0, revealed),
          pendingAction,
          steps,
        }
        return next
      })
      if (revealed >= totalChars) {
        clearInterval(streamTimerRef.current)
        streamTimerRef.current = null
        setIsStreaming(false)
        textareaRef.current?.focus()
      }
    }, tickMs)
  }

  const handleConfirmAction = async (index, action) => {
    setMessages((prev) => {
      const next = prev.slice()
      next[index] = { ...next[index], pendingAction: null, actionState: 'confirmed' }
      return next
    })
    setIsSending(true)
    try {
      const res = await fetch('/api/github/actions/execute', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: action.id }),
      })
      const data = await res.json()
      setIsSending(false)
      if (!res.ok) {
        throw new Error(data.detail || 'The action failed.')
      }
      streamAssistantReply(data.reply)
    } catch (err) {
      setIsSending(false)
      streamAssistantReply(`Something went wrong: ${err.message}`)
    }
  }

  const handleCancelAction = (index, action) => {
    setMessages((prev) => {
      const next = prev.slice()
      next[index] = { ...next[index], pendingAction: null, actionState: 'cancelled' }
      return next
    })
    fetch('/api/github/actions/cancel', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: action.id }),
    }).catch(() => {})
  }

  useLayoutEffect(() => {
    const from = flipFromRect.current
    const el = searchFieldRef.current
    if (!from || !el) return
    flipFromRect.current = null

    const to = el.getBoundingClientRect()
    const dx = from.left + from.width / 2 - (to.left + to.width / 2)
    const dy = from.top - to.top
    const scaleX = from.width / to.width
    const scaleY = from.height / to.height

    el.style.transformOrigin = 'top center'
    el.style.transition = 'none'
    el.style.transform = `translate(${dx}px, ${dy}px) scale(${scaleX}, ${scaleY})`

    // Force a reflow so the browser registers the starting transform
    // before we animate away from it.
    el.getBoundingClientRect()

    requestAnimationFrame(() => {
      el.style.transition = DOCK_TRANSITION
      el.style.transform = 'translate(0, 0) scale(1, 1)'
    })
  }, [hasStarted])

  useEffect(() => {
    const el = messagesRef.current
    if (el) {
      el.scrollTop = el.scrollHeight
    }
  }, [messages, isSending, isStreaming])

  const sendMessage = async (text) => {
    const trimmed = text.trim()
    if (!trimmed || isSending || isStreaming) return

    if (!hasStarted) {
      flipFromRect.current = searchFieldRef.current.getBoundingClientRect()
      setHasStarted(true)
    }

    setQuery('')
    setMessages((prev) => [...prev, { role: 'user', content: trimmed }])
    setIsSending(true)
    setLongWait(false)
    setLiveSteps([])

    const controller = new AbortController()
    // Up to MAX_TOOL_ROUNDS backend rounds, each with its own LLM + GitHub
    // API latency; the live step timeline gives feedback in the meantime,
    // so this can afford to be generous rather than aborting a request
    // that's still legitimately working through several tool calls.
    const timeout = setTimeout(() => controller.abort(), 300000)
    const longWaitTimer = setTimeout(() => setLongWait(true), 4000)

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: trimmed, chat_id: chatId, repo: selectedRepo || null }),
        signal: controller.signal,
      })

      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'The request failed.')
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let final = null
      const steps = []

      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let newlineIndex
        while ((newlineIndex = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, newlineIndex).trim()
          buffer = buffer.slice(newlineIndex + 1)
          if (!line) continue
          const event = JSON.parse(line)
          if (event.type === 'step') {
            steps.push({ tool: event.tool, label: event.label })
            setLiveSteps(steps.slice())
          } else if (event.type === 'final') {
            final = event
          }
        }
      }

      setIsSending(false)
      setLiveSteps([])
      if (!final) {
        throw new Error('The response ended unexpectedly.')
      }
      streamAssistantReply(final.reply, final.pending_action || null, steps)
    } catch (err) {
      setIsSending(false)
      setLiveSteps([])
      const message = err.name === 'AbortError' ? 'That took too long and timed out.' : err.message
      streamAssistantReply(`Something went wrong: ${message}`)
    } finally {
      clearTimeout(timeout)
      clearTimeout(longWaitTimer)
      setLongWait(false)
    }
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    sendMessage(query)
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(query)
    }
  }

  return (
    <div className="page">
      <div className={`frame ${hasStarted ? 'frame-chat' : ''}`}>
        <div className="orbs" aria-hidden="true">
          <span className="orb orb-1" />
          <span className="orb orb-2" />
          <span className="orb orb-3" />
          <span className="orb orb-4" />
          <span className="orb orb-5" />
        </div>

        <header className="topbar">
          <button className="icon-btn menu-btn" aria-label="Open menu" type="button">
            <span />
            <span />
            <span />
          </button>
          <div className="topbar-right">
            {user && repos.length > 0 && (
              <select
                className="repo-picker"
                value={selectedRepo}
                onChange={(e) => setSelectedRepo(e.target.value)}
                title="Repository the assistant will act on"
              >
                <option value="">Choose a repository…</option>
                {repos.map((r) => (
                  <option key={r.full_name} value={r.full_name}>
                    {r.full_name}
                  </option>
                ))}
              </select>
            )}
            {user ? (
              <button
                className="avatar avatar-user"
                type="button"
                onClick={handleLogout}
                title={`Sign out (${user.login})`}
              >
                <img src={user.avatar_url} alt={user.login} />
              </button>
            ) : (
              <a className="auth-btn auth-btn-topbar" href="/api/auth/github/login" title="Sign in with GitHub">
                <GitHubMark />
                Sign in
              </a>
            )}
          </div>
        </header>

        <main className={`hero ${hasStarted ? 'hero-chat' : ''}`}>
          <div className="glow" aria-hidden="true" />

          <div className={`intro ${hasStarted ? 'intro-hidden' : ''}`}>
            <div className="brand">
              <img className="brand-logo" src={logo} alt="Git Show logo" />
              <div className="brand-text">
                <h1 className="brand-title">
                  Git <span>Show</span>
                </h1>
                <p className="brand-tagline">
                  Ask your repository's history anything, in plain language.
                </p>
              </div>
            </div>

            {!user && (
              <div className="intro-auth">
                <p className="intro-auth-copy">
                  Sign in with GitHub to let Git Show read your repositories and act
                  on them — every change is proposed first and only runs after you
                  confirm it.
                </p>
                <a className="auth-btn" href="/api/auth/github/login">
                  <GitHubMark />
                  Sign in with GitHub
                </a>
              </div>
            )}
          </div>

          {hasStarted && (
            <div className="messages" ref={messagesRef}>
              <div className="messages-inner">
                {messages.map((m, i) => (
                  <div key={i} className={`bubble bubble-${m.role}`}>
                    {m.steps && m.steps.length > 0 && (
                      <ol className="timeline timeline-done">
                        {m.steps.map((s, si) => (
                          <li key={si} className="timeline-step done">
                            <span className="timeline-dot" />
                            <span className="timeline-label">{s.label}</span>
                          </li>
                        ))}
                      </ol>
                    )}
                    <div
                      className="bubble-content"
                      dangerouslySetInnerHTML={{ __html: formatMessage(m.content) }}
                    />
                    {m.pendingAction && !(isStreaming && i === messages.length - 1) && (
                      <div className="action-card">
                        <div className="action-card-label">
                          Proposed action: <code>{m.pendingAction.tool}</code>
                        </div>
                        <pre className="action-card-args">
                          {JSON.stringify(m.pendingAction.arguments, null, 2)}
                        </pre>
                        <div className="action-card-buttons">
                          <button
                            type="button"
                            className="action-btn action-confirm"
                            onClick={() => handleConfirmAction(i, m.pendingAction)}
                          >
                            Confirm
                          </button>
                          <button
                            type="button"
                            className="action-btn action-cancel"
                            onClick={() => handleCancelAction(i, m.pendingAction)}
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    )}
                    {m.actionState === 'cancelled' && (
                      <p className="action-resolved">Action cancelled.</p>
                    )}
                  </div>
                ))}
                {isSending && (
                  <div className="bubble bubble-assistant bubble-typing-bubble">
                    {liveSteps.length > 0 ? (
                      <ol className="timeline">
                        {liveSteps.map((s, si) => (
                          <li
                            key={si}
                            className={`timeline-step ${si === liveSteps.length - 1 ? 'active' : 'done'}`}
                          >
                            <span className="timeline-dot" />
                            <span className="timeline-label">{s.label}</span>
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <div className="bubble-typing">
                        <span />
                        <span />
                        <span />
                      </div>
                    )}
                    {longWait && (
                      <p className="typing-note">Still working — this one needs a few steps…</p>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}

          <form className={`search ${hasStarted ? 'search-docked' : ''}`} onSubmit={handleSubmit}>
            <label className="sr-only" htmlFor="query">
              Ask your query
            </label>
            <div className="search-field" ref={searchFieldRef}>
              <svg
                className="search-icon"
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                aria-hidden="true"
              >
                <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="1.6" />
                <path d="M21 21L16.65 16.65" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              </svg>
              <textarea
                id="query"
                ref={textareaRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Ask your query  . . ."
                rows={hasStarted ? 1 : 3}
              />
              <button
                className="submit-btn"
                type="submit"
                aria-label="Ask"
                disabled={isSending || isStreaming || !query.trim()}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path
                    d="M5 12H19M19 12L13 6M19 12L13 18"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
            </div>

            <div className={`chips-wrap ${hasStarted ? 'intro-hidden' : ''}`}>
              <div className="chips">
                {EXAMPLE_QUERIES.map((example) => (
                  <button
                    key={example}
                    type="button"
                    className="chip"
                    onClick={() => setQuery(example)}
                  >
                    {example}
                  </button>
                ))}
              </div>

              <p className="hint">Press Enter to ask, Shift-Enter for a new line</p>
            </div>
          </form>
        </main>
      </div>
    </div>
  )
}

export default App
