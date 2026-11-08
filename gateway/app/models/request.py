"""Validate the text-only chat request supported by Tollgate's first version."""

from typing import Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# These are application limits; character counts are not token counts or byte counts.
MAX_MESSAGES = 50
MAX_MESSAGE_CHARACTERS = 8_000
MAX_TOTAL_CHARACTERS = 32_000
MAX_OUTPUT_TOKENS = 4_096


class Message(BaseModel):
    """Allow a supported role and a non-empty text message."""

    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARACTERS)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        """Reject whitespace-only content while preserving valid prompt spacing."""
        if not value.strip():
            raise ValueError("Message content must contain non-whitespace text.")
        return value


class ChatCompletionRequest(BaseModel):
    """Reject unknown fields and incorrect types instead of silently converting them."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        json_schema_extra={
            "examples": [
                {
                    "model": "sim-basic",
                    "messages": [
                        {"role": "user", "content": "Explain an API gateway."}
                    ],
                    "max_completion_tokens": 256,
                    "stream": False,
                }
            ]
        },
    )

    # This validates the name's format; provider routing will enforce model access later.
    model: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"
    )
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)
    max_completion_tokens: int = Field(
        default=256,
        ge=1,
        le=MAX_OUTPUT_TOKENS,
        validation_alias=AliasChoices("max_completion_tokens", "max_tokens"),
    )
    stream: bool = False
    stream_options: dict[Literal["include_usage"], bool] | None = None

    @model_validator(mode="after")
    def check_total_content(self) -> Self:
        """Limit combined message text, even when individual messages are valid."""
        if (
            sum(len(message.content) for message in self.messages)
            > MAX_TOTAL_CHARACTERS
        ):
            raise ValueError(
                f"Combined message content exceeds {MAX_TOTAL_CHARACTERS} characters."
            )
        return self


class CustomerCreate(BaseModel):
    """Accept a readable local customer name without hidden extra fields."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)


class KeyCreate(BaseModel):
    """Bind a newly issued key to one customer and an explicit model allow-list."""

    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: str = Field(min_length=1, max_length=64)
    label: str = Field(default="Application key", min_length=1, max_length=80)
    allowed_models: list[str] = Field(min_length=1, max_length=20)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class CreditGrant(BaseModel):
    """Require decimal-string credits and a unique retry reference, not a float."""

    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: str = Field(min_length=1, max_length=64)
    amount_usd: str = Field(min_length=1, max_length=20)
    reference: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class SpendCap(BaseModel):
    """Use null to remove a lifetime cap; settled spending is never reset."""

    model_config = ConfigDict(extra="forbid", strict=True)
    customer_id: str = Field(min_length=1, max_length=64)
    amount_usd: str | None = Field(default=None, max_length=20)


class Resolution(BaseModel):
    """Require evidence for an operator-approved correction of an unknown outcome."""

    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["record_usage", "release"]
    evidence: str = Field(min_length=12, max_length=1000)
    input_tokens: int = Field(default=0, ge=0, le=100000)
    output_tokens: int = Field(default=0, ge=0, le=100000)
    cached_tokens: int = Field(default=0, ge=0, le=100000)
    cache_write_tokens: int = Field(default=0, ge=0, le=100000)


class FreezeCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    frozen: bool
    reason: str = Field(min_length=8, max_length=300)


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    amount_usd: Literal["5.00", "10.00", "25.00", "50.00"]
    reference: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class AllowanceRequest(BaseModel):
    """Ask for an offline spending allowance that the standby relay can honour."""

    model_config = ConfigDict(extra="forbid", strict=True)
    amount_usd: str = Field(min_length=1, max_length=20)
    ttl_minutes: int = Field(default=240, ge=5, le=10_080)
    models: list[str] = Field(min_length=1, max_length=20)
    include_direct: bool = False


class OfflineRecord(BaseModel):
    """One request served by the relay; only counts are accepted, never money or text."""

    model_config = ConfigDict(extra="forbid", strict=True)
    record_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    model: str = Field(min_length=1, max_length=80)
    input_tokens: int = Field(ge=0, le=10_000_000)
    output_tokens: int = Field(ge=0, le=10_000_000)
    cached_tokens: int = Field(default=0, ge=0, le=10_000_000)
    cache_write_tokens: int = Field(default=0, ge=0, le=10_000_000)
    created_at: float = Field(gt=0)
    sig: str = Field(default="", max_length=64)


class OfflineSync(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    allowance_id: str = Field(min_length=1, max_length=64)
    records: list[OfflineRecord] = Field(max_length=500)
    close: bool = False


class ContactUpdate(BaseModel):
    """Where outage and recovery notices are sent. Either field may be empty."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    email: str | None = Field(
        default=None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    phone: str | None = Field(default=None, max_length=20, pattern=r"^\+[1-9][0-9]{6,14}$")
