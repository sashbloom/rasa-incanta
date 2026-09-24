import { useState, type FormEvent } from 'react'
import { apiFetch, ApiError } from '../api'
import type { Me } from '../types'

export function SignIn({ onSignedIn }: { onSignedIn: (me: Me) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onSignedIn(await apiFetch<Me>('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not reach the server. Try again in a moment.')
    } finally {
      setBusy(false)
    }
  }

  const input = 'mt-1 block w-full rounded-md border border-line bg-white px-3 py-2 text-[15px] text-navy'
  return (
    <main className="flex min-h-full items-start justify-center px-4 pt-[14vh]">
      <form onSubmit={submit} className="w-full max-w-[360px]" aria-labelledby="sign-in-title">
        <p className="t-deal m-0 mb-8">Rasa Incanta</p>
        <h1 id="sign-in-title" className="t-section m-0 mb-5">Sign in</h1>
        <label className="t-label block">
          Username
          <input className={input} value={username} onChange={(e) => setUsername(e.target.value)}
            autoComplete="username" autoFocus required />
        </label>
        <label className="t-label mt-4 block">
          Password
          <input className={input} type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password" required />
        </label>
        {error && <p role="alert" className="mt-4 mb-0">{error}</p>}
        <button type="submit" disabled={busy}
          className="t-label mt-6 cursor-pointer rounded-md border-0 bg-gold-web px-5 py-2.5 text-navy disabled:cursor-default disabled:bg-line">
          {busy ? 'Signing in' : 'Sign in'}
        </button>
      </form>
    </main>
  )
}
