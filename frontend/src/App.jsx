import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import logo from './assets/logo.png'
import RepoPicker from './RepoPicker.jsx'
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

const ACTION_STATE_NOTES = {
  cancelled: 'Action cancelled.',
  expired: 'This action expired before it was confirmed.',
  failed: 'This action failed.',
}

const AGENT_NAMES = {
  orchestrator: 'Orchestrator',
  planner: 'Planner',
  reader: 'Reader',
  writer: 'Writer',
  utility: 'Utility',
  verifier: 'Verifier',
  memory: 'Memory',
}

// Writes that can't be undone from Git Show get a louder confirmation card.
const DESTRUCTIVE_TOOLS = new Set(['delete_file', 'delete_branch', 'merge_pull_request'])

const Timeline = ({ entries, live = false }) => (
  <ol className={`timeline ${live ? '' : 'timeline-done'}`}>
    {entries.map((entry, i) => {
      const isLast = i === entries.length - 1
      const state = live && isLast ? 'active' : 'done'
      if (entry.kind === 'agent') {
        return (
          <li key={i} className={`timeline-step timeline-agent ${state}`}>
            <span className="timeline-dot" />
            <span className={`agent-badge agent-${entry.agent}`}>{AGENT_NAMES[entry.agent] || entry.agent}</span>
            <span className="timeline-label">{entry.label}</span>
          </li>
        )
      }
      return (
        <li key={i} className={`timeline-step timeline-tool ${state}`}>
          <span className="timeline-dot" />
          <span className="timeline-label">{entry.label}</span>
        </li>
      )
    })}
  </ol>
)

const ActivityLog = ({ entries, defaultOpen }) => {
  const [open, setOpen] = useState(defaultOpen)
  const agents = entries.filter((e) => e.kind === 'agent').length
  const tools = entries.length - agents
  return (
    <div className="activity-log">
      <button type="button" className="activity-toggle" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        {open ? 'Hide' : 'Show'} agent activity
        <span className="activity-count">
          {agents > 0 ? `${agents} hand-off${agents === 1 ? '' : 's'}` : ''}
          {agents > 0 && tools > 0 ? ' · ' : ''}
          {tools > 0 ? `${tools} tool call${tools === 1 ? '' : 's'}` : ''}
        </span>
      </button>
      {open && <Timeline entries={entries} />}
    </div>
  )
}

const PLAN_STATUS_LABELS = { done: 'Done', skipped: 'Skipped', pending: 'To do', active: 'In progress' }

