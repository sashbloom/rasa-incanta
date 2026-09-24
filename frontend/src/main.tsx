import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App, BASENAME } from './App'
import './styles.css'

// The server answers at / and under /reports/rasa-incanta/ with the same page. The router's
// basename is the prefix, so a visit at the root moves the address under the prefix first.
if (!window.location.pathname.startsWith(BASENAME)) {
  const { pathname, search, hash } = window.location
  window.history.replaceState(null, '', `${BASENAME}${pathname === '/' ? '/' : pathname}${search}${hash}`)
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
