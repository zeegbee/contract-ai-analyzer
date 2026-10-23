import { useId, useState } from 'react'
import { Link } from 'react-router'

import type { Evidence, MatchStatus, RelevanceStatus, Risk } from '../../api/analyses'
import { ru } from '../../i18n/ru'
import { SeverityBadge, ValidationBadge } from './badges'
import { validationHint } from './labels'

const t = ru.analyses.risks
const LONG_QUOTE = 600

const MATCH_TONE: Record<MatchStatus, string> = {
  exact: 'text-green-900',
  normalized: 'text-green-900',
  not_found: 'text-red-800',
  fragment_mismatch: 'text-red-800',
  too_short: 'text-red-800',
  pending: 'text-slate-700',
}
const RELEVANCE_TONE: Record<RelevanceStatus, string> = {
  supports: 'text-green-900',
  partially_supports: 'text-amber-900',
  does_not_support: 'text-red-800',
  unchecked: 'text-amber-900',
}

function Quote({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false)
  const id = useId()
  const long = text.length > LONG_QUOTE
  const shown = long && !expanded ? `${text.slice(0, LONG_QUOTE)}…` : text
  return (
    <div>
      {/* Текст модели и договора выводится как текст (React экранирует), разметка не интерпретируется. */}
      <blockquote
        id={id}
        className="whitespace-pre-wrap break-words border-l-4 border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-900"
      >
        «{shown}»
      </blockquote>
      {long && (
        <button
          type="button"
          aria-controls={id}
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 text-xs text-blue-800 underline"
        >
          {expanded ? t.collapse : t.showFull}
        </button>
      )}
    </div>
  )
}

function pages(e: Evidence): string | null {
  if (e.page_start === null) return null
  const range = e.page_end !== null && e.page_end !== e.page_start ? `${e.page_start}–${e.page_end}` : `${e.page_start}`
  return `${t.page} ${range}${e.pages_approximate ? ` (${t.pageApprox})` : ''}`
}

function EvidenceItem({ evidence, documentId }: { evidence: Evidence; documentId: string }) {
  const place = [pages(evidence), evidence.clause_ref ? `${t.clause} ${evidence.clause_ref}` : null].filter(Boolean)
  const fragmentKey = evidence.fragment_key ?? evidence.claimed_fragment_key
  return (
    <li className="space-y-1">
      <Quote text={evidence.quote} />
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        <span className={MATCH_TONE[evidence.match_status]}>{t.match[evidence.match_status]}</span>
        <span className={RELEVANCE_TONE[evidence.relevance_status]}>{t.relevance[evidence.relevance_status]}</span>
        {place.length > 0 && <span className="text-slate-700">{place.join(', ')}</span>}
        <span className="text-slate-600">
          {t.fragment} {fragmentKey}
          {evidence.fragment_id && (
            <>
              {' · '}
              <Link
                to={`/documents/${encodeURIComponent(documentId)}?fragment=${encodeURIComponent(fragmentKey)}${evidence.page_start !== null ? `&page=${evidence.page_start}` : ''}`}
                className="text-blue-800 underline"
              >
                {t.openFragment}
              </Link>
            </>
          )}
        </span>
      </div>
      {evidence.relevance_rationale && <p className="text-xs text-slate-700">{evidence.relevance_rationale}</p>}
    </li>
  )
}

function Field({ label, children }: { label: string; children: string }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-slate-600">{label}</dt>
      <dd className="whitespace-pre-wrap break-words text-sm text-slate-900">{children}</dd>
    </div>
  )
}

function reasons(risk: Risk): string[] {
  const codes = risk.validation_notes.status_reasons
  if (!Array.isArray(codes)) return []
  return codes.filter((c): c is string => typeof c === 'string').map((c) => t.reasons[c] ?? c)
}

