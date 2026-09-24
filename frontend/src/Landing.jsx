import { useEffect, useRef, useState } from 'react'
import logo from './assets/logo.png'
import AgentTree from './landing/AgentTree.jsx'
import Transcript from './landing/Transcript.jsx'
import './Landing.css'

const HEADLINE = 'Ask your repository anything.'

// One request's journey through Git Show, in order — so the stages really
// are a sequence, and the rail beside them tracks where you are.
const STAGES = [
  {
    name: 'Ask',
    title: 'Say what you want, in plain words',
    body: 'Ask about code, commits, issues, or pull requests — or ask for a change. Git Show works out your goal first, then decides whether one agent can handle it or it needs a plan.',
    lines: [
      { who: 'you', text: 'Why does sign-in fail when a token has expired?' },
      { who: 'orchestrator', text: 'Goal: find why expired tokens break sign-in' },
      { who: 'orchestrator', text: 'Needs several steps — handing it to the Planning agent' },
    ],
  },
  {
    name: 'Plan',
    title: 'A plan that checks its own work',
    body: 'The Planning agent splits the goal into steps and sends each to the Reader or the Writer. Every result comes back to it; if a result isn’t what the step expected, it rewrites the plan and carries on.',
    lines: [
      { who: 'planner', text: 'Step 1/3 → Reader: find where tokens are validated' },
      { who: 'reader', text: 'Finding files matching "*auth*"' },
      { who: 'reader', text: 'Reading verify_token in backend/auth.py' },
      { who: 'planner', text: 'Step 1 output as expected — continuing' },
      { who: 'planner', text: 'Step 2 output not as expected — the check lives in middleware.py. Revising the plan' },
    ],
  },
  {
    name: 'Confirm',
    title: 'Nothing changes until you confirm',
    body: 'When the fix needs a commit, the Writer proposes it with the exact diff. It runs only after you press Confirm — then Git Show re-reads GitHub to make sure the change really landed.',
    lines: [
      { who: 'writer', text: 'Proposed: edit backend/middleware.py' },
      { who: 'diff', tone: 'del', text: '-    if token.expires_at > now:' },
      { who: 'diff', tone: 'add', text: '+    if token.expires_at <= now:' },
      { who: 'diff', tone: 'add', text: '+        return refresh(token)' },
      { who: 'writer', text: 'You confirmed — committing' },
      { who: 'verifier', text: 'Confirmed: middleware.py on main has the new content' },
    ],
  },
  {
    name: 'Remember',
    title: 'It remembers what you worked on',
    body: 'Chats, plans, and every step are saved. Pick up any conversation later, or ask what changed last week — Git Show looks it up in your own history.',
    lines: [
      { who: 'you', text: 'What did we change in auth last week?' },
      { who: 'memory', text: 'Accessing memory: runs that edited auth files' },
      { who: 'reader', text: 'Found 2 confirmed edits and 1 cancelled proposal' },
    ],
  },
]

const prefersReducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false

