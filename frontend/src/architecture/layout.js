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
