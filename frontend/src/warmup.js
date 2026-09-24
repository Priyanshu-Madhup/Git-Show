// The backend runs on a host that sleeps when idle and takes up to a minute
// to wake. Wake it the moment any page loads, so it's ready by the time
// someone signs in; keep it awake while a page is open; and if a sign-in
// link is clicked before it's up, wait on our own page instead of dropping
// the visitor onto the host's loading screen.

const HEALTH_URL = '/api/health'
const RETRY_MS = 3000
const GIVE_UP_MS = 120000
const KEEP_ALIVE_MS = 10 * 60 * 1000

let ready = false
let waking = null

async function ping() {
  const res = await fetch(HEALTH_URL, { cache: 'no-store' })
  if (!res.ok) throw new Error(`health ${res.status}`)
  const body = await res.json()
  if (body.status !== 'ok') throw new Error('not ready')
}

export function wakeBackend() {
  if (ready) return Promise.resolve(true)
  if (waking) return waking
  const started = Date.now()
  waking = new Promise((resolve) => {
    const attempt = () => {
      ping()
        .then(() => {
          ready = true
          resolve(true)
        })
        .catch(() => {
          if (Date.now() - started > GIVE_UP_MS) {
            waking = null
            resolve(false)
          } else {
            setTimeout(attempt, RETRY_MS)
          }
        })
    }
    attempt()
  })
  return waking
}

function keepAwake() {
  setInterval(() => {
    if (document.visibilityState !== 'visible') return
    ping().catch(() => {
      // It went back to sleep (or the network dropped): wake it again.
      ready = false
      wakeBackend()
    })
  }, KEEP_ALIVE_MS)
}

function showWaitingNote() {
  let note = document.getElementById('wake-note')
  if (!note) {
    note = document.createElement('div')
    note.id = 'wake-note'
    note.className = 'wake-note'
    note.setAttribute('role', 'status')
    note.innerHTML =
      '<span class="wake-note-spinner" aria-hidden="true"></span>' +
      '<span>Starting the server — the first visit after a quiet spell takes up to a minute. You’ll be sent on automatically.</span>'
    document.body.appendChild(note)
  }
  return note
}

// Sign-in and install links go to the backend, so hold them until it's up.
function holdBackendLinks() {
  document.addEventListener('click', (event) => {
    if (ready || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return
    const link = event.target.closest?.('a[href^="/api/auth/"]')
    if (!link) return
    event.preventDefault()
    const note = showWaitingNote()
    // Go even if it never answered: the host's own loading page is better
    // than a button that does nothing.
    wakeBackend().then(() => {
      note.remove()
      window.location.href = link.href
    })
  })
}

export function startWarmup() {
  wakeBackend()
  keepAwake()
  holdBackendLinks()
}
