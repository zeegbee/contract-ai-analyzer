import { z } from 'zod'

import { apiRequest } from './client'

const checkValue = z.string()

export const readinessSchema = z.object({
  status: z.enum(['ready', 'not_ready']),
  checks: z.record(z.string(), checkValue),
  version: z.string(),
})

export type Readiness = z.infer<typeof readinessSchema>

export async function fetchReadiness(signal?: AbortSignal): Promise<Readiness> {
  const raw = await apiRequest<unknown>('/health/ready', { signal, acceptStatuses: [503] })
  return readinessSchema.parse(raw)
}
