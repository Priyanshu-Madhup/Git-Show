import { useEffect, useRef, useState } from 'react'

const AGENTS = {
  orchestrator: { name: 'Orchestrator', role: 'Works out your goal and picks the route', color: '#b9c7bf' },
  planner: { name: 'Planning agent', role: 'Plans the steps and checks every result', color: '#c9a6ff' },
  reader: { name: 'Reader', role: 'Reads code, history, issues, and memory', color: '#7cc7ff' },
  writer: { name: 'Writer', role: 'Proposes one change at a time', color: '#ffc36b' },
  verifier: { name: 'Verifier', role: 'Re-checks GitHub after you confirm', color: '#6bf0d9' },
}

// Two layouts in their own coordinate spaces: a wide tree, and a taller one
// for phones. Node positions and edge paths are in viewBox units.
const LAYOUTS = {
  wide: {
    viewBox: [1000, 700],
    nodes: {
      orchestrator: [500, 70],
      planner: [500, 300],
      reader: [175, 470],
      writer: [825, 470],
      verifier: [825, 632],
    },
    edges: {
      'orchestrator-planner': { d: 'M500,108 L500,262', label: [512, 190, 'start'], text: 'needs a plan' },
      'orchestrator-reader': { d: 'M438,86 C300,110 190,230 178,432', label: [262, 150, 'end'], text: 'one step' },
      'orchestrator-writer': { d: 'M562,86 C700,110 810,230 822,432', label: [738, 150, 'start'], text: 'one step' },
      'planner-reader': { d: 'M430,322 C340,360 260,400 214,432', label: [300, 372, 'end'], text: 'step ⇄ result' },
      'planner-writer': { d: 'M570,322 C660,360 740,400 786,432', label: [700, 372, 'start'], text: 'step ⇄ result' },
      'writer-verifier': { d: 'M825,508 L825,594', label: [838, 556, 'start'], text: 'after you confirm' },
      'verifier-planner': { d: 'M760,640 C610,650 520,560 506,338', label: [596, 612, 'end'], text: 'result', dashed: true },
    },
  },
  tall: {
    viewBox: [400, 900],
    nodes: {
      orchestrator: [200, 60],
      planner: [200, 330],
      reader: [96, 560],
      writer: [304, 560],
      verifier: [304, 790],
    },
    edges: {
      'orchestrator-planner': { d: 'M200,98 L200,292', label: [208, 200, 'start'], text: 'needs a plan' },
      'orchestrator-reader': { d: 'M150,84 C70,140 40,330 80,522', label: [80, 168, 'end'], text: 'one step' },
      'orchestrator-writer': { d: 'M250,84 C330,140 360,330 320,522', label: [320, 168, 'start'], text: 'one step' },
      'planner-reader': { d: 'M170,368 C150,420 120,470 104,522', label: [120, 440, 'end'], text: '' },
      'planner-writer': { d: 'M230,368 C250,420 280,470 296,522', label: [280, 440, 'start'], text: '' },
      'writer-verifier': { d: 'M304,598 L304,752', label: [296, 680, 'end'], text: 'after you confirm' },
      'verifier-planner': { d: 'M254,806 C150,820 200,500 200,368', label: [150, 760, 'end'], text: 'result', dashed: true },
    },
  },
}

// One loop of a real request, as slots in an 8-second cycle. Each pulse
// travels its edge during its slot; `reverse` runs it back to the source.
const CYCLE_S = 8
const PULSES = [
  { edge: 'orchestrator-planner', from: 0.0, to: 0.11, at: 'planner', caption: 'The orchestrator hands a bigger goal to the Planning agent' },
  { edge: 'planner-reader', from: 0.13, to: 0.24, at: 'reader', caption: 'The Planning agent sends step 1 to the Reader' },
  { edge: 'planner-reader', from: 0.26, to: 0.37, at: 'planner', reverse: true, caption: 'The Reader reports back; the planner checks it against what it expected' },
  { edge: 'planner-writer', from: 0.39, to: 0.5, at: 'writer', caption: 'Next step: the Writer proposes a change, and you confirm it' },
  { edge: 'writer-verifier', from: 0.53, to: 0.63, at: 'verifier', caption: 'The Verifier re-reads GitHub to make sure the change landed' },
  { edge: 'verifier-planner', from: 0.65, to: 0.77, at: 'planner', caption: 'The result goes back to the planner, which moves on or re-plans' },
  { edge: 'orchestrator-reader', from: 0.8, to: 0.94, at: 'reader', caption: 'Simple requests skip the plan: straight to the Reader or the Writer' },
  { edge: 'orchestrator-writer', from: 0.8, to: 0.94, at: 'writer', caption: 'Simple requests skip the plan: straight to the Reader or the Writer' },
]

const EDGE_COLOR = (edgeId) => AGENTS[edgeId.split('-')[1]].color

function pulseTiming({ from, to }) {
  const e = 0.004
  return {
    keyTimes: `0;${from};${to};1`,
    opacity: { values: '0;0;1;1;0;0', keyTimes: `0;${from};${from + e};${to - e};${to};1` },
  }
}

