import { forwardRef, useEffect, useId, useMemo, useRef, useState } from 'react'

const RepoIcon = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true">
    <path d="M2 2.5A2.5 2.5 0 0 1 4.5 0h8.75a.75.75 0 0 1 .75.75v12.5a.75.75 0 0 1-.75.75h-2.5a.75.75 0 0 1 0-1.5h1.75v-2h-8a1 1 0 0 0-.714 1.7.75.75 0 1 1-1.072 1.05A2.495 2.495 0 0 1 2 11.5Zm10.5-1h-8a1 1 0 0 0-1 1v6.708A2.486 2.486 0 0 1 4.5 9h8ZM5 12.25a.25.25 0 0 1 .25-.25h3.5a.25.25 0 0 1 .25.25v3.25a.25.25 0 0 1-.4.2l-1.45-1.087a.249.249 0 0 0-.3 0L5.4 15.7a.25.25 0 0 1-.4-.2Z" />
  </svg>
)

const Chevron = () => (
  <svg viewBox="0 0 16 16" width="12" height="12" fill="none" aria-hidden="true">
    <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

const Check = () => (
  <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
    <path d="M3.5 8.5l3 3 6-7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

const MENU_HEIGHT = 340

// The repository picker: a button showing the current repository, opening a
// searchable list. Used in the chat input on the home screen and in the top
// bar once a chat starts; the forwarded ref is the element the dock
// animation moves between the two.
const RepoPicker = forwardRef(function RepoPicker({ repos, value, onChange, variant = 'inline', align = 'left' }, ref) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const [active, setActive] = useState(0)
  const [placement, setPlacement] = useState('down')
  const rootRef = useRef(null)
  const triggerRef = useRef(null)
  const listRef = useRef(null)
  const listId = useId()

  const setRoot = (el) => {
    rootRef.current = el
    if (typeof ref === 'function') ref(el)
    else if (ref) ref.current = el
  }

  // "No repository" first, then the repos matching the filter.
  const options = useMemo(() => {
    const q = filter.trim().toLowerCase()
    const matches = repos.filter((r) => !q || r.full_name.toLowerCase().includes(q))
    return q ? matches : [{ full_name: '', name: 'No repository', none: true }, ...matches]
  }, [repos, filter])

  const openMenu = () => {
    const rect = rootRef.current?.getBoundingClientRect()
    if (rect) {
      const below = window.innerHeight - rect.bottom
      setPlacement(below < MENU_HEIGHT && rect.top > below ? 'up' : 'down')
    }
    const current = options.findIndex((o) => o.full_name === value)
    setActive(current >= 0 ? current : 0)
    setFilter('')
    setOpen(true)
  }

  const close = (refocus = true) => {
    setOpen(false)
    if (refocus) triggerRef.current?.focus()
  }

  const choose = (option) => {
    onChange(option.full_name)
    close()
  }

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e) => {
      if (!rootRef.current?.contains(e.target)) close(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open])

  useEffect(() => {
    if (!open) return
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active, open])

  const onSearchKeyDown = (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((i) => Math.min(options.length - 1, i + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((i) => Math.max(0, i - 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (options[active]) choose(options[active])
    } else if (e.key === 'Escape') {
      e.preventDefault()
      close()
    } else if (e.key === 'Tab') {
      close(false)
    }
  }

  const selected = repos.find((r) => r.full_name === value)
  const [owner, name] = selected ? [selected.full_name.split('/')[0], selected.name] : [null, null]

  return (
    <div
      ref={setRoot}
      className={`repo-picker repo-picker-${variant} ${open ? 'is-open' : ''} ${selected ? 'has-value' : ''}`}
    >
      <button
        ref={triggerRef}
        type="button"
        className="repo-picker-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        title={selected ? `Repository: ${selected.full_name}` : 'Choose the repository Git Show will work on'}
        onClick={() => (open ? close() : openMenu())}
        onKeyDown={(e) => {
          if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !open) {
            e.preventDefault()
            openMenu()
          }
        }}
      >
        <RepoIcon />
        <span className="repo-picker-label">
          {selected ? (
            <>
              <span className="repo-picker-owner">{owner}/</span>
              {name}
            </>
          ) : (
            'Choose a repository'
          )}
        </span>
        <span className="repo-picker-chevron">
          <Chevron />
        </span>
      </button>

      {open && (
        <div className={`repo-picker-menu repo-picker-menu-${placement} repo-picker-menu-${align}`}>
          <input
            className="repo-picker-search"
            type="text"
            value={filter}
            autoFocus
            placeholder="Find a repository…"
            aria-label="Find a repository"
            aria-controls={listId}
            aria-activedescendant={options[active] ? `${listId}-${active}` : undefined}
            onChange={(e) => {
              setFilter(e.target.value)
              setActive(0)
            }}
            onKeyDown={onSearchKeyDown}
          />
          <ul className="repo-picker-list" role="listbox" id={listId} aria-label="Repositories" ref={listRef}>
            {options.map((o, i) => {
              const isSelected = o.full_name === value
              return (
                <li
                  key={o.full_name || 'none'}
                  id={`${listId}-${i}`}
                  data-index={i}
                  role="option"
                  aria-selected={isSelected}
                  className={`repo-picker-option ${i === active ? 'is-active' : ''} ${o.none ? 'is-none' : ''}`}
                  onMouseEnter={() => setActive(i)}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => choose(o)}
                >
                  <span className="repo-picker-option-name">
                    {o.none ? (
                      o.name
                    ) : (
                      <>
                        <span className="repo-picker-owner">{o.full_name.split('/')[0]}/</span>
                        {o.name}
                      </>
                    )}
                  </span>
                  {o.private && <span className="repo-picker-private">Private</span>}
                  <span className="repo-picker-check">{isSelected && <Check />}</span>
                </li>
              )
            })}
            {options.length === 0 && <li className="repo-picker-empty">No repositories match “{filter}”</li>}
          </ul>
        </div>
      )}
    </div>
  )
})

export default RepoPicker
