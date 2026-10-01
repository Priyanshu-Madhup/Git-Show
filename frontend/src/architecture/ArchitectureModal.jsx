import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Background, Controls, Handle, MarkerType, MiniMap, ReactFlow } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { formatRelativeTime } from '../time.js'
import { fitZoom, layoutGraph } from './layout.js'
import './Architecture.css'

const KINDS = {
  frontend: { label: 'Frontend', color: '#8bffce' },
  backend: { label: 'Backend', color: '#34e58f' },
  service: { label: 'Service', color: '#34e58f' },
  agent: { label: 'Agent', color: '#c9a6ff' },
  worker: { label: 'Worker', color: '#9ad8ff' },
  data: { label: 'Data', color: '#ffd27a' },
  storage: { label: 'Storage', color: '#ffd27a' },
  external: { label: 'External', color: '#ff9f9f' },
  infra: { label: 'Infra', color: '#7fc4e0' },
  library: { label: 'Library', color: '#7fe0d0' },
  config: { label: 'Config', color: '#93a39a' },
  test: { label: 'Tests', color: '#93a39a' },
  other: { label: 'Other', color: '#93a39a' },
}
const kindOf = (kind) => KINDS[kind] || KINDS.other

const COMPONENT_SIZE = { width: 248, height: 132 }
const MODULE_SIZE = { width: 220, height: 74 }
const POLL_MS = 4000