const PlanCard = ({ plan, live = false }) => {
  const done = plan.steps.filter((st) => st.status === 'done').length
  return (
    <div className="plan-card">
      <div className="plan-card-head">
        <span className="agent-badge agent-planner">Plan</span>
        <span className="plan-card-goal">{plan.goal || 'Working on it'}</span>
        <span className="plan-card-progress">
          {done}/{plan.steps.length}
          {plan.version > 1 ? ` · revision ${plan.version}` : ''}
        </span>
      </div>
      <ol className="plan-steps">
        {plan.steps.map((st) => {
          const status = live && st.id === plan.active_step_id && st.status === 'pending' ? 'active' : st.status
          return (
            <li key={st.id} className={`plan-step plan-step-${status}`}>
              <span className="plan-step-marker" aria-label={PLAN_STATUS_LABELS[status] || status} />
              <span className={`agent-badge agent-${st.agent}`}>{AGENT_NAMES[st.agent] || st.agent}</span>
              <span className="plan-step-text">{st.instruction}</span>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

const DiffView = ({ diff }) => (
  <pre className="diff-view">
    {diff.split('\n').map((line, i) => {
      let cls = ''
      if (line.startsWith('+++') || line.startsWith('---')) cls = 'diff-file'
      else if (line.startsWith('+')) cls = 'diff-add'
      else if (line.startsWith('-')) cls = 'diff-del'
      else if (line.startsWith('@@')) cls = 'diff-hunk'
      return (
        <span key={i} className={`diff-line ${cls}`}>
          {line || ' '}
          {'\n'}
        </span>
      )
    })}
  </pre>
)

const ActionCard = ({ action, disabled, onConfirm, onCancel }) => {
  const [showChanges, setShowChanges] = useState(false)
  const [showDetails, setShowDetails] = useState(false)
  const destructive = DESTRUCTIVE_TOOLS.has(action.tool)
  const diff = action.preview?.diff
  return (
    <div className={`action-card ${destructive ? 'action-card-destructive' : ''}`}>
      <div className="action-card-label">
        <span className="agent-badge agent-writer">Writer</span>
        Proposed GitHub action: <code>{action.tool}</code>
        {action.arguments?.owner && action.arguments?.repo && (
          <>
            {' '}
            in <code>{`${action.arguments.owner}/${action.arguments.repo}`}</code>
          </>
        )}
        {action.preview?.path && (
          <>
            {' '}
            on <code>{action.preview.path}</code>
          </>
        )}
      </div>
      {destructive && (
        <p className="action-card-warning">This can't be undone from Git Show. Review it carefully before confirming.</p>
      )}
      <div className="action-card-toggles">
        {diff !== undefined && (
          <button type="button" className="action-toggle" onClick={() => setShowChanges((v) => !v)}>
            {showChanges ? 'Hide changes' : 'View changes'}
          </button>
        )}
        <button type="button" className="action-toggle" onClick={() => setShowDetails((v) => !v)}>
          {showDetails ? 'Hide details' : 'Details'}
        </button>
      </div>
      {showChanges && diff !== undefined && (diff ? <DiffView diff={diff} /> : <p className="action-resolved">No textual changes.</p>)}
      {showDetails && <pre className="action-card-args">{JSON.stringify(action.arguments, null, 2)}</pre>}
      <div className="action-card-buttons">
        <button type="button" className="action-btn action-cancel" onClick={onCancel} disabled={disabled}>
          Cancel
        </button>
        <button
          type="button"
          className={`action-btn action-confirm ${destructive ? 'action-confirm-destructive' : ''}`}
          onClick={onConfirm}
          disabled={disabled}
        >
          Confirm
        </button>
      </div>
    </div>
  )
}

const formatRelativeTime = (iso) => {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

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

    // Fenced code block. Lines are already HTML-escaped, and inline
    // formatting must not touch code, so they go in as-is.
    const fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/)
    if (fence) {
      const code = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) {
        code.push(lines[i])
        i++
      }
      i++
      const lang = fence[2] ? ` data-lang="${fence[2]}"` : ''
      html.push(`<pre class="code-block"${lang}><code>${code.join('\n')}</code></pre>`)
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
      !/^\s*(`{3,}|~{3,})/.test(lines[i]) &&
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
  const [chatId, setChatId] = useState(() => crypto.randomUUID())
  const [conversations, setConversations] = useState([])
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [messages, setMessages] = useState([])
  const [hasStarted, setHasStarted] = useState(false)
  const [isSending, setIsSending] = useState(false)
  const [isStreaming, setIsStreaming] = useState(false)
  const [longWait, setLongWait] = useState(false)
  const [liveTimeline, setLiveTimeline] = useState([])
  const [livePlan, setLivePlan] = useState(null)
  const [user, setUser] = useState(null)
  const [authChecked, setAuthChecked] = useState(false)
  const [repos, setRepos] = useState([])
  const [selectedRepo, setSelectedRepo] = useState('')
  // Whose repo list is loaded, so "no repos yet" isn't mistaken for "still loading".
  const [reposLoadedFor, setReposLoadedFor] = useState(null)
  const [installUrl, setInstallUrl] = useState(null)

  useEffect(() => {
    fetch('/api/auth/me', { credentials: 'include' })
      .then((res) => (res.ok ? res.json() : null))
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setAuthChecked(true))
    fetch('/api/config')
      .then((res) => (res.ok ? res.json() : {}))
      .then((config) => setInstallUrl(config.install_url || null))
      .catch(() => setInstallUrl(null))
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
      .finally(() => setReposLoadedFor(user.login))
  }, [user])

  const needsInstall = !!user && reposLoadedFor === user.login && repos.length === 0

  const loadConversations = useCallback(() => {
    fetch('/api/conversations', { credentials: 'include' })
      .then((res) => (res.ok ? res.json() : { conversations: [] }))
      .then((data) => setConversations(data.conversations || []))
      .catch(() => setConversations([]))
  }, [])

  useEffect(() => {
    if (user) loadConversations()
  }, [user, loadConversations])

  useEffect(() => {
    if (!sidebarOpen) return
    const onKeyDown = (e) => {
      if (e.key === 'Escape') setSidebarOpen(false)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [sidebarOpen])

  const searchFieldRef = useRef(null)
  const flipFromRect = useRef(null)
  const repoPickerRef = useRef(null)
  const repoFlipFromRect = useRef(null)
  const messagesRef = useRef(null)
  const textareaRef = useRef(null)
  const streamTimerRef = useRef(null)

  useEffect(() => () => clearInterval(streamTimerRef.current), [])

  const stopRevealing = () => {
    clearInterval(streamTimerRef.current)
    streamTimerRef.current = null
    setIsStreaming(false)
  }

  const startNewChat = () => {
    if (isSending) return
    stopRevealing()
    setChatId(crypto.randomUUID())
    setMessages([])
    setQuery('')
    setHasStarted(false)
    setSidebarOpen(false)
  }

  const openConversation = async (id) => {
    if (isSending) return
    setSidebarOpen(false)
    if (id === chatId && hasStarted) return
    try {
      const res = await fetch(`/api/conversations/${id}`, { credentials: 'include' })
      if (!res.ok) throw new Error('Chat not found')
      const data = await res.json()
      stopRevealing()
      setChatId(data.id)
      setMessages(
        data.messages.map((m) => ({
          role: m.role,
          content: m.content,
          steps: m.steps,
          plan: m.plan || null,
          runId: m.run_id || null,
          canContinue: !!m.can_continue,
          pendingAction: m.pending_action || null,
          actionState: m.action_state || null,
        })),
      )
      setSelectedRepo(repos.some((r) => r.full_name === data.repo) ? data.repo : '')
      setHasStarted(true)
    } catch {
      loadConversations()
    }
  }

  const deleteConversation = async (id) => {
    const res = await fetch(`/api/conversations/${id}`, { method: 'DELETE', credentials: 'include' })
    if (!res.ok) return
    setConversations((prev) => prev.filter((c) => c.id !== id))
    if (id === chatId) startNewChat()
  }

  const handleLogout = async () => {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' })
    startNewChat()
    setConversations([])
    setUser(null)
  }

  useLayoutEffect(() => {
    const el = textareaRef.current
    if (!el || !hasStarted) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [query, hasStarted])

  const streamAssistantReply = (fullText, extras = {}) => {
    const totalChars = fullText.length
    const tickMs = 16
    const duration = Math.min(2600, Math.max(350, totalChars * 12))
    const totalTicks = Math.max(1, Math.round(duration / tickMs))
    const charsPerTick = Math.max(1, Math.ceil(totalChars / totalTicks))

    setMessages((prev) => [...prev, { role: 'assistant', content: '', ...extras }])
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
          ...extras,
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

  // Every agent run streams NDJSON events: agent hand-offs, tool steps, plan
  // revisions, and one final reply. Chat, Confirm, Cancel, and Continue all
  // resume the same kind of run, so they share this reader. There's no
  // client-side timeout: the orchestrator decides when a run stops or pauses.
  const runStream = async (url, body) => {
    setIsSending(true)
    setLongWait(false)
    setLiveTimeline([])
    setLivePlan(null)
    const longWaitTimer = setTimeout(() => setLongWait(true), 4000)

    try {
      const res = await fetch(url, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'The request failed.')
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let final = null
      let plan = null
      const timeline = []

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
          if (event.type === 'agent' || event.type === 'step') {
            timeline.push({
              kind: event.type === 'agent' ? 'agent' : 'tool',
              agent: event.agent,
              tool: event.tool,
              label: event.label,
            })
            setLiveTimeline(timeline.slice())
          } else if (event.type === 'plan') {
            plan = event.plan
            setLivePlan(plan)
          } else if (event.type === 'final') {
            final = event
          }
        }
      }

      setIsSending(false)
      setLiveTimeline([])
      setLivePlan(null)
      if (!final) {
        throw new Error('The response ended unexpectedly.')
      }
      if (final.signed_out) {
        // GitHub rejected the token: the backend dropped the session, so
        // show the Sign in button again.
        setUser(null)
      }
      streamAssistantReply(final.reply, {
        steps: final.steps || timeline,
        plan: final.plan || plan,
        pendingAction: final.pending_action || null,
        canContinue: !!final.can_continue,
        runId: final.run_id || null,
      })
      if (user) loadConversations()
    } catch (err) {
      setIsSending(false)
      setLiveTimeline([])
      setLivePlan(null)
      streamAssistantReply(`Something went wrong: ${err.message}`)
    } finally {
      clearTimeout(longWaitTimer)
      setLongWait(false)
    }
  }

  const updateMessage = (index, patch) => {
    setMessages((prev) => {
      const next = prev.slice()
      next[index] = { ...next[index], ...patch }
      return next
    })
  }

  const handleConfirmAction = (index, action) => {
    updateMessage(index, { pendingAction: null, actionState: 'confirmed' })
    runStream('/api/github/actions/execute', { id: action.id })
  }

  const handleCancelAction = (index, action) => {
    updateMessage(index, { pendingAction: null, actionState: 'cancelled' })
    runStream('/api/github/actions/cancel', { id: action.id })
  }

  const handleContinue = (index, message) => {
    updateMessage(index, { canContinue: false })
    runStream(`/api/runs/${message.runId}/continue`)
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

  // Same FLIP technique as the search field above, but the repo picker
  // actually swaps DOM parents (hero row -> topbar) rather than just
  // changing its own CSS position, since it needs to end up docked
  // alongside the avatar, not wherever the search bar lands. The ref still
  // resolves to whichever instance is currently mounted, so the technique
  // works the same way across the reparent.
  useLayoutEffect(() => {
    const from = repoFlipFromRect.current
    const el = repoPickerRef.current
    if (!from || !el) return
    repoFlipFromRect.current = null

    const to = el.getBoundingClientRect()
    const dx = from.left + from.width / 2 - (to.left + to.width / 2)
    const dy = from.top - to.top
    const scaleX = from.width / to.width
    const scaleY = from.height / to.height

    el.style.transformOrigin = 'top center'
    el.style.transition = 'none'
    el.style.transform = `translate(${dx}px, ${dy}px) scale(${scaleX}, ${scaleY})`

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
    if (!trimmed || !user || isSending || isStreaming) return

    if (!hasStarted) {
      flipFromRect.current = searchFieldRef.current.getBoundingClientRect()
      if (repoPickerRef.current) {
        repoFlipFromRect.current = repoPickerRef.current.getBoundingClientRect()
      }
      setHasStarted(true)
    }

    setQuery('')
    setMessages((prev) => [...prev, { role: 'user', content: trimmed }])
    await runStream('/api/chat', { message: trimmed, chat_id: chatId, repo: selectedRepo || null })
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

  // On the home screen the picker sits in the chat input; once a chat starts
  // it docks in the top bar (see the dock animation above).
  const showInlinePicker = !hasStarted && !!user && repos.length > 0

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

        <div
          className={`sidebar-backdrop ${sidebarOpen ? 'sidebar-backdrop-open' : ''}`}
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
        <aside
          id="chat-sidebar"
          className={`sidebar ${sidebarOpen ? 'sidebar-open' : ''}`}
          aria-label="Chat history"
          inert={!sidebarOpen}
        >
          <div className="sidebar-head">
            <h2 className="sidebar-title">Chats</h2>
            <button
              type="button"
              className="sidebar-close"
              aria-label="Close chat history"
              onClick={() => setSidebarOpen(false)}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M6 6L18 18M18 6L6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
              </svg>
            </button>
          </div>

          <button type="button" className="sidebar-new" onClick={startNewChat} disabled={isSending}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M12 5V19M5 12H19" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
            New chat
          </button>

          {user ? (
            conversations.length > 0 ? (
              <ul className="sidebar-list">
                {conversations.map((c) => (
                  <li key={c.id} className={`sidebar-item ${c.id === chatId ? 'sidebar-item-active' : ''}`}>
                    <button
                      type="button"
                      className="sidebar-item-open"
                      onClick={() => openConversation(c.id)}
                      disabled={isSending}
                      title={c.title}
                    >
                      <span className="sidebar-item-title">{c.title}</span>
                      <span className="sidebar-item-meta">
                        {c.repo ? `${c.repo} · ` : ''}
                        {formatRelativeTime(c.updated_at)}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="sidebar-item-delete"
                      aria-label={`Delete chat: ${c.title}`}
                      title="Delete chat"
                      onClick={() => deleteConversation(c.id)}
                      disabled={isSending && c.id === chatId}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <path
                          d="M5 7H19M10 11V17M14 11V17M6 7L7 19H17L18 7M9 7V4H15V7"
                          stroke="currentColor"
                          strokeWidth="1.6"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="sidebar-empty">Your chats will appear here.</p>
            )
          ) : (
            <p className="sidebar-empty">Sign in with GitHub to keep your chat history.</p>
          )}
        </aside>

        <header className="topbar">
          <button
            className="icon-btn menu-btn"
            aria-label="Chat history"
            aria-expanded={sidebarOpen}
            aria-controls="chat-sidebar"
            type="button"
            onClick={() => setSidebarOpen((open) => !open)}
          >
            <span />
            <span />
            <span />
          </button>
          <div className="topbar-right">
            {hasStarted && user && repos.length > 0 && (
              <RepoPicker
                ref={repoPickerRef}
                repos={repos}
                value={selectedRepo}
                onChange={setSelectedRepo}
                variant="topbar"
                align="right"
              />
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

            {needsInstall && (
              <div className="intro-auth">
                <p className="intro-auth-copy">
                  Git Show can't see any of your repositories yet.{' '}
                  {installUrl
                    ? 'Install it on your account and pick the repositories it may use — you can change this any time.'
                    : 'Install the Git Show GitHub App on your account, then reload this page.'}
                </p>
                {installUrl && (
                  <a className="auth-btn" href={installUrl}>
                    <GitHubMark />
                    Install Git Show on your repositories
                  </a>
                )}
              </div>
            )}
          </div>

          {hasStarted && (
            <div className="messages" ref={messagesRef}>
              <div className="messages-inner">
                {messages.map((m, i) => {
                  const revealing = isStreaming && i === messages.length - 1
                  const isLatestAssistant = m.role === 'assistant' && i === messages.length - 1
                  return (
                    <div key={i} className={`bubble bubble-${m.role}`}>
                      {m.plan && m.plan.steps?.length > 0 && <PlanCard plan={m.plan} />}
                      {m.steps && m.steps.length > 0 && (
                        <ActivityLog entries={m.steps} defaultOpen={isLatestAssistant} />
                      )}
                      <div
                        className="bubble-content"
                        dangerouslySetInnerHTML={{ __html: formatMessage(m.content) }}
                      />
                      {m.pendingAction && !revealing && (
                        <ActionCard
                          action={m.pendingAction}
                          disabled={isSending || isStreaming}
                          onConfirm={() => handleConfirmAction(i, m.pendingAction)}
                          onCancel={() => handleCancelAction(i, m.pendingAction)}
                        />
                      )}
                      {m.canContinue && m.runId && !revealing && (
                        <div className="continue-card">
                          <span>Paused to keep the run in check.</span>
                          <button
                            type="button"
                            className="action-btn action-confirm"
                            onClick={() => handleContinue(i, m)}
                            disabled={isSending || isStreaming}
                          >
                            Continue
                          </button>
                        </div>
                      )}
                      {ACTION_STATE_NOTES[m.actionState] && (
                        <p className="action-resolved">{ACTION_STATE_NOTES[m.actionState]}</p>
                      )}
                    </div>
                  )
                })}
                {isSending && (
                  <div className="bubble bubble-assistant bubble-typing-bubble">
                    {livePlan && livePlan.steps?.length > 0 && <PlanCard plan={livePlan} live />}
                    {liveTimeline.length > 0 ? (
                      <Timeline entries={liveTimeline} live />
                    ) : (
                      <div className="bubble-typing">
                        <span />
                        <span />
                        <span />
                      </div>
                    )}
                    {longWait && (
                      <p className="typing-note">Still working — the agents are on it…</p>
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
            <div className={`search-field ${showInlinePicker ? 'search-field-has-picker' : ''}`} ref={searchFieldRef}>
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
                placeholder={
                  user
                    ? 'Ask your query  . . .'
                    : authChecked
                      ? 'Sign in with GitHub to ask about your repositories'
                      : 'Checking your sign-in…'
                }
                rows={hasStarted ? 1 : 3}
                disabled={!user}
              />
              {showInlinePicker && (
                <RepoPicker
                  ref={repoPickerRef}
                  repos={repos}
                  value={selectedRepo}
                  onChange={setSelectedRepo}
                  variant="inline"
                  align="left"
                />
              )}
              {authChecked && !user ? (
                <a className="auth-btn search-signin" href="/api/auth/github/login">
                  <GitHubMark />
                  Sign in
                </a>
              ) : (
              <button
                className="submit-btn"
                type="submit"
                aria-label="Ask"
                disabled={!user || isSending || isStreaming || !query.trim()}
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
              )}
            </div>

            <div className={`chips-wrap ${hasStarted ? 'intro-hidden' : ''}`}>
              <div className="chips">
                {EXAMPLE_QUERIES.map((example) => (
                  <button
                    key={example}
                    type="button"
                    className="chip"
                    onClick={() => setQuery(example)}
                    disabled={!user}
                  >
                    {example}
                  </button>
                ))}
              </div>

              <p className="hint">
                {user || !authChecked
                  ? 'Press Enter to ask, Shift-Enter for a new line'
                  : 'Sign in with GitHub to start — Git Show only sees the repositories you allow'}
              </p>
              {user && installUrl && repos.length > 0 && (
                <p className="hint">
                  Missing a repository? <a className="hint-link" href={installUrl}>Manage repository access</a>
                </p>
              )}
            </div>
          </form>
        </main>
      </div>
    </div>
  )
}

export default App
