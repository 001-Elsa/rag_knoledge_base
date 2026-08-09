"""Pydantic 请求/响应模型。"""
import re
import unicodedata
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


# ---- 认证 ----
class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    phone: str
    password: str = Field(min_length=6, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        return unicodedata.normalize("NFKC", str(value)).strip()

    @field_validator("phone", mode="before")
    @classmethod
    def normalize_phone(cls, value):
        phone = unicodedata.normalize("NFKC", str(value)).strip()
        phone = re.sub(r"[\s-]+", "", phone)
        if phone.startswith("+86"):
            phone = phone[3:]
        elif phone.startswith("0086"):
            phone = phone[4:]
        if not re.fullmatch(r"1[3-9]\d{9}", phone):
            raise ValueError("请输入有效的中国大陆手机号")
        return phone


class LoginRequest(BaseModel):
    username: str
    password: str

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        return unicodedata.normalize("NFKC", str(value)).strip()


class RegistrationResponse(BaseModel):
    message: str
    username: str


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
    phone: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class APIKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    expires_at: datetime | None = None


class APIKeyCreatedResponse(BaseModel):
    id: str
    name: str
    key: str
    prefix: str
    expires_at: datetime | None = None


class APIKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
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
    source_type: str = "upload"
    source_url: str | None = None
    source_metadata: dict = Field(default_factory=dict)
    department: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    chunk_strategy: str = "recursive"
    error: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentMetadataUpdateRequest(BaseModel):
    department: str | None = Field(default=None, max_length=128)
    tags: list[str] = Field(default_factory=list, max_length=30)
    source_url: str | None = Field(default=None, max_length=2048)


class ExternalSourceImportRequest(BaseModel):
    kb_id: str
    source_type: str = Field(pattern="^(web|api|database)$")
    name: str = Field(min_length=1, max_length=255)
    url: str
    query: str | None = Field(default=None, max_length=5000)
    headers: dict[str, str] = Field(default_factory=dict)
    json_path: str | None = Field(default=None, max_length=255)
    max_rows: int = Field(default=1000, ge=1, le=10000)


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
    from_stage: str | None = Field(
        default=None,
        description="失败阶段标签；用于审计和 checkpoint 复用提示，不表示跳过之前阶段",
    )


class ResumeFromStageRequest(BaseModel):
    from_stage: str = Field(
        default="parsing",
        pattern="^(parsing|chunking|embedding|indexing)$",
        description="失败阶段标签；Worker 仍从解析开始，并自动复用匹配的 Embedding checkpoint",
    )


# ---- 对话 ----
class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None  # 为空则新建会话
    kb_id: str | None = None            # 为空则检索该用户全部文档
    mode: str = Field(default="rag", pattern="^(rag|agent)$")  # rag=固定管道 / agent=工具循环
    retrieval_profile: str = Field(
        default="auto",
        pattern="^(auto|vector|flat|keyword|hybrid|hybrid_rerank|parent_child|multi_query|hyde|graph)$",
    )
    document_types: list[str] = Field(default_factory=list, max_length=20)
    source_types: list[str] = Field(default_factory=list, max_length=20)
    departments: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=30)
    sections: list[str] = Field(default_factory=list, max_length=50)
    created_after: datetime | None = None
    created_before: datetime | None = None
    response_format: str = Field(default="markdown", pattern="^(markdown|json)$")
    response_schema: dict | None = None


class FeedbackRequest(BaseModel):
    conversation_id: str
    message_id: str | None = None
    rating: int = Field(ge=-1, le=1)
    reason: str | None = Field(default=None, max_length=64)
    comment: str | None = Field(default=None, max_length=1000)


class InteractionRequest(BaseModel):
    action: str = Field(pattern="^(copy|regenerate|followup|research)$")
    conversation_id: str | None = None


class ConversationRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ConversationOut(BaseModel):
    id: str
    title: str
    kb_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: str
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
    estimated_cost_usd: float = 0.0


class DailyUsage(BaseModel):
    date: str
    questions: int
    prompt_tokens: int
    completion_tokens: int
