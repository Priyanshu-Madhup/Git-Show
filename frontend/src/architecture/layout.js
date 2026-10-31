import dagre from '@dagrejs/dagre'

// Places nodes with dagre (a layered graph layout), so arrows mostly run one
// way: from the user-facing parts toward data stores and external systems.
export const layoutGraph = (nodes, edges, { direction = 'LR', nodeWidth, nodeHeight }) => {
  const g = new dagre.graphlib.Graph({ multigraph: true })
  g.setGraph({ rankdir: direction, nodesep: 36, ranksep: direction === 'LR' ? 90 : 64, marginx: 16, marginy: 16 })
  g.setDefaultEdgeLabel(() => ({}))
  nodes.forEach((n) => g.setNode(n.id, { width: n.width ?? nodeWidth, height: n.height ?? nodeHeight }))
  edges.forEach((e) => {
    if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target, {}, e.id)
  })
  dagre.layout(g)
  const horizontal = direction === 'LR'
  return nodes.map((n) => {
    const { x, y, width, height } = g.node(n.id)
    return {
      ...n,
      // dagre gives centers; React Flow wants the top-left corner.
      position: { x: x - width / 2, y: y - height / 2 },
      sourcePosition: horizontal ? 'right' : 'bottom',
      targetPosition: horizontal ? 'left' : 'top',
    }
  })
}

// The zoom at which a laid-out graph would fit a width × height canvas.
export const fitZoom = (placed, width, height) => {
  let right = 0
  let bottom = 0
  placed.forEach((n) => {
    right = Math.max(right, n.position.x + n.width)
    bottom = Math.max(bottom, n.position.y + n.height)
  })
  return Math.min(width / (right || 1), height / (bottom || 1))
}

// The layers view: components sit in labelled boxes by layer, laid out like a
// system poster. Frontend, backend and AI services across the top, data stores
// in the middle, other external services along the bottom. Returns the group
// boxes first (React Flow wants parents before children), then the components
// positioned inside them. Each node also carries `absolute`, its position on
// the canvas, for choosing which side an arrow leaves from.
export const LAYER_GROUPS = {
  frontend: { label: 'Frontend', color: '#7fc4ff' },
  backend: { label: 'Backend', color: '#34e58f' },
  ai: { label: 'AI model services', color: '#c9a6ff' },
  data: { label: 'Data & storage', color: '#ffd27a' },
  external: { label: 'External services', color: '#ff9f9f' },
}
const LAYER_ROWS = [['frontend', 'backend', 'ai'], ['data'], ['external']]
const KIND_GROUP = { frontend: 'frontend', data: 'data', storage: 'data', external: 'external' }

export const groupOf = (component) => component.group || KIND_GROUP[component.kind] || 'backend'

export const layoutLayers = (nodes, { nodeWidth, nodeHeight }) => {
  const PAD = 20
  const HEADER = 46
  const GAP = 20
  const GROUP_GAP = 64
  const ROW_GAP = 96

  const members = {}
  nodes.forEach((n) => {
    const g = groupOf(n.data.component)
    ;(members[g] ||= []).push(n)
  })

  const rows = LAYER_ROWS.map((ids, rowIndex) =>
    ids
      .filter((id) => members[id]?.length)
      .map((id) => {
        const count = members[id].length
        // Narrow columns on the top row, wide shelves below.
        const cols = rowIndex === 0 ? (count > 3 ? 2 : 1) : Math.min(count, 4)
        const lines = Math.ceil(count / cols)
        return {
          id,
          cols,
          width: PAD * 2 + cols * nodeWidth + (cols - 1) * GAP,
          height: HEADER + PAD + lines * nodeHeight + (lines - 1) * GAP + PAD,
        }
      }),
  ).filter((row) => row.length)

  const rowWidths = rows.map((row) => row.reduce((sum, g) => sum + g.width, 0) + (row.length - 1) * GROUP_GAP)
  const widest = Math.max(...rowWidths, 1)
  const groups = []
  const children = []
  let y = 0
  rows.forEach((row, r) => {
    const rowHeight = Math.max(...row.map((g) => g.height))
    let x = (widest - rowWidths[r]) / 2
    row.forEach((g) => {
      groups.push({
        id: `group-${g.id}`,
        type: 'group',
        position: { x, y },
        width: g.width,
        height: rowHeight,
        data: { label: LAYER_GROUPS[g.id].label, color: LAYER_GROUPS[g.id].color, count: members[g.id].length },
        selectable: false,
        draggable: false,
        focusable: false,
        zIndex: -1,
      })
      members[g.id].forEach((n, i) => {
        const local = {
          x: PAD + (i % g.cols) * (nodeWidth + GAP),
          y: HEADER + (Math.floor(i / g.cols)) * (nodeHeight + GAP),
        }
        children.push({
          ...n,
          width: nodeWidth,
          height: nodeHeight,
          parentId: `group-${g.id}`,
          position: local,
          absolute: { x: x + local.x, y: y + local.y },
          data: { ...n.data, layered: true, color: LAYER_GROUPS[g.id].color },
        })
      })
      x += g.width + GROUP_GAP
    })
    y += rowHeight + ROW_GAP
  })
  return [...groups, ...children]
}