export default function Landing() {
  const canvasRef = useRef(null)
  const storyRef = useRef(null)
  const graphRef = useRef(null)
  const [stage, setStage] = useState(0)
  const [inStory, setInStory] = useState(false)
  const [reducedMotion] = useState(prefersReducedMotion)
  const [graphFailed, setGraphFailed] = useState(false)

  // The 3D scene loads with its own chunk (Three.js), after first paint.
  useEffect(() => {
    let cancelled = false
    import('./landing/commitGraph3d.js')
      .then(({ createCommitGraph }) => {
        if (cancelled || !canvasRef.current) return
        graphRef.current = createCommitGraph(canvasRef.current, { reducedMotion })
      })
      .catch(() => setGraphFailed(true))
    return () => {
      cancelled = true
      graphRef.current?.dispose()
      graphRef.current = null
    }
  }, [reducedMotion])

  // Scroll drives the camera through the graph and picks the active stage.
  useEffect(() => {
    let frame = 0
    const update = () => {
      frame = 0
      const story = storyRef.current
      if (!story) return
      const rect = story.getBoundingClientRect()
      const vh = window.innerHeight
      const storyTravel = Math.max(1, rect.height - vh)
      const storyP = Math.min(1, Math.max(0, -rect.top / storyTravel))
      let p
      if (rect.top > 0) {
        p = 0.2 * Math.min(1, 1 - rect.top / vh)
      } else if (rect.bottom > vh) {
        p = 0.2 + 0.72 * storyP
      } else {
        p = 0.92 + 0.08 * Math.min(1, (vh - rect.bottom) / vh)
      }
      const active = Math.min(STAGES.length - 1, Math.floor(storyP * STAGES.length))
      setStage(active)
      setInStory(rect.top <= vh * 0.5 && rect.bottom > vh * 0.5)
      graphRef.current?.setProgress(p)
      graphRef.current?.setStage(rect.top <= vh * 0.35 && rect.bottom > vh ? active : -1)
    }
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(update)
    }
    const onPointer = (e) => {
      graphRef.current?.setPointer((e.clientX / window.innerWidth - 0.5) * 2, (0.5 - e.clientY / window.innerHeight) * 2)
    }
    update()
    window.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('resize', onScroll)
    if (!reducedMotion) window.addEventListener('pointermove', onPointer, { passive: true })
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onScroll)
      window.removeEventListener('pointermove', onPointer)
    }
  }, [reducedMotion])

  const jumpTo = (i) => {
    const story = storyRef.current
    if (!story) return
    const travel = story.offsetHeight - window.innerHeight
    window.scrollTo({
      top: story.offsetTop + travel * ((i + 0.5) / STAGES.length),
      behavior: reducedMotion ? 'auto' : 'smooth',
    })
  }

  return (
    <div className={`landing ${graphFailed ? 'landing-no-graph' : ''} ${inStory ? 'landing-in-story' : ''}`}>
      <canvas className="landing-canvas" ref={canvasRef} aria-hidden="true" />
      <div className="landing-vignette" aria-hidden="true" />
      <div className="landing-vignette landing-vignette-story" aria-hidden="true" />

      <header className="landing-nav">
        <a className="landing-brand" href="/">
          <img src={logo} alt="" />
          <span>Git Show</span>
        </a>
        <a className="landing-nav-cta" href="/app">
          Open the app
        </a>
      </header>

      <main>
        <section className="landing-hero">
          <h1 className="landing-headline" aria-label={HEADLINE}>
            {HEADLINE.split(' ').map((word, w, words) => {
              // Characters print one by one, but stay grouped by word so a
              // line only ever breaks between words.
              const offset = words.slice(0, w).join(' ').length + (w ? 1 : 0)
              return (
                <span key={w} className="landing-word" aria-hidden="true">
                  {word.split('').map((ch, i) => (
                    <span
                      key={i}
                      className="landing-char"
                      style={{ animationDelay: `${reducedMotion ? 0 : 450 + (offset + i) * 55}ms` }}
                    >
                      {ch}
                    </span>
                  ))}
                  {w === words.length - 1 && <span className="landing-caret" />}
                </span>
              )
            })}
          </h1>
          <p className="landing-lede">
            Git Show reads your code, history, issues, and pull requests on GitHub, and can branch, edit, and open pull
            requests for you. Every change waits for your confirmation.
          </p>
          <div className="landing-actions">
            <a className="landing-btn landing-btn-primary" href="/app">
              Try Git Show
            </a>
            <button type="button" className="landing-btn landing-btn-quiet" onClick={() => jumpTo(0)}>
              See how it works
            </button>
          </div>
        </section>

        <section className="landing-story" ref={storyRef} style={{ height: `${STAGES.length * 105 + 40}vh` }}>
          <div className="story-sticky">
            <nav className="story-rail" aria-label="How Git Show works">
              <ol>
                {STAGES.map((s, i) => (
                  <li key={s.name}>
                    <button
                      type="button"
                      className={`story-rail-step ${i === stage ? 'is-active' : ''} ${i < stage ? 'is-past' : ''}`}
                      aria-current={i === stage ? 'step' : undefined}
                      onClick={() => jumpTo(i)}
                    >
                      <span className="story-rail-dot" aria-hidden="true" />
                      {s.name}
                    </button>
                  </li>
                ))}
              </ol>
            </nav>

            <div className="story-stages">
              {STAGES.map((s, i) => (
                <article
                  key={s.name}
                  className={`story-stage ${i === stage ? 'is-active' : i < stage ? 'is-past' : 'is-next'}`}
                  aria-hidden={i !== stage}
                >
                  <h2>{s.title}</h2>
                  <p className="story-body">{s.body}</p>
                  <Transcript lines={s.lines} play={i === stage} reducedMotion={reducedMotion} />
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="landing-agents" aria-labelledby="agents-heading">
          <div className="landing-agents-inner">
            <h2 id="agents-heading">Who does what</h2>
            <p className="landing-agents-intro">
              Every request starts with the orchestrator. Simple ones go straight to the Reader or the Writer; bigger
              goals go to the Planning agent, which directs both and hears back after every step. Any change you
              confirm is re-checked by the Verifier.
            </p>
            <AgentTree reducedMotion={reducedMotion} />
          </div>
        </section>

        <section className="landing-close">
          <h2>Point it at a repository</h2>
          <p>
            Sign in with GitHub and choose which repositories Git Show may use. You can ask right away — and change
            nothing until you say so.
          </p>
          <a className="landing-btn landing-btn-primary" href="/app">
            Try Git Show
          </a>
        </section>
      </main>

      <footer className="landing-footer">
        <span>Git Show</span>
        <a href="https://github.com/Priyanshu-Madhup/Git-Show">Source on GitHub</a>
      </footer>
    </div>
  )
}
