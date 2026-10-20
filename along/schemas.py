from typing import Literal
from pydantic import BaseModel, Field, model_validator


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


class ResearchDecision(BaseModel):
    action: Literal["direct", "search", "clarify"]
    reason: str = Field(min_length=1, max_length=1000)
    information_gap: str = Field(default="", max_length=1000)
    query: str = Field(default="", max_length=500)
    questions: list[str] = Field(default_factory=list, max_length=5)
    source_ids: list[str] = Field(default_factory=list, max_length=6)
    verification_required: bool = False

    @model_validator(mode="after")
    def usable_decision(self):
        if self.action == "search" and (not self.query.strip() or not self.information_gap.strip()):
            raise ValueError("搜索必须有具体查询和明确资料缺口")
        if self.action == "clarify" and not any(q.strip() for q in self.questions):
            raise ValueError("澄清必须给出用户能回答的问题")
        if self.action == "direct" and self.verification_required and not self.source_ids:
            raise ValueError("关键操作需要核验时，必须选择适用资料、搜索或先澄清")
        return self


class Route(BaseModel):
    intent: Literal["ask", "start", "pause", "resume", "finish", "clarify"]
    goal: str = ""
    related_to_task: bool = False
    needs_history: bool = False
    clarification: str = ""


class SearchQuery(BaseModel):
    start_time: float | None = None
    end_time: float | None = None
    queries: list[str] = Field(default_factory=list, max_length=4)
    objects: list[str] = Field(default_factory=list, max_length=10)


class MemoryCall(BaseModel):
    tool: Literal["browse", "search", "open", "current"]
    query: str = Field(default="", max_length=500)
    event_id: str | None = Field(default=None, max_length=100)
    start_time: float | None = Field(default=None, allow_inf_nan=False)
    end_time: float | None = Field(default=None, allow_inf_nan=False)
    before_time: float | None = Field(default=None, allow_inf_nan=False)
    step_id: str | None = Field(default=None, max_length=60)
    plan_revision: int | None = Field(default=None, ge=0)
    seconds: int = Field(default=8, ge=1, le=120)
    frames: int = Field(default=8, ge=2, le=12)

    @model_validator(mode="after")
    def valid_range(self):
        if self.start_time is not None and self.end_time is not None and self.start_time > self.end_time:
            raise ValueError("记忆时间范围反向")
        if self.tool == "search" and not self.query.strip():
            raise ValueError("搜索记忆需要查询内容")
        return self


class Answer(BaseModel):
    memory: MemoryCall | None = None
    research: ResearchDecision | None = None
    reply: str = Field(max_length=5000)
    observed: list[str] = Field(default_factory=list, max_length=10)
    hypotheses: list[str] = Field(default_factory=list, max_length=10)
    missing: list[str] = Field(default_factory=list, max_length=10)
    blocking_missing: list[str] = Field(default_factory=list, max_length=1)
    checks: list[str] = Field(default_factory=list, max_length=10)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    source_ids: list[str] = Field(default_factory=list, max_length=10)
    suggestion: str | None = Field(default=None, max_length=2000)


class FrameSelection(BaseModel):
    frame_ids: list[str] = Field(default_factory=list, max_length=6)


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
    action: Literal["confirm_plan", "pause", "resume", "finish", "step", "suggestion", "note", "issue"]
    target: str = Field(default="", max_length=100)
    value: str = Field(default="", max_length=2000)