export default function AgentTree({ reducedMotion }) {
  const rootRef = useRef(null)
  const svgRef = useRef(null)
  const [visible, setVisible] = useState(reducedMotion)
  const [layoutName, setLayoutName] = useState(() => (window.innerWidth < 760 ? 'tall' : 'wide'))
  const [slot, setSlot] = useState(-1)
  const layout = LAYOUTS[layoutName]
  const [vw, vh] = layout.viewBox

  useEffect(() => {
    const media = window.matchMedia('(max-width: 759px)')
    const onChange = () => setLayoutName(media.matches ? 'tall' : 'wide')
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [])

  useEffect(() => {
    if (visible) return
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true)
          observer.disconnect()
        }
      },
      { threshold: 0.3 },
    )
    observer.observe(rootRef.current)
    return () => observer.disconnect()
  }, [visible])

  // Follow the SVG's own animation clock, so the highlighted agent and the
  // caption always match where the pulse actually is.
  useEffect(() => {
    if (!visible || reducedMotion) return
    const timer = setInterval(() => {
      const t = svgRef.current?.getCurrentTime?.()
      if (t == null) return
      const phase = ((t - 2) / CYCLE_S) % 1
      if (t < 2) return setSlot(-1)
      setSlot(PULSES.findIndex((p) => phase >= p.from && phase <= p.to + 0.02))
    }, 120)
    return () => clearInterval(timer)
  }, [visible, reducedMotion])

  const current = slot >= 0 ? PULSES[slot] : null
  const activeEdges = new Set(current ? PULSES.filter((p) => p.from === current.from).map((p) => p.edge) : [])
  const activeAgents = new Set(current ? PULSES.filter((p) => p.from === current.from).map((p) => p.at) : [])
  const order = ['orchestrator', 'planner', 'reader', 'writer', 'verifier']

  return (
    <div className={`agent-tree ${visible ? 'is-visible' : ''} ${reducedMotion ? 'is-still' : ''}`} ref={rootRef}>
      <div className="agent-tree-stage" style={{ aspectRatio: `${vw} / ${vh}` }}>
        <svg ref={svgRef} className="agent-tree-edges" viewBox={`0 0 ${vw} ${vh}`} aria-hidden="true">
          <defs>
            <filter id="pulse-glow" x="-200%" y="-200%" width="500%" height="500%">
              <feGaussianBlur stdDeviation="5" />
            </filter>
          </defs>
          {Object.entries(layout.edges).map(([id, edge], i) => (
            <g key={id} className={`agent-edge ${activeEdges.has(id) ? 'is-active' : ''}`}>
              <path
                id={`edge-${layoutName}-${id}`}
                d={edge.d}
                pathLength="1"
                className={`agent-edge-line ${edge.dashed ? 'is-dashed' : ''}`}
                style={{ stroke: EDGE_COLOR(id), '--delay': `${0.5 + i * 0.12}s` }}
              />
              {edge.text && (
                <text
                  x={edge.label[0]}
                  y={edge.label[1]}
                  textAnchor={edge.label[2]}
                  className="agent-edge-label"
                  style={{ '--delay': `${1.3 + i * 0.08}s` }}
                >
                  {edge.text}
                </text>
              )}
            </g>
          ))}
          {visible &&
            !reducedMotion &&
            PULSES.map((p, i) => {
              const timing = pulseTiming(p)
              const color = EDGE_COLOR(p.edge)
              const keyPoints = p.reverse ? '1;1;0;0' : '0;0;1;1'
              return (
                <g key={`${layoutName}-${i}`}>
                  {[
                    { r: 11, className: 'agent-pulse-glow', filter: 'url(#pulse-glow)' },
                    { r: 4.5, className: 'agent-pulse-core' },
                  ].map((dot) => (
                    <circle key={dot.r} r={dot.r} fill={color} className={dot.className} filter={dot.filter} opacity="0">
                      <animateMotion
                        dur={`${CYCLE_S}s`}
                        begin="2s"
                        repeatCount="indefinite"
                        keyPoints={keyPoints}
                        keyTimes={timing.keyTimes}
                        calcMode="linear"
                      >
                        <mpath href={`#edge-${layoutName}-${p.edge}`} />
                      </animateMotion>
                      <animate
                        attributeName="opacity"
                        dur={`${CYCLE_S}s`}
                        begin="2s"
                        repeatCount="indefinite"
                        values={timing.opacity.values}
                        keyTimes={timing.opacity.keyTimes}
                      />
                    </circle>
                  ))}
                </g>
              )
            })}
        </svg>

        {order.map((key, i) => {
          const agent = AGENTS[key]
          const [x, y] = layout.nodes[key]
          return (
            <div
              key={key}
              className={`agent-node agent-node-${key} ${activeAgents.has(key) ? 'is-active' : ''}`}
              style={{
                left: `${(x / vw) * 100}%`,
                top: `${(y / vh) * 100}%`,
                '--agent': agent.color,
                '--delay': `${i * 0.12}s`,
              }}
            >
              <span className="agent-node-name">{agent.name}</span>
              <span className="agent-node-role">{agent.role}</span>
            </div>
          )
        })}
      </div>

      <p className="agent-tree-caption" aria-live="off">
        {reducedMotion
          ? 'Simple requests go straight to the Reader or the Writer; bigger goals go through the Planning agent, and every confirmed change is re-checked by the Verifier.'
          : current?.caption || 'Follow a request through Git Show'}
      </p>
    </div>
  )
}