export function RiskCard({ risk, documentId }: { risk: Risk; documentId: string }) {
  const headingId = useId()
  const current = risk.evidence.filter((e) => !e.superseded)
  const superseded = risk.evidence.filter((e) => e.superseded)
  const notes = risk.validation_notes
  const reasonList = reasons(risk)
  const isMissing = risk.kind === 'missing_clause'
  const borderTone =
    risk.validation_status === 'verified_match'
      ? 'border-l-green-600'
      : risk.validation_status === 'rejected'
        ? 'border-l-red-500'
        : 'border-l-amber-500'
  return (
    <article
      aria-labelledby={headingId}
      className={`space-y-3 rounded-lg border border-l-4 border-slate-300 bg-white p-4 shadow-sm ${borderTone}`}
    >
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold text-slate-600">{risk.risk_key}</span>
          <ValidationBadge status={risk.validation_status} kind={risk.kind} />
          <SeverityBadge severity={risk.severity} />
          <span className="rounded-full border border-slate-200 px-2 py-0.5 text-xs text-slate-700">
            {t.kinds[risk.kind]}
          </span>
          <span className="rounded-full border border-slate-200 px-2 py-0.5 text-xs text-slate-700">
            {ru.analyses.categories[risk.category] ?? risk.category}
          </span>
          <span className="text-xs text-slate-700">
            {t.party}: {t.affected[risk.affected_party]}
          </span>
        </div>
        <h3 id={headingId} className="break-words text-base font-semibold text-slate-950">
          {risk.title}
        </h3>
        <p className="text-sm text-slate-700">
          {validationHint(risk.validation_status, risk.kind)}
          {reasonList.length > 0 && <> ({reasonList.join('; ')})</>}
        </p>
      </header>

      <dl className="grid gap-3 md:grid-cols-2">
        <Field label={t.description}>{risk.description}</Field>
        <Field label={t.reason}>{risk.reason}</Field>
        <Field label={t.consequences}>{risk.consequences}</Field>
        <Field label={t.recommendation}>{risk.recommendation}</Field>
      </dl>

      {risk.suggested_revision && (
        <div className="rounded-md border border-slate-200 bg-slate-50 p-3">
          <p className="text-xs font-medium text-slate-700">
            {t.revision} — <span className="font-normal">{t.revisionNote}</span>
          </p>
          <p className="mt-1 whitespace-pre-wrap break-words text-sm text-slate-900">{risk.suggested_revision}</p>
        </div>
      )}

      {notes.legal_reference_unverified === true && (
        <p className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-950">⚠ {t.legal}</p>
      )}
      {notes.party_wording_not_neutral === true && <p className="text-xs text-amber-900">⚠ {t.wording}</p>}

      {isMissing && risk.search_scope && (
        <section aria-label={t.scope} className="space-y-2 rounded-md border border-amber-200 bg-amber-50/50 p-3 text-sm">
          <h4 className="font-medium text-amber-950">{t.scope}</h4>
          {risk.search_scope.model_scope && (
            <p>
              <span className="text-slate-700">{t.scopeModel}: </span>
              <span className="break-words">{risk.search_scope.model_scope}</span>
            </p>
          )}
          {risk.search_scope.checked_sections && risk.search_scope.checked_sections.length > 0 && (
            <details>
              <summary className="cursor-pointer text-slate-800">
                {t.scopeSections} ({risk.search_scope.checked_sections.length})
              </summary>
              <ul className="mt-1 list-disc pl-5 text-slate-800">
                {risk.search_scope.checked_sections.map((s) => (
                  <li key={s} className="break-words">
                    {s}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {risk.search_scope.limitations && risk.search_scope.limitations.length > 0 && (
            <div>
              <p className="text-slate-700">{t.scopeLimitations}:</p>
              <ul className="list-disc pl-5 text-slate-800">
                {risk.search_scope.limitations.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      {(!isMissing || current.length > 0) && (
        <section aria-label={t.evidence} className="space-y-2 border-t border-slate-200 pt-3">
          <h4 className="text-sm font-medium text-slate-800">{t.evidence}</h4>
          {current.length === 0 ? (
            <p className="text-sm text-slate-600">{t.noEvidence}</p>
          ) : (
            <ul className="space-y-3">
              {current.map((e) => (
                <EvidenceItem key={e.id} evidence={e} documentId={documentId} />
              ))}
            </ul>
          )}
          {superseded.length > 0 && (
            <details className="text-sm">
              <summary className="cursor-pointer text-slate-700">
                {t.superseded} ({superseded.length})
              </summary>
              <ul className="mt-2 space-y-3 opacity-80">
                {superseded.map((e) => (
                  <EvidenceItem key={e.id} evidence={e} documentId={documentId} />
                ))}
              </ul>
            </details>
          )}
        </section>
      )}

      <footer className="flex flex-wrap gap-x-4 text-xs text-slate-600">
        {risk.model_confidence !== null && (
          <span>
            {t.confidence}: {risk.model_confidence.toFixed(2).replace('.', ',')} ({t.confidenceNote})
          </span>
        )}
        {typeof notes.merged_from === 'number' && (
          <span>
            {t.merged}: {notes.merged_from}
          </span>
        )}
      </footer>
    </article>
  )
}
