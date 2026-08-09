"""Permission-scoped web, HTTP API, and read-only database ingestion."""

import asyncio
import hashlib
import ipaddress
import json
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.limiter import limiter
from app.models import DocStatus, Document, KnowledgeBase, OutboxEvent, User
from app.schemas import DocumentOut, ExternalSourceImportRequest
from app.services.audit import add_audit_event
from app.services.object_storage import get_object_storage, make_staging_file
from app.services.permissions import get_kb_with_permission
from app.services.tenancy import lock_workspace_quota

router = APIRouter(prefix="/api/imports", tags=["数据接入"])


def _allowed_host(hostname: str) -> None:
    allowlist = {
        value.strip().casefold()
        for value in settings.external_source_allowed_hosts.split(",")
        if value.strip()
    }
    if hostname.casefold() in allowlist:
        return
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise ValueError("来源主机无法解析") from exc
    for value in addresses:
        address = ipaddress.ip_address(value)
        if any(
            (
                address.is_private,
                address.is_loopback,
                address.is_link_local,
                address.is_multicast,
                address.is_reserved,
                address.is_unspecified,
            )
        ):
            raise ValueError("禁止访问内网/本机地址；可信内网源需加入允许列表")


def _validate_url(value: str, *, database: bool = False) -> None:
    parsed = urlparse(value)
    schemes = {"postgresql", "postgresql+psycopg"} if database else {"http", "https"}
    if parsed.scheme not in schemes or not parsed.hostname:
        raise ValueError("来源 URL 协议或主机无效")
    _allowed_host(parsed.hostname)


def _validate_database_query(query: str) -> str:
    normalized = query.strip()
    if (
        not normalized
        or not normalized.casefold().startswith(("select ", "with "))
        or ";" in normalized
    ):
        raise ValueError("数据库接入只允许单条 SELECT/WITH 查询")
    return normalized


def _select_json_path(value, path: str | None):
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        else:
            raise ValueError("JSON Path 不存在")
    return value


def _records_markdown(records, max_rows: int) -> str:
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list):
        return str(records)
    rows = records[:max_rows]
    if not rows:
        return ""
    if not all(isinstance(row, dict) for row in rows):
        return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    lines = [" | ".join(columns), " | ".join(["---"] * len(columns))]
    for row in rows:
        lines.append(
            " | ".join(str(row.get(column, "")).replace("\n", " ") for column in columns)
        )
    return "\n".join(lines)


async def _fetch_http(body: ExternalSourceImportRequest) -> tuple[bytes, str, str, dict]:
    _validate_url(body.url)
    safe_headers = {
        key: value
        for key, value in body.headers.items()
        if key.casefold() not in {"host", "content-length", "connection"}
    }
    async with httpx.AsyncClient(
        timeout=settings.external_source_timeout_seconds,
        follow_redirects=False,
    ) as client:
        response = await client.get(body.url, headers=safe_headers)
        response.raise_for_status()
        if int(response.headers.get("content-length", "0") or 0) > settings.max_upload_mb * 1024 * 1024:
            raise ValueError("远程内容超过上传大小限制")
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if body.source_type == "web":
            payload = response.content
            extension = ".html" if "html" in content_type else ".txt"
        else:
            if "json" in content_type:
                value = _select_json_path(response.json(), body.json_path)
                payload = _records_markdown(
                    value, min(body.max_rows, settings.external_source_max_rows)
                ).encode()
            else:
                payload = response.content
            extension = ".md"
        if len(payload) > settings.max_upload_mb * 1024 * 1024:
            raise ValueError("远程内容超过上传大小限制")
        return payload, extension, content_type or "text/plain", {
            "http_status": response.status_code,
            "content_type": content_type,
        }


def _fetch_database(body: ExternalSourceImportRequest) -> tuple[bytes, str, str, dict]:
    _validate_url(body.url, database=True)
    query = _validate_database_query(body.query or "")
    engine = create_engine(body.url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            result = connection.execute(text(query))
            columns = list(result.keys())
            maximum = min(body.max_rows, settings.external_source_max_rows)
            rows = [dict(zip(columns, row)) for row in result.fetchmany(maximum)]
    finally:
        engine.dispose()
    payload = _records_markdown(rows, body.max_rows).encode()
    return payload, ".md", "text/markdown", {"row_count": len(rows), "columns": columns}


@router.post("", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(settings.rate_limit_upload)
async def import_source(
    body: ExternalSourceImportRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await get_kb_with_permission(db, body.kb_id, user.id, "write")
    try:
        if body.source_type == "database":
            payload, extension, mime_type, metadata = await asyncio.to_thread(_fetch_database, body)
        else:
            payload, extension, mime_type, metadata = await _fetch_http(body)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not payload:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "来源没有可导入内容")
    content_hash = hashlib.sha256(payload).hexdigest()
    duplicate = (
        await db.execute(
            select(Document.id).where(
                Document.owner_id == user.id,
                Document.kb_id == body.kb_id,
                Document.content_hash == content_hash,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        raise HTTPException(status.HTTP_409_CONFLICT, "该内容已存在于当前知识库")
    await lock_workspace_quota(db, kb.workspace_id)
    used_bytes = (
        await db.execute(
            select(func.coalesce(func.sum(Document.size_bytes), 0))
            .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
            .where(KnowledgeBase.workspace_id == kb.workspace_id)
        )
    ).scalar_one()
    if used_bytes + len(payload) > settings.workspace_storage_quota_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "工作区存储配额不足")
    staging = make_staging_file(extension)
    object_key = f"workspaces/{kb.workspace_id}/knowledge-bases/{kb.id}/{uuid.uuid4().hex}{extension}"
    storage = get_object_storage()
    try:
        staging.write_bytes(payload)
        await asyncio.to_thread(storage.put_file, object_key, staging)
        document = Document(
            owner_id=user.id,
            kb_id=kb.id,
            filename=f"{Path(body.name).stem}{extension}",
            filepath=object_key,
            object_key=object_key,
            content_hash=content_hash,
            mime_type=mime_type,
            size_bytes=len(payload),
            source_type=body.source_type,
            source_url=body.url.split("@")[-1] if body.source_type == "database" else body.url,
            source_metadata=metadata,
            status=DocStatus.uploaded,
            stage=DocStatus.uploaded.value,
        )
        db.add(document)
        await db.flush()
        db.add(
            OutboxEvent(
                event_type="document.ingest.requested",
                aggregate_type="document",
                aggregate_id=document.id,
                payload={"document_id": document.id, "workspace_id": kb.workspace_id},
                dedup_key=f"document.ingest.initial:{document.id}",
            )
        )
        add_audit_event(
            db,
            action="document.import",
            resource_type="document",
            resource_id=document.id,
            actor_user_id=user.id,
            workspace_id=kb.workspace_id,
            request=request,
            after={"source_type": body.source_type, "filename": document.filename},
        )
        await db.commit()
        await db.refresh(document)
        return document
    except Exception:
        await db.rollback()
        await asyncio.to_thread(storage.delete, object_key)
        raise
    finally:
        staging.unlink(missing_ok=True)
