"""Append-only audit event helpers."""

from fastapi import Request

from app.models import AuditLog


def _trace_id() -> str | None:
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        return f"{context.trace_id:032x}" if context.is_valid else None
    except ImportError:
        return None


def add_audit_event(
    db,
    *,
    action: str,
    resource_type: str,
    actor_user_id: str | None,
    workspace_id: str | None = None,
    organization_id: str | None = None,
    resource_id: str | None = None,
    request: Request | None = None,
    outcome: str = "success",
    before: dict | None = None,
    after: dict | None = None,
) -> AuditLog:
    event = AuditLog(
        organization_id=organization_id,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        source_ip=request.client.host if request and request.client else None,
        request_id=(
            getattr(request.state, "request_id", None)
            or request.headers.get("X-Request-ID")
            if request
            else None
        ),
        trace_id=_trace_id(),
        before=before,
        after=after,
    )
    db.add(event)
    return event
