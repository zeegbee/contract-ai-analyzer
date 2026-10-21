import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ReactNode, useState } from 'react'
import { Link, useParams } from 'react-router'

import {
  ACTIVE_ANALYSIS_STATUSES,
  ANALYSES_KEY,
  type AnalysisDetail,
  cancelAnalysis,
  type Coverage,
  fetchAnalysis,
  FINISHED_STATUSES,
  retryAnalysis,
  type Summary,
} from '../../api/analyses'
import { fetchRedactionConfig, REDACTION_CONFIG_KEY } from '../../api/redaction'
import { ErrorNotice } from '../../components/ErrorNotice'
import { Button, Notice, PageTitle } from '../../components/ui'
import { ru } from '../../i18n/ru'
import { formatDateTime } from '../../lib/format'
import { AnalysisStatusBadge } from './badges'
import { ExportPanel } from './ExportPanel'
import { RisksSection } from './RisksSection'

const t = ru.analyses.detail
const POLL_MS = 2500

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap gap-x-2">
      <dt className="text-slate-600">{label}:</dt>
      <dd className="break-words">{children}</dd>
    </div>
  )
}

function Progress({ analysis }: { analysis: AnalysisDetail }) {
  const { stage, chunks_done: done, chunks_total: total, retry_scheduled: retry } = analysis.progress
  const value = total ? Math.round(((done ?? 0) / total) * 100) : 0
  return (
    <section aria-labelledby="progress-title" className="space-y-2 rounded-lg border border-blue-200 bg-blue-50 p-4">
      <h2 id="progress-title" className="font-medium text-blue-950">
        {t.progress}: {ru.analyses.stages[stage ?? 'queued'] ?? stage}
      </h2>
      {total ? (
        <>
          <progress
            aria-label={t.chunks}
            max={total}
            value={done ?? 0}
            className="h-2 w-full overflow-hidden rounded accent-blue-700"
          >
            {value}%
          </progress>
          <p className="text-sm text-blue-950">
            {t.chunks}: {done ?? 0} / {total}
          </p>
        </>
      ) : null}
      {retry && <p className="text-sm text-amber-900">{t.retryScheduled}</p>}
    </section>
  )
}

/** Строка о маскировке: сколько значений скрыто по категориям или что текст отправлен без маскировки. */
function RedactionLine({ analysis, isCloud }: { analysis: AnalysisDetail; isCloud: boolean }) {
  const tr = ru.redaction.report
  const info = analysis.redaction
  const config = useQuery({
    queryKey: REDACTION_CONFIG_KEY,
    queryFn: ({ signal }) => fetchRedactionConfig(signal),
    enabled: Boolean(info && info.mode === 'masked'),
  })
  if (!info) {
    return 'redaction' in analysis.config_snapshot && !isCloud ? <Row label={tr.title}>{tr.notApplicable}</Row> : null
  }
  if (info.mode !== 'masked') return <Row label={tr.title}>{tr.unmasked}</Row>
  const names = new Map((config.data?.categories ?? []).map((c) => [c.code, c.short]))
  const listed = Object.entries(info.categories)
    .map(([code, n]) => `${names.get(code) ?? code} — ${n}`)
    .join(', ')
  return (
    <Row label={tr.title}>
      {tr.masked(info.values)}
      {listed ? ` (${listed})` : ''}
    </Row>
  )
}

