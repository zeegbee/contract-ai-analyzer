"""Загрузка объектов с проверкой доступа в самом SQL-запросе.

Объект не загружается «сначала, а проверяется потом»: условие доступа входит в WHERE,
поэтому чужой и несуществующий объект неразличимы — оба дают 404.

Правила (ARCHITECTURE §6.1):
- Superadmin — системный доступ ко всем активным документам (обращение к чужому аудируется).
- view: владелец или пользователь с явным `DocumentAccess` (любым).
- analyze: роль analyst+ и (владелец или `DocumentAccess.permission = analyze`).
- manage (удаление, выдача доступа): роль analyst+ и владелец.
- Анализы и экспорты проверяются через родительский документ.
- Документы в статусе `deleting` недоступны никому.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from sqlalchemy import ColumnElement, Select, and_, exists, false, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models.analyses import Analysis
from app.models.documents import Document, DocumentVersion
from app.models.enums import DocumentPermission, DocumentStatus
from app.models.system import Export
from app.models.users import DocumentAccess
from app.security.policies import CONTRIBUTOR_ROLES, Actor, is_superadmin


class DocumentAction(StrEnum):
    view = "view"
    analyze = "analyze"
    manage = "manage"


def _has_access_row(actor_id: object, permissions: tuple[DocumentPermission, ...]) -> ColumnElement[bool]:
    return exists().where(
        DocumentAccess.document_id == Document.id,
        DocumentAccess.user_id == actor_id,
        DocumentAccess.permission.in_(permissions),
    )


def _personal_view_clause(actor_id: object) -> ColumnElement[bool]:
    granted = _has_access_row(actor_id, (DocumentPermission.view, DocumentPermission.analyze))
    return and_(Document.status == DocumentStatus.active, or_(Document.owner_id == actor_id, granted))


def document_access_clause(actor: Actor, action: DocumentAction) -> ColumnElement[bool]:
    """Условие WHERE для таблицы documents."""
    active = Document.status == DocumentStatus.active
    if is_superadmin(actor):
        return active
    is_owner = Document.owner_id == actor.id
    contributor = true() if actor.role in CONTRIBUTOR_ROLES else false()
    if action is DocumentAction.view:
        return _personal_view_clause(actor.id)
    if action is DocumentAction.analyze:
        granted = _has_access_row(actor.id, (DocumentPermission.analyze,))
        return and_(active, contributor, or_(is_owner, granted))
    return and_(active, contributor, is_owner)


def listed_documents_query(actor: Actor) -> Select[Document]:
    """Список «моих» документов: свои и явно доступные — для всех ролей, включая Superadmin.

    Системный доступ Superadmin к чужим документам — только по ID и с записью в аудит,
    а не массовым просмотром списка.
    """
    return select(Document).where(_personal_view_clause(actor.id))


async def get_document_for(
    session: AsyncSession, actor: Actor, document_id: uuid.UUID, action: DocumentAction
) -> Document:
    stmt = select(Document).where(Document.id == document_id, document_access_clause(actor, action))
    document = (await session.execute(stmt)).scalar_one_or_none()
    if document is None:
        raise NotFoundError("Документ не найден", code="document_not_found")
    return document


async def get_version_for(
    session: AsyncSession,
    actor: Actor,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    action: DocumentAction = DocumentAction.view,
) -> tuple[Document, DocumentVersion]:
    """Версия проверяется через родительский документ из URL: чужая или «не та» версия → 404."""
    stmt = (
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(
            Document.id == document_id,
            DocumentVersion.id == version_id,
            document_access_clause(actor, action),
        )
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise NotFoundError("Версия документа не найдена", code="version_not_found")
    return row[0], row[1]


async def get_analysis_for(
    session: AsyncSession, actor: Actor, analysis_id: uuid.UUID, action: DocumentAction = DocumentAction.view
) -> Analysis:
    stmt = (
        select(Analysis)
        .join(Document, Document.id == Analysis.document_id)
        .where(Analysis.id == analysis_id, document_access_clause(actor, action))
    )
    analysis = (await session.execute(stmt)).scalar_one_or_none()
    if analysis is None:
        raise NotFoundError("Анализ не найден", code="analysis_not_found")
    return analysis


async def get_export_for(session: AsyncSession, actor: Actor, export_id: uuid.UUID) -> Export:
    stmt = (
        select(Export)
        .join(Analysis, Analysis.id == Export.analysis_id)
        .join(Document, Document.id == Analysis.document_id)
        .where(Export.id == export_id, document_access_clause(actor, DocumentAction.view))
    )
    export = (await session.execute(stmt)).scalar_one_or_none()
    if export is None:
        raise NotFoundError("Экспорт не найден", code="export_not_found")
    return export


async def is_superadmin_override(session: AsyncSession, actor: Actor, document: Document) -> bool:
    """True, если доступ к документу есть только благодаря роли Superadmin (такой доступ аудируется)."""
    if not is_superadmin(actor) or document.owner_id == actor.id:
        return False
    stmt = select(
        exists().where(DocumentAccess.document_id == document.id, DocumentAccess.user_id == actor.id)
    )
    return not (await session.execute(stmt)).scalar_one()
