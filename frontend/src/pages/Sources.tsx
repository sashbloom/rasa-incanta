import { useEffect, useState, type FormEvent } from 'react'
import { apiFetch, ApiError } from '../api'
import { timeIST } from '../format'
import type { SourcesView } from '../types'

// Where each source's status lives in the latest run's stats.sources, and its display name.
const RUN_SOURCES: [string, string][] = [
  ['zoho', 'Zoho deals'],
  ['zoho_outreach', 'Zoho outreach log'],
  ['outlook', 'Outlook mail'],
  ['readai', 'Read.ai meetings'],
  ['setu', 'Setu case studies'],
  ['setu_rerank', 'Setu matching (Claude re-rank)'],
  ['icp', 'ICP scoring'],
]

function Row({ name, status }: { name: string; status: string | undefined }) {
  const ok = status === 'ok' || status?.startsWith('ok ')
  return (
    <div className="grid grid-cols-[200px_1fr] gap-4 border-t border-line py-2.5">
      <dt className="t-label leading-6">{name}</dt>
      <dd className="m-0">
        {status === undefined ? <span className="text-slate">Not run yet</span> : (
          <>
            <span className="t-label mr-2">{ok ? 'Working' : 'Needs attention'}</span>
            {!ok && <span>{status}</span>}
            {ok && status !== 'ok' && <span className="text-slate">{status.replace(/^ok \(?|\)$/g, '')}</span>}
          </>
        )}
      </dd>
    </div>
  )
}

function OutlookConnect({ view, onConnected }: { view: SourcesView['outlook']; onConnected: () => void }) {
  const [authorizeUrl, setAuthorizeUrl] = useState<string | null>(null)
  const [pasted, setPasted] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function start() {
    setMessage(null)
    try {
      const { authorize_url } = await apiFetch<{ authorize_url: string }>('/api/outlook/connect/start', { method: 'POST' })
      setAuthorizeUrl(authorize_url)
      window.open(authorize_url, '_blank', 'noopener')
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : 'Could not reach the server.')
    }
  }

  async function finish(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setMessage(null)
    try {
      const done = await apiFetch<{ account: string }>('/api/outlook/connect/finish', {
        method: 'POST', body: JSON.stringify({ redirect_url: pasted }),
      })
      setMessage(`Connected to ${done.account}.`)
      setAuthorizeUrl(null)
      setPasted('')
      onConnected()
    } catch (e) {
      setMessage(e instanceof ApiError ? e.message : 'Could not reach the server.')
    } finally {
      setBusy(false)
    }
  }

  if (!view.configured) {
    return <p className="m-0">Set MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET and MYRAH_MAILBOX on the server first.</p>
  }
  return (
    <div>
      <p className="m-0">
        {view.connected ? `Connected to ${view.account}.` : `Not connected. ${view.mailbox} needs to sign in once.`}
      </p>
      {!authorizeUrl ? (
        <button type="button" onClick={start}
          className="t-label mt-3 cursor-pointer rounded-md border-0 bg-gold-web px-4 py-2 text-navy">
          {view.connected ? 'Reconnect Outlook' : 'Connect Outlook'}
        </button>
      ) : (
        <form onSubmit={finish} className="mt-3">
          <p className="m-0">
            Sign in as {view.mailbox} in the tab that opened (<a href={authorizeUrl} target="_blank" rel="noopener">open it again</a>).
            Microsoft then sends you to a page that does not load. Copy that page's full address and paste it here.
          </p>
          <label className="t-label mt-3 block">
            Address after sign-in
            <input value={pasted} onChange={(e) => setPasted(e.target.value)} required placeholder="http://localhost/callback?code=..."
              className="mt-1 block w-full rounded-md border border-line bg-white px-3 py-2 text-[15px] text-navy" />
          </label>
          <button type="submit" disabled={busy}
            className="t-label mt-3 cursor-pointer rounded-md border-0 bg-gold-web px-4 py-2 text-navy disabled:bg-line">
            {busy ? 'Connecting' : 'Finish connecting'}
          </button>
        </form>
      )}
      {message && <p role="status" className="m-0 mt-3">{message}</p>}
    </div>
  )
}

/** Source health: each source's status from the latest run, plus the Outlook sign-in. */
export function Sources() {
  const [view, setView] = useState<SourcesView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const load = () => apiFetch<SourcesView>('/api/sources').then(setView).catch((e: Error) => setError(e.message))
  useEffect(() => {
    load()
  }, [])

  if (error) return <p className="p-10">{error}</p>
  if (!view) return <p className="t-meta p-10">Loading sources.</p>

  const run = view.last_run
  const webhook = `${window.location.origin}${import.meta.env.BASE_URL.replace(/\/$/, '')}${view.readai.webhook_path}`
  return (
    <div className="max-w-[760px] px-4 py-6 md:px-12 md:py-10">
      <h1 className="t-page m-0">Sources</h1>
      <p className="t-meta m-0">{run ? `From the run at ${timeIST(run.started_at)}` : 'No run yet.'}</p>

      <dl className="m-0 mt-6">
        {RUN_SOURCES.map(([key, name]) => <Row key={key} name={name} status={run?.stats.sources?.[key]} />)}
      </dl>

      <section className="mt-10" aria-labelledby="outlook-heading">
        <h2 id="outlook-heading" className="t-section m-0 mb-2">Outlook</h2>
        <OutlookConnect view={view.outlook} onConnected={load} />
      </section>

      <section className="mt-10" aria-labelledby="readai-heading">
        <h2 id="readai-heading" className="t-section m-0 mb-2">Read.ai</h2>
        <p className="m-0">
          {view.readai.configured ? `${view.readai.meetings} ${view.readai.meetings === 1 ? 'meeting' : 'meetings'} received.` : 'READAI_WEBHOOK_SECRET is not set, so deliveries are refused.'}
        </p>
        <p className="m-0 mt-2">In Read.ai, add a workspace webhook for "meeting end" with this address:</p>
        <code className="mt-1 block break-all rounded-md border border-line bg-white px-3 py-2 text-[13px]">{webhook}</code>
      </section>

      <section className="mt-10" aria-labelledby="keys-heading">
        <h2 id="keys-heading" className="t-section m-0 mb-2">Keys</h2>
        <dl className="m-0">
          <Row name="Anthropic (Claude)" status={view.anthropic.configured ? 'ok' : 'ANTHROPIC_API_KEY is not set.'} />
          <Row name="Exa (ICP web research)" status={view.exa.configured ? 'ok' : 'EXA_API_KEY is not set: the ICP web-research criteria become data gaps.'} />
        </dl>
      </section>
    </div>
  )
}
