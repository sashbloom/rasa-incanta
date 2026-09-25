
export type DealRow = {
  id: string
  name: string
  account_name: string | null
  owner_name: string | null
  has_actions: boolean
}

export type BoardView = {
  id: 'pipeline' | 'pre_pipeline' | 'prospect'
  label: string
  count: number
  stages: { stage: string; count: number; deals: DealRow[] }[]
}

export type RunView = {
  id: string
  week_start: string
  status: 'running' | 'succeeded' | 'partial' | 'failed'
  started_at: string | null
  finished_at: string | null
  error: string | null
  stats: {
    phase?: 'pull' | 'mail' | 'capability' | 'icp' | 'cards' | 'nba' | 'done'
    sources?: Record<string, string>
    deals?: number
    capability_done?: number
    capability_to_do?: number
    icp_to_score?: number
    icp_done?: number
    icp_cached?: number
    nba_to_draft?: number
    nba_done?: number
    nba_created?: number
    nba_kept?: number
    nba_skipped?: { deal: string; reason: string }[]
  }
}

export type WeekView = { week_start: string; notice: string | null; boards: BoardView[] }

export type Segment = {
  key: string
  name: string
  present: boolean
  source: string | null
  sources: string[]
  date: string | null
  reason: string | null
}

export type Evidence = {
  fact_id: string
  source: string
  source_label: string
  ref: string
  date: string | null
  excerpt: string
}

export type Objective = 'advance' | 'unblock' | 'reframe' | 'nurture' | 're_engage'

export type Action = {
  id: string
  rank: number
  objective: Objective
  action: string
  why_now: string
  effort: string | null
  sme: string | null
  proof: { name: string; why?: string | null } | null
  evidence: Evidence[]
}

export type IcpSummary = {
  status: 'scored' | 'gate_1_stopped'
  recommendation: string | null
  provisional: boolean | null
  client_total: number | null
  client_verdict: string | null
  practus_total: number | null
  practus_verdict: string | null
  gates_fired: string[] | null
  computed_at: string | null
}

export type DealDetail = {
  icp: IcpSummary | null
  id: string
  name: string
  stage: string
  sbu: string | null
  account_name: string | null
  contact_name: string | null
  owner_name: string | null
  ep_involved: string[]
  el_involved: string[]
  days_in_stage: number | null
  last_touch: string | null
  segments: Segment[]
  actions: Action[]
  actions_week: string | null
}

export type SourcesView = {
  last_run: RunView | null
  outlook: { configured: boolean; connected: boolean; account: string | null; mailbox: string | null; redirect_uri: string }
  readai: { configured: boolean; meetings: number; webhook_path: string }
  exa: { configured: boolean }
  anthropic: { configured: boolean }
}
