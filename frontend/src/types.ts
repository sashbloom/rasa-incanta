export type Me = { username: string; role: string; sbus: string[]; via_portal: boolean }

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

export type WeekView = { week_start: string; notice: string | null; boards: BoardView[] }

export type Segment = {
  key: string
  name: string
  present: boolean
  source: string | null
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
  evidence: Evidence[]
}

export type DealDetail = {
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