const fetchJson = async (url, options) => {
  const res = await fetch(url, { credentials: 'include', ...options })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`)
  return data
}

const CloseIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M6 6L18 18M18 6L6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
  </svg>
)

const ComponentNode = ({ data, sourcePosition, targetPosition }) => {
  const c = data.component
  const kind = kindOf(c.kind)
  return (
    <div
      className={`arch-node ${data.selected ? 'arch-node-selected' : ''} ${data.dim ? 'arch-node-dim' : ''}`}
      style={{ '--kind': kind.color }}
    >
      <Handle type="target" position={targetPosition} className="arch-handle" />
      <span className="arch-node-kind">{kind.label}</span>
      <strong className="arch-node-name">{c.name}</strong>
      <span className="arch-node-meta">
        {c.metrics?.files ? `${c.metrics.files} files · ${c.metrics.lines.toLocaleString()} lines` : c.paths.length ? 'Config and assets' : 'Outside the repository'}
      </span>
      {c.tech.length > 0 && (
        <span className="arch-node-tech">
          {c.tech.slice(0, 3).map((t) => (
            <span key={t} className="arch-chip">
              {t}
            </span>
          ))}
        </span>
      )}
      <Handle type="source" position={sourcePosition} className="arch-handle" />
    </div>
  )
}

const ModuleNode = ({ data, sourcePosition, targetPosition }) => {
  const m = data.module
  return (
    <div
      className={`arch-node arch-node-module ${data.selected ? 'arch-node-selected' : ''} ${data.dim ? 'arch-node-dim' : ''}`}
      style={{ '--kind': data.color }}
    >
      <Handle type="target" position={targetPosition} className="arch-handle" />
      <strong className="arch-node-name arch-mono">{m.id === '(root)' ? '(root)' : `${m.id}/`}</strong>
      <span className="arch-node-meta">
        {m.files} files · {m.lines.toLocaleString()} lines{data.componentName ? ` · ${data.componentName}` : ''}
      </span>
      <Handle type="source" position={sourcePosition} className="arch-handle" />
    </div>
  )
}

const nodeTypes = { component: ComponentNode, module: ModuleNode }

// Tests and config are real parts of a repo but not of its architecture;
// they import nearly everything, so they're dropped from the diagram
// entirely rather than shown or offered as a toggle.
const AUX_KINDS = new Set(['test', 'config'])
const isAux = (component) => !!component && AUX_KINDS.has(component.kind)

// Strips test/config components, the modules they own, and anything
// connecting to them, once, so every view and every list downstream is
// already clean.
const stripAux = (arch) => {
  const auxIds = new Set(arch.components.filter(isAux).map((c) => c.id))
  if (auxIds.size === 0) return arch
  const moduleIds = new Set(arch.modules.nodes.filter((m) => !auxIds.has(m.component)).map((m) => m.id))
  return {
    ...arch,
    components: arch.components.filter((c) => !auxIds.has(c.id)),
    connections: arch.connections.filter((e) => !auxIds.has(e.source) && !auxIds.has(e.target)),
    modules: {
      nodes: arch.modules.nodes.filter((m) => moduleIds.has(m.id)),
      edges: arch.modules.edges.filter((e) => moduleIds.has(e.source) && moduleIds.has(e.target)),
    },
  }
}

// Nodes and edges for the chosen view, before layout.
const rawGraph = (arch, view) => {
  if (view === 'components') {
    return {
      nodes: arch.components.map((c) => ({ id: c.id, type: 'component', data: { component: c }, ...COMPONENT_SIZE })),
      edges: arch.connections.map((e) => ({ ...e, text: e.label })),
      size: COMPONENT_SIZE,
    }
  }
  const components = new Map(arch.components.map((c) => [c.id, c]))
  const modules = arch.modules.nodes
  const ids = new Set(modules.map((m) => m.id))
  return {
    nodes: modules.map((m) => {
      const owner = components.get(m.component)
      return {
        id: m.id,
        type: 'module',
        data: { module: m, color: kindOf(owner?.kind).color, componentName: owner?.name },
        ...MODULE_SIZE,
      }
    }),
    edges: arch.modules.edges
      .filter((e) => ids.has(e.source) && ids.has(e.target))
      .map((e) => ({
        id: `${e.source}->${e.target}`,
        source: e.source,
        target: e.target,
        kind: 'imports',
        text: `${e.count} import${e.count === 1 ? '' : 's'}`,
        alwaysLabelled: true,
      })),
    size: MODULE_SIZE,
  }
}

// Laid out from the model's connections only: edges filled in from the
// import graph would otherwise pull the ranking out of shape.
const layoutEdges = (raw) => raw.edges.filter((e) => !e.inferred)

// The laid-out graph, styled around the focused node (hovered, else
// selected): its edges are highlighted and labelled, everything unrelated
// is dimmed, and inferred edges only appear when they touch it. With no
// focus, the diagram is just boxes and arrows.
const decorate = (raw, placed, selected, focus) => {
  const neighbours = new Set(focus ? [focus] : [])
  raw.edges.forEach((e) => {
    if (e.source === focus) neighbours.add(e.target)
    if (e.target === focus) neighbours.add(e.source)
  })
  const nodes = placed.map((n) => ({
    ...n,
    data: { ...n.data, selected: n.id === selected, dim: !!focus && !neighbours.has(n.id) },
  }))
  const edges = raw.edges.map((e) => {
    const touches = !!focus && (e.source === focus || e.target === focus)
    const active = !focus || touches
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      // Short on the canvas; the full label is in the details panel.
      label: touches || e.alwaysLabelled ? (e.text.length > 34 ? `${e.text.slice(0, 33).trimEnd()}…` : e.text) : undefined,
      labelBgPadding: [6, 3],
      labelBgBorderRadius: 6,
      hidden: e.inferred && !touches,
      // Every arrow flows; the focused node's flow faster (speeds are in the CSS).
      animated: true,
      className: `arch-edge arch-edge-${e.kind} ${e.inferred ? 'arch-edge-inferred' : ''} ${active ? '' : 'arch-edge-dim'} ${touches ? 'arch-edge-focus' : ''}`,
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: touches ? '#8bffce' : active ? '#34e58f' : 'rgba(120, 220, 170, 0.2)' },
    }
  })
  return { nodes, edges }
}

const Overview = ({ arch, onSelect }) => (
  <>
    <p className="arch-summary">{arch.summary}</p>
    <dl className="arch-stats">
      <div>
        <dt>Source files</dt>
        <dd>{arch.stats.source_files.toLocaleString()}</dd>
      </div>
      <div>
        <dt>Lines</dt>
        <dd>{arch.stats.lines.toLocaleString()}</dd>
      </div>
      <div>
        <dt>Functions &amp; classes</dt>
        <dd>{arch.stats.symbols.toLocaleString()}</dd>
      </div>
      <div>
        <dt>Internal imports</dt>
        <dd>{arch.stats.internal_imports.toLocaleString()}</dd>
      </div>
    </dl>
    {arch.flows.length > 0 && (
      <section className="arch-section">
        <h3>Key flows</h3>
        {arch.flows.map((f) => (
          <div key={f.name} className="arch-flow">
            <h4>{f.name}</h4>
            <ol>
              {f.steps.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          </div>
        ))}
      </section>
    )}
    <section className="arch-section">
      <h3>Components</h3>
      <ul className="arch-link-list">
        {arch.components.map((c) => (
          <li key={c.id}>
            <button type="button" className="arch-link" onClick={() => onSelect(c.id)}>
              <span className="arch-dot" style={{ '--kind': kindOf(c.kind).color }} />
              {c.name}
            </button>
          </li>
        ))}
      </ul>
    </section>
    {arch.external.length > 0 && (
      <section className="arch-section">
        <h3>External packages</h3>
        <div className="arch-chips">
          {arch.external.map((p) => (
            <span key={p.name} className="arch-chip" title={`Imported by ${p.uses} file${p.uses === 1 ? '' : 's'}`}>
              {p.name}
            </span>
          ))}
        </div>
      </section>
    )}
  </>
)

const ComponentDetails = ({ arch, component: c, treeUrl, onSelect }) => {
  const names = new Map(arch.components.map((x) => [x.id, x.name]))
  const outgoing = arch.connections.filter((e) => e.source === c.id)
  const incoming = arch.connections.filter((e) => e.target === c.id)
  const kind = kindOf(c.kind)
  return (
    <>
      <span className="arch-kind-badge" style={{ '--kind': kind.color }}>
        {kind.label}
      </span>
      <h3 className="arch-detail-title">{c.name}</h3>
      <p className="arch-summary">{c.description}</p>
      {c.metrics.files > 0 && (
        <p className="arch-muted">
          {c.metrics.files} files · {c.metrics.lines.toLocaleString()} lines · {c.metrics.symbols} functions &amp; classes
        </p>
      )}
      {c.responsibilities.length > 0 && (
        <section className="arch-section">
          <h3>Responsibilities</h3>
          <ul className="arch-bullets">
            {c.responsibilities.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </section>
      )}
      {c.tech.length > 0 && (
        <section className="arch-section">
          <h3>Tech</h3>
          <div className="arch-chips">
            {c.tech.map((t) => (
              <span key={t} className="arch-chip">
                {t}
              </span>
            ))}
          </div>
        </section>
      )}
      {(outgoing.length > 0 || incoming.length > 0) && (
        <section className="arch-section">
          <h3>Connections</h3>
          <ul className="arch-link-list">
            {outgoing.map((e) => (
              <li key={e.id}>
                <button type="button" className="arch-link" onClick={() => onSelect(e.target)}>
                  <span className="arch-arrow">→</span>
                  <span>
                    <strong>{names.get(e.target)}</strong>
                    <span className="arch-muted"> · {e.label}</span>
                  </span>
                </button>
              </li>
            ))}
            {incoming.map((e) => (
              <li key={e.id}>
                <button type="button" className="arch-link" onClick={() => onSelect(e.source)}>
                  <span className="arch-arrow">←</span>
                  <span>
                    <strong>{names.get(e.source)}</strong>
                    <span className="arch-muted"> · {e.label}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
      {c.paths.length > 0 && (
        <section className="arch-section">
          <h3>Where it lives</h3>
          <ul className="arch-paths">
            {c.paths.map((p) => (
              <li key={p}>
                <a href={`${treeUrl}/${p}`} target="_blank" rel="noreferrer" className="arch-mono">
                  {p}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  )
}

const ModuleDetails = ({ arch, module: m, treeUrl, onSelect }) => {
  const owner = arch.components.find((c) => c.id === m.component)
  const out = arch.modules.edges.filter((e) => e.source === m.id)
  const into = arch.modules.edges.filter((e) => e.target === m.id)
  return (
    <>
      <h3 className="arch-detail-title arch-mono">{m.id}/</h3>
      <p className="arch-muted">
        {m.files} files · {m.lines.toLocaleString()} lines · {m.symbols} functions &amp; classes
      </p>
      {owner && (
        <p className="arch-muted">
          Part of <strong className="arch-white">{owner.name}</strong>
        </p>
      )}
      {(out.length > 0 || into.length > 0) && (
        <section className="arch-section">
          <h3>Imports</h3>
          <ul className="arch-link-list">
            {out.map((e) => (
              <li key={`o-${e.target}`}>
                <button type="button" className="arch-link" onClick={() => onSelect(e.target)}>
                  <span className="arch-arrow">→</span>
                  <span className="arch-mono">{e.target}/</span>
                  <span className="arch-muted"> · {e.count}</span>
                </button>
              </li>
            ))}
            {into.map((e) => (
              <li key={`i-${e.source}`}>
                <button type="button" className="arch-link" onClick={() => onSelect(e.source)}>
                  <span className="arch-arrow">←</span>
                  <span className="arch-mono">{e.source}/</span>
                  <span className="arch-muted"> · {e.count}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className="arch-section">
        <h3>Files</h3>
        <ul className="arch-paths">
          {m.paths.map((p) => (
            <li key={p}>
              <a href={`${treeUrl.replace('/tree/', '/blob/')}/${p}`} target="_blank" rel="noreferrer" className="arch-mono">
                {p}
              </a>
            </li>
          ))}
        </ul>
      </section>
    </>
  )
}

const Progress = ({ repo }) => (
  <div className="arch-state">
    <span className="arch-spinner" aria-hidden="true" />
    <h3>Analyzing {repo}</h3>
    <p>
      Reading every source file&rsquo;s structure and its real import graph, then mapping the components and how they
      connect. This usually takes about a minute. You can close this window; the result is saved and will be here when
      you come back.
    </p>
  </div>
)

const ArchitectureModal = ({ repo, onClose }) => {
  const queryClient = useQueryClient()
  const key = useMemo(() => ['architecture', repo.toLowerCase()], [repo])
  const url = `/api/repos/${repo}/architecture`
  const [view, setView] = useState('components')
  // null: pick whichever direction fits the canvas at the larger zoom.
  const [directionChoice, setDirectionChoice] = useState(null)
  const [selected, setSelected] = useState(null)
  const [hovered, setHovered] = useState(null)
  const [canvasSize, setCanvasSize] = useState(null)
  const autoStarted = useRef(false)
  const flow = useRef(null)
  const resizeObserver = useRef(null)

  // Measure the canvas (for choosing a direction), and refit the diagram
  // whenever the window changes size.
  const canvasRef = useCallback((el) => {
    resizeObserver.current?.disconnect()
    if (!el) return
    resizeObserver.current = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect
      setCanvasSize((prev) => (prev && prev.width === width && prev.height === height ? prev : { width, height }))
      requestAnimationFrame(() => flow.current?.fitView({ padding: 0.12, maxZoom: 1.1 }))
    })
    resizeObserver.current.observe(el)
  }, [])
  useEffect(() => () => resizeObserver.current?.disconnect(), [])

  // Served from the session cache after the first open (staleTime is
  // Infinity); polls only while an analysis is running.
  const query = useQuery({
    queryKey: key,
    queryFn: () => fetchJson(url),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? POLL_MS : false),
  })
  const analyze = useMutation({
    mutationFn: (force) =>
      fetchJson(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force }),
      }),
    onSuccess: (data) => queryClient.setQueryData(key, data),
  })

  const data = query.data
  const arch = useMemo(() => (data?.architecture ? stripAux(data.architecture) : null), [data])
  const startAnalysis = analyze.mutate

  useEffect(() => {
    if (data?.status === 'none' && !autoStarted.current) {
      autoStarted.current = true
      startAnalysis(false)
    }
  }, [data?.status, startAnalysis])

  useEffect(() => {
    const onKeyDown = (e) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const raw = useMemo(() => (arch ? rawGraph(arch, view) : null), [arch, view])
  const layouts = useMemo(() => {
    if (!raw) return null
    const options = { nodeWidth: raw.size.width, nodeHeight: raw.size.height }
    const edges = layoutEdges(raw)
    return {
      LR: layoutGraph(raw.nodes, edges, { ...options, direction: 'LR' }),
      TB: layoutGraph(raw.nodes, edges, { ...options, direction: 'TB' }),
    }
  }, [raw])
  const autoDirection =
    layouts && canvasSize && fitZoom(layouts.LR, canvasSize.width, canvasSize.height) < fitZoom(layouts.TB, canvasSize.width, canvasSize.height)
      ? 'TB'
      : 'LR'
  const direction = directionChoice ?? autoDirection
  const focus = hovered ?? selected
  const graph = useMemo(
    () => (layouts ? decorate(raw, layouts[direction], selected, focus) : null),
    [raw, layouts, direction, selected, focus],
  )
  const switchView = (next) => {
    setView(next)
    setSelected(null)
    setHovered(null)
  }
  // From the overview or a connection link.
  const selectComponent = (id) => {
    setView('components')
    setSelected(id)
  }

  const running = data?.status === 'running' || analyze.isPending
  const failed = data?.status === 'failed'
  const treeUrl = arch ? `https://github.com/${arch.repository}/tree/${arch.branch}` : ''
  const selectedComponent = view === 'components' && arch?.components.find((c) => c.id === selected)
  const selectedModule = view === 'modules' && arch?.modules.nodes.find((m) => m.id === selected)

  let body
  if (query.isPending) {
    body = (
      <div className="arch-state">
        <span className="arch-spinner" aria-hidden="true" />
        <p>Loading…</p>
      </div>
    )
  } else if (query.isError) {
    body = (
      <div className="arch-state">
        <h3>Couldn&rsquo;t load the architecture</h3>
        <p>{query.error.message}</p>
        <button type="button" className="arch-btn" onClick={() => query.refetch()}>
          Try again
        </button>
      </div>
    )
  } else if (!arch) {
    body = failed ? (
      <div className="arch-state">
        <h3>The analysis didn&rsquo;t finish</h3>
        <p>{data.error}</p>
        <button type="button" className="arch-btn" onClick={() => startAnalysis(true)} disabled={running}>
          Try again
        </button>
      </div>
    ) : analyze.isError ? (
      <div className="arch-state">
        <h3>Couldn&rsquo;t start the analysis</h3>
        <p>{analyze.error.message}</p>
        <button type="button" className="arch-btn" onClick={() => startAnalysis(false)}>
          Try again
        </button>
      </div>
    ) : (
      <Progress repo={repo} />
    )
  } else {
    body = (
      <div className="arch-body">
        <div className="arch-canvas" ref={canvasRef}>
          <ReactFlow
            key={`${view}-${direction}`}
            nodes={graph.nodes}
            edges={graph.edges}
            nodeTypes={nodeTypes}
            colorMode="dark"
            onInit={(instance) => {
              flow.current = instance
            }}
            fitView
            fitViewOptions={{ padding: 0.12, maxZoom: 1.1 }}
            minZoom={0.15}
            maxZoom={2}
            nodesConnectable={false}
            edgesFocusable={false}
            onNodeClick={(_, node) => setSelected((cur) => (cur === node.id ? null : node.id))}
            onNodeMouseEnter={(_, node) => setHovered(node.id)}
            onNodeMouseLeave={() => setHovered(null)}
            onPaneClick={() => setSelected(null)}
            proOptions={{ hideAttribution: true }}
          >
            {!focus && <div className="arch-hint">Hover or click a box to see what it connects to</div>}
            <Background gap={22} size={1.2} color="rgba(120, 220, 170, 0.16)" />
            <Controls showInteractive={false} />
            <MiniMap
              pannable
              zoomable
              style={{ width: 168, height: 112 }}
              nodeColor={(n) => (n.type === 'module' ? n.data.color : kindOf(n.data.component?.kind).color)}
              nodeStrokeWidth={0}
              maskColor="rgba(2, 4, 3, 0.72)"
            />
          </ReactFlow>
        </div>
        <aside className="arch-panel">
          {selectedComponent ? (
            <ComponentDetails arch={arch} component={selectedComponent} treeUrl={treeUrl} onSelect={selectComponent} />
          ) : selectedModule ? (
            <ModuleDetails arch={arch} module={selectedModule} treeUrl={treeUrl} onSelect={setSelected} />
          ) : (
            <Overview arch={arch} onSelect={selectComponent} />
          )}
          <p className="arch-foot">
            Analyzed {formatRelativeTime(arch.generated_at)} from {arch.branch} @ {arch.commit_sha.slice(0, 7)}
          </p>
        </aside>
      </div>
    )
  }

  return createPortal(
    <div className="tree-modal-backdrop" onClick={onClose}>
      <div className="arch-modal" role="dialog" aria-modal="true" aria-label={`Architecture of ${repo}`} onClick={(e) => e.stopPropagation()}>
        <div className="tree-modal-head">
          <div className="tree-modal-title">
            <h2>Architecture</h2>
            <span className="widget-meta">
              {repo}
              {arch ? ` · ${arch.components.length} components · ${arch.connections.length} connections` : ''}
            </span>
          </div>
          <div className="arch-head-tools">
            {arch && (
              <>
                <div className="arch-tabs" role="tablist" aria-label="View">
                  <button type="button" role="tab" aria-selected={view === 'components'} onClick={() => switchView('components')}>
                    Components
                  </button>
                  <button type="button" role="tab" aria-selected={view === 'modules'} onClick={() => switchView('modules')}>
                    Modules
                  </button>
                </div>
                <div className="arch-tabs" role="group" aria-label="Layout direction">
                  <button
                    type="button"
                    aria-pressed={direction === 'LR'}
                    onClick={() => setDirectionChoice('LR')}
                    title="Lay the diagram out left to right"
                  >
                    Horizontal
                  </button>
                  <button
                    type="button"
                    aria-pressed={direction === 'TB'}
                    onClick={() => setDirectionChoice('TB')}
                    title="Lay the diagram out top to bottom"
                  >
                    Vertical
                  </button>
                </div>
                <button
                  type="button"
                  className="arch-pill"
                  onClick={() => startAnalysis(true)}
                  disabled={running}
                  title="Run the analysis again on the latest commit"
                >
                  <svg
                    className={running ? 'arch-pill-spin' : ''}
                    width="13"
                    height="13"
                    viewBox="0 0 24 24"
                    fill="none"
                    aria-hidden="true"
                  >
                    <path
                      d="M20 12a8 8 0 1 1-2.34-5.66M20 4v5h-5"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                  {running ? 'Analyzing…' : 'Re-analyze'}
                </button>
              </>
            )}
            <button type="button" className="sidebar-close" aria-label="Close architecture" onClick={onClose}>
              <CloseIcon />
            </button>
          </div>
        </div>
        {arch && running && (
          <div className="arch-banner">
            <span className="arch-spinner arch-spinner-small" aria-hidden="true" />
            Re-analyzing at the latest commit. The diagram below is the previous analysis.
          </div>
        )}
        {arch && !running && data.stale && (
          <div className="arch-banner arch-banner-warn">
            The repository has moved on since this analysis ({arch.commit_sha.slice(0, 7)} →{' '}
            {data.current_commit.slice(0, 7)}).
            <button type="button" className="action-toggle" onClick={() => startAnalysis(true)}>
              Re-analyze
            </button>
          </div>
        )}
        {arch && !running && failed && (
          <div className="arch-banner arch-banner-warn">The last re-analysis failed: {data.error} Showing the previous result.</div>
        )}
        {body}
      </div>
    </div>,
    document.body,
  )
}

export default ArchitectureModal
