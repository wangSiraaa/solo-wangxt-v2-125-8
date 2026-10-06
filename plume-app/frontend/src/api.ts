import type {
  ChecksReport,
  MetRow,
  PlumeGridRequest,
  PlumeGridResponse,
  PlumePointRequest,
  PlumePointResult,
  SourceRow,
  StabilitySweepRequest,
  StabilitySweepResponse,
} from './types'

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let body: any = null
    try {
      body = await resp.json()
    } catch {
      /* ignore */
    }
    const err = new Error(
      body?.message || `请求失败：HTTP ${resp.status}`,
    ) as Error & { status: number; apiError?: any }
    err.status = resp.status
    err.apiError = body
    throw err
  }
  return resp.json() as Promise<T>
}

export const api = {
  health: () => fetch('/api/health').then((r) => jsonOrThrow<any>(r)),
  meta: () => fetch('/api/meta').then((r) => jsonOrThrow<any>(r)),
  sources: () => fetch('/api/sources').then((r) => jsonOrThrow<SourceRow[]>(r)),
  meteorology: () =>
    fetch('/api/meteorology').then((r) => jsonOrThrow<MetRow[]>(r)),
  plumeGrid: (req: PlumeGridRequest) =>
    fetch('/api/plume/grid', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    }).then((r) => jsonOrThrow<PlumeGridResponse>(r)),
  plumePoints: (req: PlumePointRequest) =>
    fetch('/api/plume/points', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    }).then((r) => jsonOrThrow<{ points: PlumePointResult[] }>(r)),
  stabilitySweep: (req: StabilitySweepRequest) =>
    fetch('/api/plume/stability-sweep', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    }).then((r) => jsonOrThrow<StabilitySweepResponse>(r)),
  windCheck: (windFromDeg: number) =>
    fetch(
      `/api/plume/wind-check?wind_from_deg=${encodeURIComponent(windFromDeg)}`,
    ).then((r) => jsonOrThrow<any>(r)),
  checks: () => fetch('/api/checks').then((r) => jsonOrThrow<ChecksReport>(r)),
}
