// The one way the app talks to its API. Never call fetch directly.
const API_BASE: string =
  import.meta.env.VITE_API_URL ??
  (import.meta.env.PROD ? import.meta.env.BASE_URL.replace(/\/$/, '') : 'http://localhost:8000')

export class ApiError extends Error {
  constructor(public status: number, message: string, public body: unknown = null) {
    super(message)
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(API_BASE + path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
  if (!response.ok) {
    let message = response.statusText
    let body: unknown = null
    try {
      body = await response.json()
      message = (body as { detail?: string }).detail ?? message
    } catch {
      // not JSON; keep the status text
    }
    throw new ApiError(response.status, message, body)
  }
  return response.json() as Promise<T>
}