function SummaryPanel({ analysis, summary }: { analysis: AnalysisDetail; summary: Summary | null }) {
  const snapshot = analysis.config_snapshot as {
    model?: { display_name?: string; external_model_id?: string }
    provider?: { name?: string; is_cloud?: boolean }
    verifier?: { same_as_analysis?: boolean; model?: { display_name?: string } }
    pipeline?: { pipeline?: string; prompts?: Record<string, string> }
    captured_at?: string
  }
  const usage = analysis.usage as { prompt_tokens?: number; completion_tokens?: number; cost?: string } | null
  const counts = summary?.counts.by_validation_status ?? {}
  const partyName = analysis.party_name ?? summary?.party.resolved_name
  return (
    <section aria-labelledby="summary-title" className="space-y-3 rounded-lg border border-slate-200 bg-white p-4">
      <h2 id="summary-title" className="text-lg font-semibold">
        {t.summary}
      </h2>
      {summary && <p className="text-sm font-medium text-slate-900">{summary.statement}</p>}
      {summary && (
        <ul className="flex flex-wrap gap-2 text-sm">
          <li className="rounded-md bg-green-50 px-2 py-1 text-green-900">
            {t.counts.verified_match}: {counts.verified_match ?? 0}
          </li>
          <li className="rounded-md bg-amber-50 px-2 py-1 text-amber-950">
            {t.counts.needs_review}: {counts.needs_review ?? 0}
          </li>
          <li className="rounded-md bg-red-50 px-2 py-1 text-red-900">
            {t.counts.rejected}: {counts.rejected ?? 0}
          </li>
          <li className="rounded-md bg-slate-100 px-2 py-1 text-slate-800">
            {t.counts.missing}: {summary.counts.missing_clauses}
          </li>
        </ul>
      )}
      <dl className="grid gap-1 text-sm sm:grid-cols-2">
        <Row label={t.party}>
          {ru.analyses.party[analysis.party]}
          {partyName ? ` — ${partyName}` : ''}
        </Row>
        <Row label={t.model}>
          {snapshot.model?.display_name ?? '—'} ({snapshot.model?.external_model_id ?? '—'})
        </Row>
        <Row label={t.provider}>
          {snapshot.provider?.name ?? '—'} · {snapshot.provider?.is_cloud ? t.cloud : t.local}
        </Row>
        <RedactionLine analysis={analysis} isCloud={Boolean(snapshot.provider?.is_cloud)} />
        <Row label={t.verifier}>
          {snapshot.verifier?.same_as_analysis === false ? (snapshot.verifier.model?.display_name ?? '—') : t.sameModel}
        </Row>
        <Row label={t.depth}>{ru.analyses.depth[analysis.depth]}</Row>
        <Row label={t.categories}>{analysis.categories.map((c) => ru.analyses.categories[c] ?? c).join(', ')}</Row>
        <Row label={t.started}>{formatDateTime(analysis.started_at ?? analysis.created_at)}</Row>
        <Row label={t.finished}>{formatDateTime(analysis.finished_at)}</Row>
        {summary?.contract.contract_type && <Row label={t.contract}>{summary.contract.contract_type}</Row>}
        {usage && (usage.prompt_tokens ?? 0) > 0 && (
          <Row label={t.usage}>
            {(usage.prompt_tokens ?? 0) + (usage.completion_tokens ?? 0)} {t.tokens}
            {usage.cost ? ` · ${usage.cost} USD` : ` · ${t.costUnknown}`}
          </Row>
        )}
        <Row label={t.snapshot}>
          {snapshot.pipeline?.pipeline ?? '—'}
          {snapshot.captured_at ? ` · ${formatDateTime(snapshot.captured_at)}` : ''}
        </Row>
      </dl>
      {summary && summary.warnings.length > 0 && (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
          <p className="font-medium">{t.warnings}</p>
          <ul className="list-disc pl-5">
            {summary.warnings.map((w) => (
              <li key={w}>{t.warningCodes[w] ?? w}</li>
            ))}
          </ul>
        </div>
      )}
      {summary && summary.prompt_injection.length > 0 && (
        <div role="note" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
          {t.injection}: {[...new Set(summary.prompt_injection.map((h) => h.fragment_key))].join(', ')}.
        </div>
      )}
      {summary && (
        <details className="text-sm">
          <summary className="cursor-pointer font-medium">{t.limitations}</summary>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-slate-800">
            {summary.limitations.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </details>
      )}
      {summary && <p className="text-xs text-slate-600">{summary.disclaimer}</p>}
    </section>
  )
}

function CoveragePanel({ coverage }: { coverage: Coverage }) {
  const missed = coverage.sections.filter((s) => s.status !== 'analyzed')
  const cross = coverage.cross_section.status
  return (
    <section aria-labelledby="coverage-title" className="space-y-2 rounded-lg border border-slate-200 bg-white p-4 text-sm">
      <h2 id="coverage-title" className="text-lg font-semibold">
        {t.coverage}
      </h2>
      <p>
        {t.fragments}: {coverage.fragments_analyzed} / {coverage.fragments_total}
      </p>
      {cross && (
        <p>
          {t.crossSection}: {t.crossStatus[cross] ?? cross}
        </p>
      )}
      {coverage.pages_approximate && <p className="text-slate-700">{t.pagesApproximate}</p>}
      {coverage.chunks_failed.length + coverage.chunks_skipped.length > 0 && (
        <p className="text-amber-900">
          {t.failedChunks}:{' '}
          {[...coverage.chunks_failed, ...coverage.chunks_skipped].map((c) => `${c.from ?? '?'}–${c.to ?? '?'}`).join(', ')}
        </p>
      )}
      {coverage.sections.length > 0 && (
        <details open={missed.length > 0}>
          <summary className="cursor-pointer">
            {t.sections}: {coverage.sections.length}
          </summary>
          <ul className="mt-1 space-y-0.5">
            {coverage.sections.map((s) => (
              <li key={s.start_key} className="flex flex-wrap justify-between gap-2">
                <span className="break-words">{s.title}</span>
                <span className={s.status === 'analyzed' ? 'text-green-900' : 'text-amber-900'}>
                  {t.sectionStatus[s.status]}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

export function AnalysisPage() {
  const { analysisId = '' } = useParams()
  const queryClient = useQueryClient()
  const key = [...ANALYSES_KEY, analysisId]
  const [retryConsent, setRetryConsent] = useState(false)
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => fetchAnalysis(analysisId, signal),
    refetchInterval: (q) => (q.state.data && ACTIVE_ANALYSIS_STATUSES.has(q.state.data.status) ? POLL_MS : false),
  })
  const onChanged = (data: AnalysisDetail) => {
    queryClient.setQueryData(key, data)
    void queryClient.invalidateQueries({ queryKey: ANALYSES_KEY })
  }
  const cancel = useMutation({ mutationFn: () => cancelAnalysis(analysisId), onSuccess: onChanged })
  const retry = useMutation({ mutationFn: () => retryAnalysis(analysisId, retryConsent), onSuccess: onChanged })

  if (query.isPending) return <p aria-live="polite">{ru.common.loading}</p>
  if (query.isError) return <ErrorNotice error={query.error} onRetry={() => void query.refetch()} />

  const analysis = query.data
  const active = ACTIVE_ANALYSIS_STATUSES.has(analysis.status)
  const finished = FINISHED_STATUSES.has(analysis.status)
  return (
    <section aria-labelledby="analysis-title" className="space-y-4">
      <Link to="/analyses" className="text-sm text-blue-800 hover:underline">
        ← {t.back}
      </Link>
      <PageTitle
        id="analysis-title"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <AnalysisStatusBadge status={analysis.status} />
            {analysis.can_cancel && (
              <Button
                variant="danger"
                disabled={cancel.isPending}
                onClick={() => window.confirm(t.confirmCancel) && cancel.mutate()}
              >
                {t.cancel}
              </Button>
            )}
          </div>
        }
      >
        {analysis.document_title}
      </PageTitle>
      <p className="text-sm">
        <Link to={`/documents/${encodeURIComponent(analysis.document_id)}`} className="text-blue-800 underline">
          {t.document}
        </Link>
      </p>
      {cancel.isError && <ErrorNotice error={cancel.error} />}

      <div aria-live="polite">
        {active && <Progress analysis={analysis} />}
        {analysis.status === 'failed' && (
          <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900">
            {t.failed}: {analysis.error_message ?? analysis.error_code}
          </div>
        )}
        {analysis.status === 'cancelled' && <Notice>{t.cancelled}</Notice>}
      </div>

      {analysis.can_retry && (
        <div className="flex flex-wrap items-center gap-3">
          {analysis.cloud_transfer && (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={retryConsent} onChange={(e) => setRetryConsent(e.target.checked)} />
              {t.retryConsent}
            </label>
          )}
          <Button
            variant="primary"
            disabled={retry.isPending || (analysis.cloud_transfer && !retryConsent)}
            onClick={() => retry.mutate()}
          >
            {t.retry}
          </Button>
        </div>
      )}
      {retry.isError && <ErrorNotice error={retry.error} />}

      <SummaryPanel analysis={analysis} summary={analysis.summary} />
      {analysis.coverage && <CoveragePanel coverage={analysis.coverage} />}
      {finished && <RisksSection analysisId={analysis.id} documentId={analysis.document_id} />}
      {active && <p className="text-sm text-slate-600">{t.waiting}</p>}
      <ExportPanel analysisId={analysis.id} finished={finished} />
    </section>
  )
}
