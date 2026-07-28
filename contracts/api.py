"""HTTP API contracts for the FastAPI agent adapter."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts.agent import RunResult

MAX_TEXT_CHARS = 12_000
MAX_TURNS_LIMIT = 20


class GatewayMode(StrEnum):
    FAKE = "fake"
    HTTP = "http"


class AgentProfile(StrEnum):
    GENERIC = "generic"
    TRANSLATION = "translation"


class AgentRunRequest(BaseModel):
    """One isolated agent run.

    Runs deliberately do not carry conversation history. The current AgentLoop
    contract is single-request, and the API makes that boundary explicit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(
        strict=True,
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description="The task or question to send to the agent.",
        examples=["現在台北幾點？"],
    )
    gateway: GatewayMode = Field(
        default=GatewayMode.FAKE,
        description="Use the deterministic test double or the configured model gateway.",
    )
    profile: AgentProfile = Field(
        default=AgentProfile.GENERIC,
        description="Optional capability-specific prompt and policies.",
    )
    max_turns: int = Field(default=6, strict=True, ge=1, le=MAX_TURNS_LIMIT)

    @field_validator("text")
    @classmethod
    def text_must_contain_non_whitespace(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("text must not be blank")
        return stripped


class AgentRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    created_at: datetime
    duration_ms: int = Field(ge=0)
    result: RunResult


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = "ok"
    version: str


class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    http_gateway_configured: bool
    gateways: list[GatewayMode]
    profiles: list[AgentProfile]
    tools: list[str]


class APIErrorDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class APIErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    error: APIErrorDetail
