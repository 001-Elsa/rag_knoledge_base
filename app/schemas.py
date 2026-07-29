"""Pydantic 请求/响应模型。"""
from datetime import datetime

from pydantic import BaseModel, Field


# ---- 认证 ----
class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class WebSocketTicketResponse(BaseModel):
    ticket: str
    expires_in: int


class MeResponse(BaseModel):
    id: str
    username: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- 知识库 ----
class KBCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=255)
    workspace_id: str | None = None


class KBOut(BaseModel):
    id: str
    workspace_id: str
    name: str
    description: str
    created_at: datetime
    doc_count: int = 0

    model_config = {"from_attributes": True}


# ---- 文档 ----
class DocumentOut(BaseModel):
    id: str
    kb_id: str
    filename: str
    status: str
    stage: str
    chunk_count: int
    retry_count: int = 0
    active_index_version: int | None = None
    target_index_version: int | None = None
    worker_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None
    quarantined: bool = False
    error: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- 企业租户与权限 ----
class WorkspaceOut(BaseModel):
    id: str
    organization_id: str
    name: str
    role: str
    created_at: datetime


class OrganizationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class OrganizationOut(BaseModel):
    id: str
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkspaceCreateRequest(BaseModel):
    organization_id: str
    name: str = Field(min_length=1, max_length=128)


class MemberCreateRequest(BaseModel):
    username: str
    role: str = Field(pattern="^(admin|editor|viewer|auditor)$")


class MemberRoleUpdateRequest(BaseModel):
    role: str = Field(pattern="^(admin|editor|viewer|auditor)$")


class MemberOut(BaseModel):
    user_id: str
    username: str
    role: str
    created_at: datetime


class AuditLogOut(BaseModel):
    id: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    actor_user_id: str | None
    request_id: str | None
    trace_id: str | None
    before: dict | None
    after: dict | None
    created_at: datetime
    chain_seq: int | None = None
    prev_hash: str | None = None
    entry_hash: str | None = None

    model_config = {"from_attributes": True}


class DeadLetterOut(BaseModel):
    id: str
    source: str
    task_name: str
    document_id: str | None
    kb_id: str | None
    workspace_id: str | None
    payload: dict
    error: str | None
    failed_stage: str | None
    retry_count: int
    status: str
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    model_config = {"from_attributes": True}


class ReplayRequest(BaseModel):
    from_stage: str | None = None


class ResumeFromStageRequest(BaseModel):
    from_stage: str = Field(
        default="parsing",
        pattern="^(parsing|chunking|embedding|indexing)$",
    )


# ---- 对话 ----
class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None  # 为空则新建会话
    kb_id: str | None = None            # 为空则检索该用户全部文档
    mode: str = Field(default="rag", pattern="^(rag|agent)$")  # rag=固定管道 / agent=工具循环
    retrieval_profile: str = Field(
        default="hybrid",
        pattern="^(vector|hybrid|hybrid_rerank|parent_child|multi_query)$",
    )


class ConversationRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ConversationOut(BaseModel):
    id: str
    title: str
    kb_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    role: str
    content: str
    sources: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- 统计 ----
class StatsOverview(BaseModel):
    kb_count: int
    doc_count: int
    chunk_count: int
    conversation_count: int
    question_count: int
    prompt_tokens: int
    completion_tokens: int
    avg_first_token_ms: int
    avg_total_ms: int


class DailyUsage(BaseModel):
    date: str
    questions: int
    prompt_tokens: int
    completion_tokens: int
