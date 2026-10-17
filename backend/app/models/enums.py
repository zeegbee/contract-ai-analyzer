"""Перечисления предметной области (значения хранятся в БД как строки)."""

from enum import StrEnum


class UserRole(StrEnum):
    superadmin = "superadmin"
    admin = "admin"
    analyst = "analyst"
    viewer = "viewer"


class DocumentPermission(StrEnum):
    view = "view"
    analyze = "analyze"


class DocumentStatus(StrEnum):
    active = "active"
    deleting = "deleting"


class DocumentFormat(StrEnum):
    pdf = "pdf"
    docx = "docx"
    doc = "doc"


class ExtractionStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"
    unsupported_no_text = "unsupported_no_text"


class PageSource(StrEnum):
    pdf_native = "pdf_native"
    docx_rendered_breaks = "docx_rendered_breaks"
    libreoffice_render = "libreoffice_render"


class FragmentKind(StrEnum):
    heading = "heading"
    paragraph = "paragraph"
    list_item = "list_item"
    table_row = "table_row"
    table_cell = "table_cell"
    footnote = "footnote"
    header_footer = "header_footer"


class AnalysisStatus(StrEnum):
    queued = "queued"
    running = "running"
    verifying = "verifying"
    completed = "completed"
    completed_with_warnings = "completed_with_warnings"
    failed = "failed"
    cancelling = "cancelling"
    cancelled = "cancelled"


class AnalysisParty(StrEnum):
    party_a = "party_a"
    party_b = "party_b"
    named = "named"
    unknown = "unknown"


class AnalysisDepth(StrEnum):
    quick = "quick"
    standard = "standard"
    deep = "deep"


class ChunkStatus(StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    skipped = "skipped"


class RiskKind(StrEnum):
    clause_risk = "clause_risk"
    missing_clause = "missing_clause"
    contradiction = "contradiction"


class RiskCategory(StrEnum):
    payment = "payment"
    liability = "liability"
    penalties = "penalties"
    deadlines = "deadlines"
    renewal = "renewal"
    termination = "termination"
    unilateral_changes = "unilateral_changes"
    warranties = "warranties"
    confidentiality = "confidentiality"
    personal_data = "personal_data"
    intellectual_property = "intellectual_property"
    governing_law = "governing_law"
    force_majeure = "force_majeure"
    acceptance = "acceptance"
    undefined_terms = "undefined_terms"
    contradiction = "contradiction"
    missing_clause = "missing_clause"
    other = "other"


class RiskSeverity(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    informational = "informational"


class AffectedParty(StrEnum):
    party_a = "party_a"
    party_b = "party_b"
    both = "both"
    unclear = "unclear"


class ValidationStatus(StrEnum):
    pending = "pending"
    verified_match = "verified_match"
    needs_review = "needs_review"
    rejected = "rejected"


class MatchStatus(StrEnum):
    pending = "pending"
    exact = "exact"
    normalized = "normalized"
    not_found = "not_found"
    fragment_mismatch = "fragment_mismatch"
    too_short = "too_short"


class RelevanceStatus(StrEnum):
    supports = "supports"
    partially_supports = "partially_supports"
    does_not_support = "does_not_support"
    unchecked = "unchecked"


class ProviderType(StrEnum):
    openrouter = "openrouter"
    ollama = "ollama"


class StructuredOutputSupport(StrEnum):
    json_schema = "json_schema"
    json_mode = "json_mode"
    none = "none"
    unknown = "unknown"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    dead = "dead"


class ExportFormat(StrEnum):
    pdf = "pdf"
    docx = "docx"
    csv = "csv"
    xlsx = "xlsx"


class ExportStatus(StrEnum):
    queued = "queued"
    ready = "ready"
    failed = "failed"
    expired = "expired"


class BackupRunStatus(StrEnum):
    running = "running"
    success = "success"
    failed = "failed"
    # Запуск не сообщил о завершении (хост перезагружен, скрипт убит) — закрывается автоматически.
    interrupted = "interrupted"


class BackupTrigger(StrEnum):
    scheduled = "scheduled"  # по расписанию (deploy/backup.sh --scheduled)
    requested = "requested"  # «Запустить сейчас» в UI, выполнен планировщиком
    manual = "manual"  # deploy/backup.sh вручную на сервере
