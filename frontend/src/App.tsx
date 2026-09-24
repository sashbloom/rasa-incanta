import { useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom'
import { apiFetch } from './api'
import { MyWeek } from './pages/MyWeek'
import { SignIn } from './pages/SignIn'
import type { Me } from './types'

// Derived from Vite's base (/reports/rasa-incanta/), never a second copy of the string.
export const BASENAME = import.meta.env.BASE_URL.replace(/\/$/, '')

function Rail({ me, onSignOut }: { me: Me; onSignOut: () => void }) {
  return (
    <header className="flex shrink-0 flex-col bg-navy px-4 py-4 text-paper md:w-[220px] md:px-6 md:py-7">
      <p className="m-0 font-serif text-xl leading-[26px] font-bold md:mb-8">Rasa Incanta</p>
      <nav aria-label="Main" className="mt-2 flex gap-4 md:mt-0 md:block">
        <NavLink to="/" end={false}
          className={({ isActive }) => `t-label block py-1.5 text-paper no-underline ${isActive ? 'md:pl-2.5 md:shadow-[inset_3px_0_0_var(--gold)]' : ''}`}>
          My week
        </NavLink>
      </nav>
      <div className="t-meta mt-3 text-paper/80 md:mt-auto">
        <span className="block text-paper">{me.username}</span>
        {!me.via_portal && (
          <button type="button" onClick={onSignOut}
            className="t-meta mt-1 cursor-pointer border-0 bg-transparent p-0 text-paper underline">
            Sign out
          </button>
        )}
      </div>
    </header>
  )
}

export function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined)

  useEffect(() => {
    apiFetch<Me>('/api/me')
      .then(setMe)
      .catch(() => setMe(null)) // not signed in, or the server is unreachable: show sign-in
  }, [])

  async function signOut() {
    await apiFetch('/api/auth/logout', { method: 'POST' }).catch(() => undefined)
    setMe(null)
  }

  if (me === undefined) return null
  if (me === null) return <SignIn onSignedIn={setMe} />

  return (
    <BrowserRouter basename={BASENAME}>
      <div className="flex min-h-full flex-col md:flex-row">
        <Rail me={me} onSignOut={signOut} />
        <main className="min-w-0 flex-1">
          <Routes>
            <Route path="/" element={<MyWeek />} />
            <Route path="/deals/:dealId" element={<MyWeek />} />
            <Route path="*" element={<MyWeek />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
