import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { DEFAULT_WIDGETS, MAX_WIDGETS, WIDGET_INFO } from './widgetLayout.js'

const CloseIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M6 6L18 18M18 6L6 18" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
  </svg>
)

const SAVE_NOTES = {
  saving: 'Saving…',
  saved: 'Saved for this chat',
  failed: "Couldn't save. Your choice applies until you reload.",
}

// The window behind the top bar's widgets button: switch each widget on or
// off for the current chat, up to MAX_WIDGETS at once. Changes save as
// they're made.
const WidgetPicker = ({ visible, onChange, saveState, canResetPositions, onResetPositions, onClose }) => {
  useEffect(() => {
    const onKeyDown = (e) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const full = visible.length >= MAX_WIDGETS
  const toggle = (id) => onChange(visible.includes(id) ? visible.filter((w) => w !== id) : [...visible, id])
  const isDefault = visible.length === DEFAULT_WIDGETS.length && DEFAULT_WIDGETS.every((w) => visible.includes(w))

  return createPortal(
    <div className="tree-modal-backdrop" onClick={onClose}>
      <div className="widget-picker" role="dialog" aria-modal="true" aria-label="Widgets" onClick={(e) => e.stopPropagation()}>
        <div className="tree-modal-head">
          <div className="tree-modal-title">
            <h2>Widgets</h2>
            <span className="widget-meta">
              Choose up to {MAX_WIDGETS} for this chat · {visible.length} on
            </span>
          </div>
          <button type="button" className="sidebar-close" aria-label="Close" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <ul className="widget-picker-list">
          {WIDGET_INFO.map((w) => {
            const on = visible.includes(w.id)
            const blocked = !on && full
            return (
              <li key={w.id}>
                <label
                  className={`widget-picker-row ${blocked ? 'widget-picker-row-blocked' : ''}`}
                  title={blocked ? `Switch one off first: a chat shows at most ${MAX_WIDGETS}.` : undefined}
                >
                  <span className="widget-picker-text">
                    <span className="widget-picker-name">{w.name}</span>
                    <span className="widget-picker-desc">{w.description}</span>
                  </span>
                  <input
                    type="checkbox"
                    className="widget-switch"
                    checked={on}
                    disabled={blocked}
                    onChange={() => toggle(w.id)}
                  />
                </label>
              </li>
            )
          })}
        </ul>
        <div className="widget-picker-foot">
          <span className={`widget-picker-note ${saveState === 'failed' ? 'widget-picker-note-error' : ''}`} role="status">
            {SAVE_NOTES[saveState] || (full ? `${MAX_WIDGETS} on: switch one off to add another.` : '')}
          </span>
          <span className="widget-picker-actions">
            {canResetPositions && (
              <button type="button" className="action-toggle" onClick={onResetPositions}>
                Reset positions
              </button>
            )}
            {!isDefault && (
              <button type="button" className="action-toggle" onClick={() => onChange(DEFAULT_WIDGETS)}>
                Use defaults
              </button>
            )}
          </span>
        </div>
      </div>
    </div>,
    document.body,
  )
}

export default WidgetPicker
