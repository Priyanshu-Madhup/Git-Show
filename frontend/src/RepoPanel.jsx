import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { formatRelativeTime } from './time.js'
import { SLOTS, WIDGET_INFO, dockWidget, effectiveLayout, floatWidget, sendHome, useMediaQuery } from './widgetLayout.js'

const FolderIcon = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true">
    <path d="M1.75 1A1.75 1.75 0 0 0 0 2.75v10.5C0 14.216.784 15 1.75 15h12.5A1.75 1.75 0 0 0 16 13.25v-8.5A1.75 1.75 0 0 0 14.25 3H7.5a.25.25 0 0 1-.2-.1l-.9-1.2C6.07 1.26 5.55 1 5 1H1.75Z" />
  </svg>
)

const FileIcon = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true">
    <path d="M2 1.75C2 .784 2.784 0 3.75 0h6.586c.464 0 .909.184 1.237.513l2.914 2.914c.329.328.513.773.513 1.237v9.586A1.75 1.75 0 0 1 13.25 16h-9.5A1.75 1.75 0 0 1 2 14.25Zm1.75-.25a.25.25 0 0 0-.25.25v12.5c0 .138.112.25.25.25h9.5a.25.25 0 0 0 .25-.25V6h-2.75A1.75 1.75 0 0 1 9 4.25V1.5Zm6.75.062V4.25c0 .138.112.25.25.25h2.688l-.011-.013-2.914-2.914-.013-.011Z" />
  </svg>
)

const Caret = ({ open }) => (
  <svg className={`tree-caret ${open ? 'tree-caret-open' : ''}`} viewBox="0 0 16 16" width="12" height="12" fill="none" aria-hidden="true">
    <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

const ExpandIcon = () => (
  <svg viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
    <path d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5L9 7M2.5 13.5L7 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

const CloseIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M6 6L18 18M18 6L6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
  </svg>
)

// The flat path list from the index, as nested folders (folders first, then
// files, alphabetical), each folder knowing how many files it holds.
const buildTree = (entries) => {
  const root = { name: '', path: '', type: 'dir', children: [], files: 0 }
  const dirs = new Map([['', root]])
  const dirFor = (path) => {
    if (dirs.has(path)) return dirs.get(path)
    const cut = path.lastIndexOf('/')
    const node = { name: path.slice(cut + 1), path, type: 'dir', children: [], files: 0 }
    dirFor(cut >= 0 ? path.slice(0, cut) : '').children.push(node)
    dirs.set(path, node)
    return node
  }
  for (const e of entries) {
    if (e.type === 'dir') {
      dirFor(e.path)
      continue
    }
    const cut = e.path.lastIndexOf('/')
    dirFor(cut >= 0 ? e.path.slice(0, cut) : '').children.push({ name: e.path.slice(cut + 1), path: e.path, type: 'file' })
  }
  const finish = (node) => {
    node.children.sort((a, b) => (a.type === b.type ? a.name.localeCompare(b.name) : a.type === 'dir' ? -1 : 1))
    node.files = node.children.reduce((n, c) => n + (c.type === 'dir' ? finish(c) : 1), 0)
    return node.files
  }
  finish(root)
  return { root, dirPaths: [...dirs.keys()].filter(Boolean) }
}

// The panel is keyed by repository, so url is fixed for its lifetime; a
// refresh keeps what's shown until the new data arrives. A null url (a
// widget that's switched off) fetches nothing.
const useRepoData = (url, refreshKey = 0) => {
  const [state, setState] = useState({ data: null, error: null })
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    if (!url) return
    const controller = new AbortController()
    fetch(url, { credentials: 'include', signal: controller.signal })
      .then(async (res) => {
        const body = await res.json().catch(() => ({}))
        if (!res.ok) throw new Error(body.detail || 'Request failed.')
        setState({ data: body, error: null })
      })
      .catch((err) => {
        if (err.name !== 'AbortError') setState({ data: null, error: err.message })
      })
    return () => controller.abort()
  }, [url, retry, refreshKey])
  const onRetry = () => {
    setState({ data: null, error: null })
    setRetry((n) => n + 1)
  }
  return { ...state, retry: onRetry }
}

const WidgetStatus = ({ state, children }) => {
  if (state.data) return children
  if (state.error) {
    return (
      <p className="widget-note">
        {state.error}{' '}
        <button type="button" className="widget-retry" onClick={state.retry}>
          Retry
        </button>
      </p>
    )
  }
  return (
    <div className="widget-skeleton" aria-label="Loading">
      <span />
      <span />
      <span />
    </div>
  )
}

