"""Versioned, model-independent cleanup contracts. No subjective classifier lives here."""
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .core import StrictModel

ContentType = Literal["POST", "REPLY", "QUOTE", "REPOST", "DM"]
ActionType = Literal["DELETE_POST", "UNDO_REPOST", "DELETE_DM"]
NumericID = str


class Probabilities(StrictModel):
    KEEP: float = Field(ge=0, le=1, allow_inf_nan=False)
    DELETE: float = Field(ge=0, le=1, allow_inf_nan=False)
    REVIEW: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def sum_to_one(self):
        if abs(self.KEEP + self.DELETE + self.REVIEW - 1) > 0.000001:
            raise ValueError("Decision probabilities must sum to one")
        return self


class Decision(StrictModel):
    label: Literal["KEEP", "DELETE", "REVIEW"]
    probabilities: Probabilities | None = None
    engine: str | None = Field(default=None, max_length=100)
    model_version: str | None = Field(default=None, max_length=100)
    reason: str | None = Field(default=None, max_length=2000)


class Candidate(StrictModel):
    schema_version: Literal["1"] = "1"
    candidate_id: str
    account_id: str
    content_id: str
    content_type: ContentType
    author_id: str | None = None
    text: str | None = None
    created_at: datetime | None = None
    reply_to: str | None = None
    quote_id: str | None = None
    repost_of: str | None = None
    conversation_id: str | None = None
    participant_ids: list[str] = Field(default_factory=list)
    engagement: dict[str, int] | None = None
    media: list[dict] = Field(default_factory=list)
    related_posts: list[dict] = Field(default_factory=list)
    author: dict | None = None
    url: str | None = None
    observed_at: float
    source: Literal["x_api_v2", "x_session", "owner_browser_observation", "public_x_embed", "publication_receipt"] = "x_api_v2"


class ProposedAction(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=128)
    action: ActionType
    decision: Decision | None = None


class ProtectionPolicy(StrictModel):
    protected_ids: list[str] = Field(default_factory=list, max_length=10000)
    protected_dm_conversations: list[str] = Field(default_factory=list, max_length=10000)
    protect_pinned: bool = True
    min_age_seconds: int = Field(default=0, ge=0)
    engagement_thresholds: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_thresholds(self):
        known = {"like_count", "reply_count", "retweet_count", "quote_count", "impression_count", "bookmark_count"}
        if set(self.engagement_thresholds) - known or any(v < 0 for v in self.engagement_thresholds.values()):
            raise ValueError("Use nonnegative thresholds for supported public engagement metrics")
        return self
