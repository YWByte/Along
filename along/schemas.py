from typing import Literal
from pydantic import BaseModel, Field


class Observation(BaseModel):
    claim: str = Field(max_length=1000)
    frame_ids: list[str] = Field(default_factory=list, max_length=12)


class EventDraft(BaseModel):
    summary: str = Field(max_length=2000)
    objects: list[str] = Field(default_factory=list, max_length=20)
    observations: list[Observation] = Field(default_factory=list, max_length=20)
    inferences: list[str] = Field(default_factory=list, max_length=10)
    unknowns: list[str] = Field(default_factory=list, max_length=10)


class Step(BaseModel):
    id: str = Field(max_length=60)
    title: str = Field(max_length=300)
    instruction: str = Field(max_length=2000)
    checks: list[str] = Field(default_factory=list, max_length=10)
    source_ids: list[str] = Field(default_factory=list, max_length=10)


class PlanDraft(BaseModel):
    title: str = Field(max_length=300)
    prerequisites: list[str] = Field(default_factory=list, max_length=15)
    steps: list[Step] = Field(min_length=1, max_length=30)
    cautions: list[str] = Field(default_factory=list, max_length=15)
    questions: list[str] = Field(default_factory=list, max_length=10)


class Route(BaseModel):
    intent: Literal["ask", "start", "pause", "resume", "finish", "clarify"]
    goal: str = ""
    related_to_task: bool = False
    needs_history: bool = False
    clarification: str = ""


class SearchQuery(BaseModel):
    queries: list[str] = Field(default_factory=list, max_length=4)
    objects: list[str] = Field(default_factory=list, max_length=10)


class Answer(BaseModel):
    reply: str = Field(max_length=5000)
    observed: list[str] = Field(default_factory=list, max_length=10)
    hypotheses: list[str] = Field(default_factory=list, max_length=10)
    missing: list[str] = Field(default_factory=list, max_length=10)
    checks: list[str] = Field(default_factory=list, max_length=10)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    source_ids: list[str] = Field(default_factory=list, max_length=10)
    suggestion: str | None = Field(default=None, max_length=2000)


class Rank(BaseModel):
    event_ids: list[str] = Field(default_factory=list, max_length=8)


class ChatInput(BaseModel):
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    conversation_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    text: str = Field(min_length=1, max_length=4000)
    frame_id: str | None = None
    mode: Literal["auto", "instant", "task"] = "auto"
    task_id: str | None = None


class Mutation(BaseModel):
    version: int = Field(ge=1)
    action: Literal["confirm_plan", "pause", "resume", "finish", "step", "suggestion", "note"]
    target: str = Field(default="", max_length=100)
    value: str = Field(default="", max_length=2000)
