"""Supported text-only response contracts; simulator usage is labelled in headers."""

from typing import Literal, Self
from pydantic import BaseModel, ConfigDict, Field, model_validator


class HealthResponse(BaseModel):
    """Report process liveness without claiming the simulator is reachable."""

    status: Literal["ok"] = "ok"
    service: str
    mode: Literal["simulator"] = "simulator"


class AssistantMessage(BaseModel):
    """Accept only a text assistant response in the supported first version."""

    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["assistant"] = "assistant"
    content: str = Field(max_length=32_000)


class Choice(BaseModel):
    """Describe the single answer requested from the simulator."""

    model_config = ConfigDict(extra="forbid", strict=True)
    index: Literal[0] = 0
    message: AssistantMessage
    finish_reason: Literal["stop", "length", "content_filter"]


class TokenUsage(BaseModel):
    """Validate reported counts; this class never tokenizes text itself."""

    model_config = ConfigDict(extra="forbid", strict=True)
    prompt_tokens: int = Field(ge=0, le=100_000)
    completion_tokens: int = Field(ge=0, le=100_000)
    total_tokens: int = Field(ge=0, le=200_000)

    @model_validator(mode="after")
    def check_total(self) -> Self:
        """Reject contradictory totals rather than silently correcting provider data."""
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("Total tokens must equal input plus output tokens.")
        return self


class ChatCompletionResponse(BaseModel):
    """Validate the deliberately narrow non-streaming simulator response."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=128)
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(ge=0)
    model: str = Field(min_length=1, max_length=128)
    choices: list[Choice] = Field(min_length=1, max_length=1)
    usage: TokenUsage
