"""Регистрирует все ORM-модели в метаданных (нужно Alembic и приложению)."""

from app.models.analyses import Analysis, AnalysisChunk, AnalysisRisk, RiskEvidence
from app.models.catalog import ModelConfig, ProviderConfig
from app.models.documents import Document, DocumentFragment, DocumentPage, DocumentVersion
from app.models.system import AuditLog, BackupRun, ConfigRevision, Export, Job, SystemSetting, WorkerHeartbeat
from app.models.users import (
    ConsentRecord,
    DocumentAccess,
    LoginAttempt,
    User,
    UserModelPermission,
    UserSession,
)

__all__ = [
    "Analysis",
    "AnalysisChunk",
    "AnalysisRisk",
    "AuditLog",
    "BackupRun",
    "ConfigRevision",
    "ConsentRecord",
    "Document",
    "DocumentAccess",
    "DocumentFragment",
    "DocumentPage",
    "DocumentVersion",
    "Export",
    "Job",
    "LoginAttempt",
    "ModelConfig",
    "ProviderConfig",
    "RiskEvidence",
    "SystemSetting",
    "User",
    "UserModelPermission",
    "UserSession",
    "WorkerHeartbeat",
]