const TreeNode = ({ node, depth, expanded, onToggle, blobBase }) => {
  if (node.type === 'file') {
    return (
      <li>
        <a className="tree-row tree-file" style={{ '--depth': depth }} href={`${blobBase}/${node.path}`} target="_blank" rel="noreferrer">
          <span className="tree-caret-space" />
          <FileIcon />
          <span className="tree-name">{node.name}</span>
        </a>
      </li>
    )
  }
  const open = expanded.has(node.path)
  return (
    <li>
      <button type="button" className="tree-row tree-dir" style={{ '--depth': depth }} onClick={() => onToggle(node.path)} aria-expanded={open}>
        <Caret open={open} />
        <FolderIcon />
        <span className="tree-name">{node.name}</span>
        <span className="tree-count">{node.files}</span>
      </button>
      {open && (
        <ul className="tree-list">
          {node.children.map((c) => (
            <TreeNode key={c.path} node={c} depth={depth + 1} expanded={expanded} onToggle={onToggle} blobBase={blobBase} />
          ))}
        </ul>
      )}
    </li>
  )
}

const MAX_FILTER_RESULTS = 400

const TreeModal = ({ data, tree, onClose }) => {
  const [expanded, setExpanded] = useState(() => new Set())
  const [filter, setFilter] = useState('')
  const filterRef = useRef(null)
  const blobBase = `https://github.com/${data.repository}/blob/${data.branch}`

  useEffect(() => {
    filterRef.current?.focus()
    const onKeyDown = (e) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const matches = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return null
    return data.entries.filter((e) => e.type === 'file' && e.path.toLowerCase().includes(q))
  }, [filter, data.entries])

  const toggle = (path) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })

  return createPortal(
    <div className="tree-modal-backdrop" onClick={onClose}>
      <div
        className="tree-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`File tree of ${data.repository}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="tree-modal-head">
          <div className="tree-modal-title">
            <h2>{data.repository}</h2>
            <span className="widget-meta">
              {data.branch} · {data.commit_sha.slice(0, 7)} · {data.file_count.toLocaleString()} entries
              {data.truncated ? ' (partial)' : ''}
            </span>
          </div>
          <button type="button" className="sidebar-close" aria-label="Close file tree" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <div className="tree-modal-tools">
          <input
            ref={filterRef}
            className="tree-filter"
            type="search"
            placeholder="Filter files…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            aria-label="Filter files"
          />
          {!matches && (
            <>
              <button type="button" className="action-toggle" onClick={() => setExpanded(new Set(tree.dirPaths))}>
                Expand all
              </button>
              <button type="button" className="action-toggle" onClick={() => setExpanded(new Set())}>
                Collapse all
              </button>
            </>
          )}
        </div>
        <div className="tree-modal-body">
          {matches ? (
            matches.length ? (
              <ul className="tree-list">
                {matches.slice(0, MAX_FILTER_RESULTS).map((e) => (
                  <li key={e.path}>
                    <a className="tree-row tree-file" href={`${blobBase}/${e.path}`} target="_blank" rel="noreferrer">
                      <FileIcon />
                      <span className="tree-name">{e.path}</span>
                    </a>
                  </li>
                ))}
                {matches.length > MAX_FILTER_RESULTS && (
                  <li className="widget-note">
                    Showing {MAX_FILTER_RESULTS} of {matches.length} matches. Narrow the filter to see more.
                  </li>
                )}
              </ul>
            ) : (
              <p className="widget-note">No files match “{filter.trim()}”.</p>
            )
          ) : (
            <ul className="tree-list">
              {tree.root.children.map((c) => (
                <TreeNode key={c.path} node={c} depth={0} expanded={expanded} onToggle={toggle} blobBase={blobBase} />
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}

const TREE_PREVIEW_ROWS = 14

const TreeWidget = ({ state, handle }) => {
  const [modalOpen, setModalOpen] = useState(false)
  const tree = useMemo(() => (state.data ? buildTree(state.data.entries) : null), [state.data])
  return (
    <section className="widget widget-tree" aria-label="Project tree">
      <header className="widget-head" {...handle}>
        <h3>Project tree</h3>
        {state.data && (
          <span className="widget-meta">
            {state.data.branch} · {state.data.file_count.toLocaleString()}
          </span>
        )}
      </header>
      <WidgetStatus state={state}>
        {tree && (
          <button type="button" className="tree-preview" onClick={() => setModalOpen(true)} title="Open the full file tree">
            <span className="tree-preview-rows">
              {tree.root.children.slice(0, TREE_PREVIEW_ROWS).map((c) => (
                <span key={c.path} className="tree-row tree-preview-row">
                  {c.type === 'dir' ? <FolderIcon /> : <FileIcon />}
                  <span className="tree-name">{c.name}</span>
                  {c.type === 'dir' && <span className="tree-count">{c.files}</span>}
                </span>
              ))}
              {tree.root.children.length > TREE_PREVIEW_ROWS && (
                <span className="widget-note">+{tree.root.children.length - TREE_PREVIEW_ROWS} more</span>
              )}
            </span>
            <span className="tree-preview-open">
              <ExpandIcon />
              Open full tree
            </span>
          </button>
        )}
      </WidgetStatus>
      {modalOpen && tree && <TreeModal data={state.data} tree={tree} onClose={() => setModalOpen(false)} />}
    </section>
  )
}

const CommitsWidget = ({ state, handle }) => (
  <section className="widget widget-commits" aria-label="Recent commits">
    <header className="widget-head" {...handle}>
      <h3>Recent commits</h3>
    </header>
    <WidgetStatus state={state}>
      {state.data &&
        (state.data.commits.length ? (
          <ol className="commit-list">
            {state.data.commits.map((c) => (
              <li key={c.sha}>
                <a className="commit-row" href={c.url} target="_blank" rel="noreferrer" title={c.message}>
                  <span className="commit-message">{c.message}</span>
                  <span className="commit-meta">
                    <code>{c.sha}</code> · {c.author} · {formatRelativeTime(c.date)}
                  </span>
                </a>
              </li>
            ))}
          </ol>
        ) : (
          <p className="widget-note">No commits yet.</p>
        ))}
    </WidgetStatus>
  </section>
)

const SummaryWidget = ({ state, handle }) => {
  const d = state.data
  return (
    <section className="widget widget-summary" aria-label="About this repository">
      <header className="widget-head" {...handle}>
        <h3>About</h3>
        {d && (
          <span className="widget-meta">
            ★ {d.stars} · {d.forks} forks · {d.open_issues} open
          </span>
        )}
      </header>
      <WidgetStatus state={state}>
        {d && (
          <div className="summary-body">
            <p>{d.summary || d.description || 'No summary available.'}</p>
            {d.stack.length > 0 && (
              <ul className="summary-stack">
                {d.stack.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </WidgetStatus>
    </section>
  )
}

const BranchIcon = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true">
    <path d="M9.5 3.25a2.25 2.25 0 1 1 3 2.122V6A2.5 2.5 0 0 1 10 8.5H6a1 1 0 0 0-1 1v1.128a2.251 2.251 0 1 1-1.5 0V5.372a2.25 2.25 0 1 1 1.5 0v1.836A2.493 2.493 0 0 1 6 7h4a1 1 0 0 0 1-1v-.628A2.25 2.25 0 0 1 9.5 3.25Zm-6 0a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Zm8.25-.75a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5ZM4.25 12a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Z" />
  </svg>
)

const LockIcon = () => (
  <svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor" aria-label="Protected">
    <path d="M4 4a4 4 0 0 1 8 0v2h.25c.966 0 1.75.784 1.75 1.75v5.5A1.75 1.75 0 0 1 12.25 15h-8.5A1.75 1.75 0 0 1 2 13.25v-5.5C2 6.784 2.784 6 3.75 6H4Zm8.25 3.5h-8.5a.25.25 0 0 0-.25.25v5.5c0 .138.112.25.25.25h8.5a.25.25 0 0 0 .25-.25v-5.5a.25.25 0 0 0-.25-.25ZM10.5 6V4a2.5 2.5 0 1 0-5 0v2Z" />
  </svg>
)

const BranchesWidget = ({ state, handle }) => (
  <section className="widget widget-branches" aria-label="Branches">
    <header className="widget-head" {...handle}>
      <h3>Branches</h3>
      {state.data && <span className="widget-meta">{state.data.branches.length}</span>}
    </header>
    <WidgetStatus state={state}>
      {state.data && (
        <ul className="branch-list">
          {state.data.branches.map((b) => (
            <li key={b.name}>
              <a className="branch-row" href={b.url} target="_blank" rel="noreferrer" title={b.name}>
                <BranchIcon />
                <span className="tree-name">{b.name}</span>
                {b.protected && <LockIcon />}
                {b.default ? (
                  <span className="widget-chip">default</span>
                ) : (
                  b.ahead !== null && (
                    <span
                      className="branch-diff"
                      title={`${b.ahead} ahead of, ${b.behind} behind ${state.data.default_branch}`}
                    >
                      <span className="branch-ahead">↑{b.ahead}</span>
                      <span className="branch-behind">↓{b.behind}</span>
                    </span>
                  )
                )}
              </a>
            </li>
          ))}
        </ul>
      )}
    </WidgetStatus>
  </section>
)

const PullsWidget = ({ state, handle }) => (
  <section className="widget widget-pulls" aria-label="Open pull requests">
    <header className="widget-head" {...handle}>
      <h3>Pull requests</h3>
      {state.data && <span className="widget-meta">{state.data.pulls.length} open</span>}
    </header>
    <WidgetStatus state={state}>
      {state.data &&
        (state.data.pulls.length ? (
          <ol className="commit-list">
            {state.data.pulls.map((p) => (
              <li key={p.number}>
                <a className="commit-row pull-row" href={p.url} target="_blank" rel="noreferrer" title={p.title}>
                  <img className="widget-avatar" src={p.avatar_url} alt="" loading="lazy" />
                  <span className="pull-text">
                    <span className="commit-message">{p.title}</span>
                    <span className="commit-meta">
                      <code>#{p.number}</code> · {p.author} · {formatRelativeTime(p.updated_at)}
                      {p.draft && <span className="widget-chip widget-chip-muted">draft</span>}
                    </span>
                  </span>
                </a>
              </li>
            ))}
          </ol>
        ) : (
          <p className="widget-note">No open pull requests.</p>
        ))}
    </WidgetStatus>
  </section>
)

const ContributorsWidget = ({ state, handle }) => {
  const people = state.data?.contributors || []
  const most = Math.max(1, ...people.map((c) => c.contributions))
  return (
    <section className="widget widget-contributors" aria-label="Top contributors">
      <header className="widget-head" {...handle}>
        <h3>Contributors</h3>
      </header>
      <WidgetStatus state={state}>
        {state.data &&
          (people.length ? (
            <ol className="commit-list">
              {people.map((c) => (
                <li key={c.login}>
                  <a className="contributor-row" href={c.url} target="_blank" rel="noreferrer">
                    <img className="widget-avatar" src={c.avatar_url} alt="" loading="lazy" />
                    <span className="contributor-text">
                      <span className="contributor-line">
                        <span className="tree-name">{c.login}</span>
                        <span className="tree-count">{c.contributions.toLocaleString()}</span>
                      </span>
                      <span className="contributor-bar">
                        <span style={{ width: `${(c.contributions / most) * 100}%` }} />
                      </span>
                    </span>
                  </a>
                </li>
              ))}
            </ol>
          ) : (
            <p className="widget-note">No contributor stats yet.</p>
          ))}
      </WidgetStatus>
    </section>
  )
}

const IssuesWidget = ({ state, handle }) => (
  <section className="widget widget-issues" aria-label="Open issues">
    <header className="widget-head" {...handle}>
      <h3>Open issues</h3>
      {state.data && <span className="widget-meta">{state.data.issues.length} open</span>}
    </header>
    <WidgetStatus state={state}>
      {state.data &&
        (state.data.issues.length ? (
          <ol className="commit-list">
            {state.data.issues.map((i) => (
              <li key={i.number}>
                <a className="commit-row pull-row" href={i.url} target="_blank" rel="noreferrer" title={i.title}>
                  <img className="widget-avatar" src={i.avatar_url} alt="" loading="lazy" />
                  <span className="pull-text">
                    <span className="commit-message">{i.title}</span>
                    <span className="commit-meta">
                      <code>#{i.number}</code> · {i.author} · {formatRelativeTime(i.updated_at)}
                      {i.comments > 0 && ` · ${i.comments} comment${i.comments === 1 ? '' : 's'}`}
                    </span>
                    {i.labels.length > 0 && (
                      <span className="issue-labels">
                        {i.labels.map((lb) => (
                          <span key={lb.name} className="issue-label">
                            <span className="issue-label-dot" style={{ background: lb.color ? `#${lb.color}` : undefined }} />
                            {lb.name}
                          </span>
                        ))}
                      </span>
                    )}
                  </span>
                </a>
              </li>
            ))}
          </ol>
        ) : (
          <p className="widget-note">No open issues.</p>
        ))}
    </WidgetStatus>
  </section>
)

// A run's outcome as one word and a colour.
const ciState = (run) => {
  if (run.status !== 'completed') return { key: 'running', label: run.status === 'queued' ? 'Queued' : 'Running' }
  if (run.conclusion === 'success') return { key: 'pass', label: 'Passed' }
  if (['failure', 'timed_out', 'startup_failure'].includes(run.conclusion)) return { key: 'fail', label: 'Failed' }
  return { key: 'other', label: (run.conclusion || 'done').replace(/_/g, ' ') }
}

const CiWidget = ({ state, handle }) => {
  const runs = state.data?.workflows || []
  const failing = runs.filter((r) => ciState(r).key === 'fail').length
  return (
    <section className="widget widget-ci" aria-label="CI status">
      <header className="widget-head" {...handle}>
        <h3>CI status</h3>
        {state.data && runs.length > 0 && (
          <span className="widget-meta">{failing ? `${failing} failing` : 'All passing'}</span>
        )}
      </header>
      <WidgetStatus state={state}>
        {state.data &&
          (runs.length ? (
            <ol className="commit-list">
              {runs.map((r) => {
                const st = ciState(r)
                return (
                  <li key={r.url}>
                    <a className="branch-row" href={r.url} target="_blank" rel="noreferrer" title={`${r.name}: ${st.label}`}>
                      <span className={`ci-dot ci-${st.key}`} aria-hidden="true" />
                      <span className="tree-name">{r.name}</span>
                      <span className="commit-meta">
                        {st.label} · {formatRelativeTime(r.updated_at)}
                      </span>
                    </a>
                  </li>
                )
              })}
            </ol>
          ) : (
            <p className="widget-note">No GitHub Actions runs yet.</p>
          ))}
      </WidgetStatus>
    </section>
  )
}

const ReleaseWidget = ({ state, handle }) => {
  const r = state.data?.release
  return (
    <section className="widget widget-release" aria-label="Latest release">
      <header className="widget-head" {...handle}>
        <h3>Latest release</h3>
        {r?.prerelease && <span className="widget-chip widget-chip-muted">pre-release</span>}
      </header>
      <WidgetStatus state={state}>
        {state.data &&
          (r ? (
            <a className="release-body" href={r.url} target="_blank" rel="noreferrer">
              <span className="release-tag">{r.tag}</span>
              {r.name !== r.tag && <span className="release-name">{r.name}</span>}
              <span className="commit-meta">{r.published_at ? `Published ${formatRelativeTime(r.published_at)}` : 'Draft'}</span>
              {r.commits_since !== null && (
                <span className={`release-since ${r.commits_since > 0 ? 'release-since-ahead' : ''}`}>
                  {r.commits_since === 0
                    ? `${r.default_branch} has nothing new since`
                    : `${r.commits_since} commit${r.commits_since === 1 ? '' : 's'} on ${r.default_branch} since`}
                </span>
              )}
            </a>
          ) : (
            <p className="widget-note">No releases yet.</p>
          ))}
      </WidgetStatus>
    </section>
  )
}

const LANGUAGE_COLORS = ['#34e58f', '#5ab0ff', '#f5c451', '#ff8a7a', '#c792ea', '#7fdbca']

const LanguagesWidget = ({ state, handle }) => {
  const all = state.data?.languages || []
  // Six named, the rest as "Other".
  const shown = all.slice(0, LANGUAGE_COLORS.length)
  const rest = all.slice(LANGUAGE_COLORS.length).reduce((n, l) => n + l.percent, 0)
  const parts = [
    ...shown.map((l, i) => ({ ...l, color: LANGUAGE_COLORS[i] })),
    ...(rest > 0 ? [{ name: 'Other', percent: Math.round(rest * 10) / 10, color: 'var(--color-muted)' }] : []),
  ]
  return (
    <section className="widget widget-languages" aria-label="Languages">
      <header className="widget-head" {...handle}>
        <h3>Languages</h3>
      </header>
      <WidgetStatus state={state}>
        {state.data &&
          (parts.length ? (
            <div className="languages-body">
              <span className="languages-bar" aria-hidden="true">
                {parts.map((p) => (
                  <span key={p.name} style={{ width: `${p.percent}%`, background: p.color }} />
                ))}
              </span>
              <ul className="languages-list">
                {parts.map((p) => (
                  <li key={p.name}>
                    <span className="issue-label-dot" style={{ background: p.color }} />
                    <span className="tree-name">{p.name}</span>
                    <span className="tree-count">{p.percent < 0.1 ? '<0.1' : p.percent}%</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="widget-note">GitHub hasn’t detected any languages.</p>
          ))}
      </WidgetStatus>
    </section>
  )
}

const PLAN_AGENT_NAMES = { reader: 'Reader', writer: 'Writer', planner: 'Planner', verifier: 'Verifier' }

const PlanWidget = ({ plan, live, handle }) => {
  const done = plan ? plan.steps.filter((st) => st.status === 'done').length : 0
  return (
    <section className="widget widget-plan" aria-label="Current plan">
      <header className="widget-head" {...handle}>
        <h3>Current plan</h3>
        {plan && (
          <span className="widget-meta">
            {live ? 'Working · ' : ''}
            {done}/{plan.steps.length}
          </span>
        )}
      </header>
      {plan ? (
        <div className="plan-widget-body">
          {plan.goal && <p className="plan-widget-goal">{plan.goal}</p>}
          <ol className="plan-steps">
            {plan.steps.map((st) => {
              const status = live && st.id === plan.active_step_id && st.status === 'pending' ? 'active' : st.status
              return (
                <li key={st.id} className={`plan-step plan-step-${status}`}>
                  <span className="plan-step-marker" />
                  <span className={`agent-badge agent-${st.agent}`}>{PLAN_AGENT_NAMES[st.agent] || st.agent}</span>
                  <span className="plan-step-text">{st.instruction}</span>
                </li>
              )
            })}
          </ol>
        </div>
      ) : (
        <p className="widget-note">No plan yet. One appears here when a request takes several steps.</p>
      )}
    </section>
  )
}

const CHANGE_LABELS = {
  create_branch: 'Created branch',
  delete_branch: 'Deleted branch',
  create_or_update_file: 'Wrote',
  edit_file: 'Edited',
  restore_file: 'Restored',
  delete_file: 'Deleted',
  create_pull_request: 'Opened pull request',
  update_pull_request: 'Updated pull request',
  merge_pull_request: 'Merged pull request',
  request_pull_request_reviewers: 'Requested review on',
  submit_pull_request_review: 'Reviewed',
  create_issue: 'Opened issue',
  update_issue: 'Updated issue',
  add_issue_comment: 'Commented on',
  create_release: 'Released',
  create_repository: 'Created repository',
  fork_repository: 'Forked',
  star_repository: 'Starred',
  unstar_repository: 'Unstarred',
  add_collaborator: 'Added collaborator',
}

const describeChange = (c) => {
  const d = c.details
  const number = d.pull_number || d.issue_number
  const object = d.title || d.path || d.branch || d.tag_name || d.name || (number ? `#${number}` : '') || c.repo || ''
  return { action: CHANGE_LABELS[c.tool] || c.tool.replace(/_/g, ' '), object }
}

const ChangesWidget = ({ state, handle }) => (
  <section className="widget widget-changes" aria-label="Changes made in this chat">
    <header className="widget-head" {...handle}>
      <h3>Changes in this chat</h3>
      {state.data && state.data.changes.length > 0 && <span className="widget-meta">{state.data.changes.length}</span>}
    </header>
    <WidgetStatus state={state}>
      {state.data &&
        (state.data.changes.length ? (
          <ol className="commit-list">
            {state.data.changes.map((c, i) => {
              const { action, object } = describeChange(c)
              const body = (
                <>
                  <span className="commit-message">
                    {action} {object && <code>{object}</code>}
                  </span>
                  <span className="commit-meta">
                    {c.repo}
                    {c.at ? ` · ${formatRelativeTime(c.at)}` : ''}
                  </span>
                </>
              )
              return (
                <li key={`${c.at}-${i}`}>
                  {c.url ? (
                    <a className="commit-row" href={c.url} target="_blank" rel="noreferrer">
                      {body}
                    </a>
                  ) : (
                    <div className="commit-row">{body}</div>
                  )}
                </li>
              )
            })}
          </ol>
        ) : (
          <p className="widget-note">Nothing changed yet. Changes you confirm here will be listed.</p>
        ))}
    </WidgetStatus>
  </section>
)

const SuggestionsWidget = ({ state, onAsk, handle }) => {
  const questions = state.data?.questions || []
  return (
    <section className="widget widget-suggestions" aria-label="Suggested questions">
      <header className="widget-head" {...handle}>
        <h3>Suggested questions</h3>
      </header>
      <WidgetStatus state={state}>
        {state.data &&
          (questions.length ? (
            <ul className="suggestion-list">
              {questions.map((q) => (
                <li key={q}>
                  <button type="button" className="suggestion" onClick={() => onAsk(q)} title="Put this question in the chat box">
                    {q}
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="widget-note">No suggestions for this repository yet.</p>
          ))}
      </WidgetStatus>
    </section>
  )
}

// Relative heights of the home slots in each column.
const SLOT_FLEX = { L1: 1, L2: 1, L3: 1, R1: 1.15, R2: 1, R3: 1 }
// A dragged widget snaps to a home slot when the pointer is over the slot,
// or when at least this share of it (or of the slot, if smaller) overlaps
// the slot.
const SNAP_OVERLAP = 0.5
// How long a widget glides into (or out of) a slot.
const SNAP_GLIDE_MS = 140
// Movement below this is a click (or half a double-click), not a drag.
const DRAG_THRESHOLD = 4
// A floating widget always keeps this much of itself inside the chat area.
const KEEP_VISIBLE = 56

const clamp = (value, lo, hi) => Math.min(Math.max(value, lo), Math.max(lo, hi))

// The chat's repository panel: branches, pull requests, and contributors
// on the left; the file tree, latest commits, and what it is on the right.
//
// On wide screens each widget sits in a home slot in the side columns and
// can be dragged by its header anywhere in the chat area: near a slot it
// snaps in (swapping with whatever was there), anywhere else it floats
// where it's dropped. Double-clicking a header sends it home. `layout`
// (see widgetLayout.js) says where everything is. On narrower screens the
// widgets stack in one list opened from the top bar, and don't move.
// refreshKey changes after a confirmed write, which can move a branch or
// merge a pull request.
const RepoPanel = ({ repo, refreshKey, open, onClose, layout, setLayout, visible, chatId, plan, planLive, onAsk }) => {
  const shows = (id) => visible.includes(id)
  const url = (id, path) => (shows(id) ? `/api/repos/${repo}/${path}` : null)
  const tree = useRepoData(url('tree', 'tree'), refreshKey)
  const commits = useRepoData(url('commits', 'commits'), refreshKey)
  // About and Suggested questions come from the same model call.
  const summary = useRepoData(shows('summary') || shows('suggestions') ? `/api/repos/${repo}/summary` : null)
  const branches = useRepoData(url('branches', 'branches'), refreshKey)
  const pulls = useRepoData(url('pulls', 'pulls'), refreshKey)
  const contributors = useRepoData(url('contributors', 'contributors'))
  const issues = useRepoData(url('issues', 'issues'), refreshKey)
  const ci = useRepoData(url('ci', 'ci'), refreshKey)
  const release = useRepoData(url('release', 'release'), refreshKey)
  const languages = useRepoData(url('languages', 'languages'))
  const changes = useRepoData(shows('changes') ? `/api/conversations/${chatId}/changes` : null, refreshKey)
  const placed = effectiveLayout(layout, visible)

  const wide = useMediaQuery('(min-width: 1200px)')
  const rootRef = useRef(null)
  const [drag, setDrag] = useState(null)
  const [area, setArea] = useState(null)

  // Floating widgets are kept inside the chat area as the window resizes.
  useEffect(() => {
    const hero = rootRef.current?.closest('.hero')
    if (!hero) return
    const observer = new ResizeObserver(() => setArea({ w: hero.clientWidth, h: hero.clientHeight }))
    observer.observe(hero)
    return () => observer.disconnect()
  }, [])

  const render = {
    branches: (handle) => <BranchesWidget state={branches} handle={handle} />,
    pulls: (handle) => <PullsWidget state={pulls} handle={handle} />,
    contributors: (handle) => <ContributorsWidget state={contributors} handle={handle} />,
    tree: (handle) => <TreeWidget state={tree} handle={handle} />,
    commits: (handle) => <CommitsWidget state={commits} handle={handle} />,
    summary: (handle) => <SummaryWidget state={summary} handle={handle} />,
    issues: (handle) => <IssuesWidget state={issues} handle={handle} />,
    ci: (handle) => <CiWidget state={ci} handle={handle} />,
    release: (handle) => <ReleaseWidget state={release} handle={handle} />,
    languages: (handle) => <LanguagesWidget state={languages} handle={handle} />,
    plan: (handle) => <PlanWidget plan={plan} live={planLive} handle={handle} />,
    changes: (handle) => <ChangesWidget state={changes} handle={handle} />,
    suggestions: (handle) => <SuggestionsWidget state={summary} onAsk={onAsk} handle={handle} />,
  }

  // React renders the dragged copy once when a drag starts; after that it's
  // moved directly (a transform per animation frame), so dragging stays
  // smooth however much is on screen.
  const startDrag = (id, e) => {
    if (e.button !== 0) return
    e.preventDefault()
    const hero = e.currentTarget.closest('.hero')
    const areaRect = hero.getBoundingClientRect()
    const box = e.currentTarget.closest('.widget').getBoundingClientRect()
    const grab = { x: e.clientX - box.left, y: e.clientY - box.top }
    const startAt = { x: e.clientX, y: e.clientY }
    const slots = [...hero.querySelectorAll('[data-slot]')].map((el) => {
      const r = el.getBoundingClientRect()
      return { slot: el.dataset.slot, x: r.left - areaRect.left, y: r.top - areaRect.top, w: r.width, h: r.height }
    })
    const overlap = (x, y, r) => {
      const ow = Math.min(x + box.width, r.x + r.w) - Math.max(x, r.x)
      const oh = Math.min(y + box.height, r.y + r.h) - Math.max(y, r.y)
      return ow > 0 && oh > 0 ? (ow * oh) / Math.min(box.width * box.height, r.w * r.h) : 0
    }
    // The slot it's lifted from only joins the magnet once the widget has
    // left it, or the widget would stick there at the start of every drag.
    const origin = slots.find((r) => r.slot === e.currentTarget.closest('[data-slot]')?.dataset.slot)
    let armed = !origin
    let started = false
    let last = null
    let frame = 0
    let snappedTo = null
    let glideTimer = 0

    const paint = () => {
      frame = 0
      const el = hero.querySelector('.widget-dragging')
      if (!el || !last) return
      const slot = last.snap?.slot || null
      if (slot !== snappedTo) {
        // Glide into a slot, and back out to the pointer when leaving it.
        clearTimeout(glideTimer)
        el.classList.add('widget-dragging-snapped')
        if (!slot) glideTimer = setTimeout(() => el.classList.remove('widget-dragging-snapped'), SNAP_GLIDE_MS)
        snappedTo = slot
      }
      const r = last.snap || last
      el.style.transform = `translate3d(${r.x}px, ${r.y}px, 0)`
      el.style.width = `${r.w}px`
      el.style.height = `${r.h}px`
    }

    const onMove = (ev) => {
      if (!started) {
        if (Math.hypot(ev.clientX - startAt.x, ev.clientY - startAt.y) < DRAG_THRESHOLD) return
        started = true
        setDrag({ id, x: box.left - areaRect.left, y: box.top - areaRect.top, w: box.width, h: box.height })
      }
      const px = ev.clientX - areaRect.left
      const py = ev.clientY - areaRect.top
      const x = clamp(px - grab.x, KEEP_VISIBLE - box.width, areaRect.width - KEEP_VISIBLE)
      const y = clamp(py - grab.y, 0, areaRect.height - KEEP_VISIBLE)
      if (!armed && overlap(x, y, origin) < SNAP_OVERLAP) armed = true
      const targets = armed ? slots : slots.filter((r) => r !== origin)
      let snap = targets.find((r) => px >= r.x && px <= r.x + r.w && py >= r.y && py <= r.y + r.h) || null
      if (!snap) {
        let best = SNAP_OVERLAP
        for (const r of targets) {
          const share = overlap(x, y, r)
          if (share >= best) {
            best = share
            snap = r
          }
        }
      }
      last = { x, y, w: box.width, h: box.height, snap }
      if (!frame) frame = requestAnimationFrame(paint)
    }

    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
      cancelAnimationFrame(frame)
      clearTimeout(glideTimer)
      if (started && last) {
        const done = last
        setLayout((l) =>
          done.snap
            ? dockWidget(effectiveLayout(l, visible), id, done.snap.slot)
            : floatWidget(effectiveLayout(l, visible), id, { x: done.x, y: done.y, w: done.w, h: done.h }),
        )
      }
      setDrag(null)
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }

  const handleFor = (id) => ({
    onPointerDown: (e) => startDrag(id, e),
    onDoubleClick: () => setLayout((l) => sendHome(effectiveLayout(l, visible), id)),
    title: 'Drag to move · double-click to send back',
  })

  if (!wide) {
    return (
      <>
        <div className={`repo-panel-backdrop ${open ? 'repo-panel-backdrop-open' : ''}`} onClick={onClose} aria-hidden="true" />
        <div ref={rootRef} className={`repo-panels ${open ? 'repo-panels-open' : ''}`}>
          <div className="repo-panel-head">
            <span className="repo-panel-name" title={repo}>
              {repo}
            </span>
            <button type="button" className="sidebar-close repo-panel-close" aria-label="Close repository panel" onClick={onClose}>
              <CloseIcon />
            </button>
          </div>
          <aside className="repo-panel repo-panel-left" aria-label={`Activity in ${repo}`}>
            {WIDGET_INFO.filter((w) => w.side === 'left' && shows(w.id)).map((w) => (
              <Fragment key={w.id}>{render[w.id]()}</Fragment>
            ))}
          </aside>
          <aside className="repo-panel repo-panel-right" aria-label={`About ${repo}`}>
            {WIDGET_INFO.filter((w) => w.side === 'right' && shows(w.id)).map((w) => (
              <Fragment key={w.id}>{render[w.id]()}</Fragment>
            ))}
          </aside>
        </div>
      </>
    )
  }

  const column = (side, label) => (
    <aside className={`repo-panel repo-panel-${side}`} aria-label={label}>
      {SLOTS[side].map((slot) => {
        const id = placed.slots[slot]
        const shown = id && id !== drag?.id
        return (
          <div
            key={slot}
            data-slot={slot}
            // Empty slots are invisible, and faintly tinted while dragging
            // so the places a widget can go are visible.
            className={`widget-slot ${drag && (!id || id === drag.id) ? 'widget-slot-empty' : ''}`}
            style={{ flex: `${SLOT_FLEX[slot]} 1 0` }}
          >
            {shown && render[id](handleFor(id))}
          </div>
        )
      })}
    </aside>
  )

  return (
    <div ref={rootRef} className={`repo-panels repo-panels-movable ${drag ? 'repo-panels-dragging' : ''}`}>
      {column('left', `Activity in ${repo}`)}
      {column('right', `About ${repo}`)}
      {Object.entries(placed.free)
        .filter(([id]) => id !== drag?.id)
        .map(([id, f]) => (
          <div
            key={id}
            className="widget-float"
            style={{
              left: area ? clamp(f.x, KEEP_VISIBLE - f.w, area.w - KEEP_VISIBLE) : f.x,
              top: area ? clamp(f.y, 0, area.h - KEEP_VISIBLE) : f.y,
              width: f.w,
              height: f.h,
              zIndex: 4 + f.z,
            }}
          >
            {render[id](handleFor(id))}
          </div>
        ))}
      {drag && (
        <div
          className="widget-float widget-dragging"
          style={{ transform: `translate3d(${drag.x}px, ${drag.y}px, 0)`, width: drag.w, height: drag.h }}
          aria-hidden="true"
        >
          {render[drag.id]()}
        </div>
      )}
    </div>
  )
}

export default RepoPanel
