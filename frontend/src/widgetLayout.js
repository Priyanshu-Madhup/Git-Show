import { useCallback, useEffect, useState } from 'react'

// The repository panel's widgets: which exist, which a chat shows, and
// where each one is. A shown widget is either docked in one of the six home
// slots (three per side column) or floating freely inside the chat area.
// Positions are saved per browser, since they're only a viewing preference;
// which widgets a chat shows is saved with the chat.

// Every widget there is, in display order, with the column it belongs in.
export const WIDGET_INFO = [
  { id: 'branches', side: 'left', name: 'Branches', description: 'Each branch, and how far it is ahead of or behind the default one' },
  { id: 'languages', side: 'left', name: 'Languages', description: 'How the code splits across programming languages' },
  { id: 'contributors', side: 'left', name: 'Contributors', description: 'Who has committed the most' },
  { id: 'tree', side: 'right', name: 'Project tree', description: 'The files and folders, with the full tree a click away' },
  { id: 'commits', side: 'right', name: 'Recent commits', description: 'The latest commits on the default branch' },
  { id: 'summary', side: 'right', name: 'About', description: 'A short summary of what the repository is' },
  { id: 'pulls', side: 'left', name: 'Pull requests', description: 'Open pull requests, most recently updated first' },
  { id: 'issues', side: 'left', name: 'Open issues', description: 'Open issues with their labels, most recently updated first' },
  { id: 'ci', side: 'left', name: 'CI status', description: 'Whether the latest run of each GitHub Actions workflow passed' },
  { id: 'changes', side: 'left', name: 'Changes in this chat', description: 'Every change you confirmed in this chat, with links' },
  { id: 'plan', side: 'right', name: 'Current plan', description: 'The agents’ plan, step by step, kept in view while they work' },
  { id: 'release', side: 'right', name: 'Latest release', description: 'The newest release, and how many commits have landed since' },
  { id: 'suggestions', side: 'right', name: 'Suggested questions', description: 'Questions worth asking about this repository, one click to ask' },
]

const WIDGET_IDS = WIDGET_INFO.map((w) => w.id)
const SIDE = Object.fromEntries(WIDGET_INFO.map((w) => [w.id, w.side]))

// A chat with no saved choice shows these; the rest start off.
export const DEFAULT_WIDGETS = ['branches', 'languages', 'contributors', 'tree', 'commits', 'summary']
// The panel has six places, so a chat shows at most six widgets.
export const MAX_WIDGETS = 6

export const SLOTS = {
  left: ['L1', 'L2', 'L3'],
  right: ['R1', 'R2', 'R3'],
}
const ALL_SLOTS = [...SLOTS.left, ...SLOTS.right]

export const DEFAULT_LAYOUT = {
  slots: { L1: 'branches', L2: 'languages', L3: 'contributors', R1: 'tree', R2: 'commits', R3: 'summary' },
  free: {},
}

const HOME_SLOT = Object.fromEntries(Object.entries(DEFAULT_LAYOUT.slots).map(([slot, id]) => [id, slot]))

const STORAGE_KEY = 'gitshow.widgetLayout.v1'

// Known widgets only, each in at most one place, or the saved layout is
// thrown away.
const isValid = (layout) => {
  if (!layout || typeof layout.slots !== 'object' || typeof layout.free !== 'object') return false
  const placed = [...Object.values(layout.slots).filter(Boolean), ...Object.keys(layout.free)]
  return (
    Object.keys(layout.slots).every((s) => ALL_SLOTS.includes(s)) &&
    placed.every((id) => WIDGET_IDS.includes(id)) &&
    new Set(placed).size === placed.length
  )
}

const load = () => {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY))
    return isValid(saved) ? saved : DEFAULT_LAYOUT
  } catch {
    return DEFAULT_LAYOUT
  }
}

// The layout as it applies to the widgets a chat shows: hidden widgets
// give up their places, and shown widgets without one take the first free
// slot, trying their home slot, then their own column, then the other.
export const effectiveLayout = (layout, visible) => {
  const slots = {}
  for (const s of ALL_SLOTS) {
    const id = layout.slots[s]
    slots[s] = id && visible.includes(id) ? id : null
  }
  const free = {}
  for (const [id, f] of Object.entries(layout.free)) if (visible.includes(id)) free[id] = f
  const placed = new Set([...Object.values(slots), ...Object.keys(free)])
  for (const id of WIDGET_IDS) {
    if (!visible.includes(id) || placed.has(id)) continue
    const other = SIDE[id] === 'left' ? 'right' : 'left'
    const slot = [HOME_SLOT[id], ...SLOTS[SIDE[id]], ...SLOTS[other]].find((s) => s && !slots[s])
    if (slot) slots[slot] = id
    placed.add(id)
  }
  return { slots, free }
}

export const isDefaultLayout = (layout, visible) =>
  JSON.stringify(effectiveLayout(layout, visible)) === JSON.stringify(effectiveLayout(DEFAULT_LAYOUT, visible))

const placeOf = (layout, id) => {
  const slot = Object.keys(layout.slots).find((s) => layout.slots[s] === id)
  return slot ? { slot } : layout.free[id] ? { free: layout.free[id] } : null
}

// Put a widget into a slot. Whatever was there takes the moved widget's old
// place (its slot, or its free position), so nothing is ever lost.
export const dockWidget = (layout, id, slot) => {
  const from = placeOf(layout, id)
  const occupant = layout.slots[slot]
  const slots = { ...layout.slots }
  const free = { ...layout.free }
  if (from?.slot) slots[from.slot] = null
  delete free[id]
  slots[slot] = id
  if (occupant && occupant !== id) {
    if (from?.slot) slots[from.slot] = occupant
    else free[occupant] = from.free
  }
  return { slots, free }
}

export const floatWidget = (layout, id, rect) => {
  const slots = { ...layout.slots }
  for (const s of Object.keys(slots)) if (slots[s] === id) slots[s] = null
  const z = Math.max(0, ...Object.values(layout.free).map((f) => f.z || 0)) + 1
  return { slots, free: { ...layout.free, [id]: { ...rect, z } } }
}

// Back to its home slot, or for a widget without one, a slot in its own
// column (staying put if it's already docked there).
export const sendHome = (layout, id) => {
  const column = SLOTS[SIDE[id]]
  const current = placeOf(layout, id)?.slot
  const home =
    HOME_SLOT[id] || (column.includes(current) ? current : column.find((s) => !layout.slots[s]) || column[0])
  return dockWidget(layout, id, home)
}

export const useWidgetLayout = () => {
  const [layout, setLayout] = useState(load)
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(layout))
    } catch {
      // Private mode or storage blocked: the layout just won't persist.
    }
  }, [layout])
  const reset = useCallback(() => setLayout(DEFAULT_LAYOUT), [])
  return [layout, setLayout, reset]
}

export const useMediaQuery = (query) => {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mql = window.matchMedia(query)
    const onChange = () => setMatches(mql.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])
  return matches
}
