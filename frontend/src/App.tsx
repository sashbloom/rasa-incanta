import { BrowserRouter, Link, Route, Routes, useLocation } from 'react-router-dom'
import { MyWeek } from './pages/MyWeek'
import { Sources } from './pages/Sources'

// Derived from Vite's base (/reports/rasa-incanta/), never a second copy of the string.
export const BASENAME = import.meta.env.BASE_URL.replace(/\/$/, '')

function Rail() {
  // "My week" covers the board and every deal under it; "Sources" is its own page.
  const onSources = useLocation().pathname.startsWith('/sources')
  const items: [string, string, boolean][] = [['/', 'My week', !onSources], ['/sources', 'Sources', onSources]]
  return (
    <header className="flex shrink-0 flex-col bg-navy px-4 py-4 text-paper md:w-[220px] md:px-6 md:py-7">
      <p className="m-0 font-serif text-xl leading-[26px] font-bold md:mb-8">Rasa Incanta</p>
      <nav aria-label="Main" className="mt-2 flex gap-4 md:mt-0 md:block">
        {items.map(([to, label, active]) => (
          <Link key={to} to={to} aria-current={active ? 'page' : undefined}
            className={`t-label block py-1.5 text-paper no-underline ${active ? 'md:pl-2.5 md:shadow-[inset_3px_0_0_var(--gold)]' : ''}`}>
            {label}
          </Link>
        ))}
      </nav>
    </header>
  )
}

// The board is open: no sign-in, straight to My week.
export function App() {
  return (
    <BrowserRouter basename={BASENAME}>
      <div className="flex min-h-full flex-col md:flex-row">
        <Rail />
        <main className="min-w-0 flex-1">
          <Routes>
            <Route path="/" element={<MyWeek />} />
            <Route path="/deals/:dealId" element={<MyWeek />} />
            <Route path="/sources" element={<Sources />} />
            <Route path="*" element={<MyWeek />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
