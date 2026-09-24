import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/fraunces/standard-italic.css'
import '@fontsource-variable/fraunces/standard.css'
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import './index.css'
import { startWarmup } from './warmup.js'

// Wake the backend immediately, before either page has even rendered.
startWarmup()

// Two pages: the landing page at "/", the chat app at "/app". Each is its own
// chunk, so the app never downloads the landing page's 3D scene.
const App = lazy(() => import('./App.jsx'))
const Landing = lazy(() => import('./Landing.jsx'))

const Page = window.location.pathname.startsWith('/app') ? App : Landing

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <Suspense fallback={null}>
      <Page />
    </Suspense>
  </StrictMode>,
)
