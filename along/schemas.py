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
    look_for: list[str] = Field(default_factory=list, max_length=3)
    source_ids: list[str] = Field(default_factory=list, max_length=10)


class PlanDraft(BaseModel):
    title: str = Field(max_length=300)
    prerequisites: list[str] = Field(default_factory=list, max_length=15)
    steps: list[Step] = Field(min_length=1, max_length=30)
    cautions: list[str] = Field(default_factory=list, max_length=15)
    questions: list[str] = Field(default_factory=list, max_length=1)


class PlanAudit(BaseModel):
    action: Literal["keep", "revise"]
    issues: list[str] = Field(default_factory=list, max_length=5)
    revised_plan: PlanDraft | None = None

    @model_validator(mode="after")
    def usable_revision(self):
        if self.action == "revise" and self.revised_plan is None:
            raise ValueError("修改方案必须提供完整修正版")
        return self


Scenario = Literal["installation", "exercise", "general"]


class ReviewFocus(BaseModel):
    temporal_scope: Literal[
        "unspecified", "current_step", "last_completed", "after_previous_answer", "recent", "explicit"
    ] = "unspecified"
    subject: str = Field(max_length=500)
    step_id: str | None = Field(default=None, max_length=60)
    plan_revision: int | None = Field(default=None, ge=0)


class ResearchDecision(BaseModel):
    scenario: Scenario = "general"
    action: Literal["direct", "search", "clarify", "stop"]
    reason: str = Field(min_length=1, max_length=1000)
    information_gap: str = Field(default="", max_length=1000)
    query: str = Field(default="", max_length=500)
    questions: list[str] = Field(default_factory=list, max_length=1)
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
    scenario: Scenario = "general"
    review_focus: ReviewFocus | None = None
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
    allow_older: bool = False
    reason: str = Field(default="", max_length=500)
    replace_images: bool = False
    tool: Literal["browse", "search", "open", "current"]
    query: str = Field(default="", max_length=500)
    event_id: str | None = Field(default=None, max_length=100)
    start_time: float | None = Field(default=None, allow_inf_nan=False)
    end_time: float | None = Field(default=None, allow_inf_nan=False)
    before_time: float | None = Field(default=None, allow_inf_nan=False)
    offset: int = Field(default=0, ge=0, le=1000000)
    step_id: str | None = Field(default=None, max_length=60)
    plan_revision: int | None = Field(default=None, ge=0)
    seconds: int = Field(default=8, ge=1, le=120)
    frames: int = Field(default=8, ge=2, le=24)
    sheet: bool = False

    @model_validator(mode="after")
    def valid_range(self):
        if self.frames > 12 and not self.sheet:
            raise ValueError("超过12帧时须使用 sheet=true 拼成一张带时间标注的拼图")
        if self.allow_older and (self.tool != "open" or not self.reason.strip()):
            raise ValueError("向更早记录追溯只适用于open，且必须说明原因")
        if self.start_time is not None and self.end_time is not None and self.start_time > self.end_time:
            raise ValueError("记忆时间范围反向")
        if self.tool == "search" and not self.query.strip():
            raise ValueError("搜索记忆需要查询内容")
        return self


class VisualChange(BaseModel):
    claim: str = Field(min_length=1, max_length=800)
    frame_ids: list[str] = Field(min_length=2, max_length=4)


class VisualReview(BaseModel):
    visibility: Literal["clear", "limited", "unusable"]
    changes: list[VisualChange] = Field(default_factory=list, max_length=4)
    summary: str = Field(min_length=1, max_length=1200)
    limitations: list[str] = Field(default_factory=list, max_length=3)
    needs_detail: bool = False


class CriterionResult(BaseModel):
    criterion: str = Field(min_length=1, max_length=300)
    status: Literal["met", "not_met", "unseen"]
    frame_ids: list[str] = Field(default_factory=list, max_length=6)


Verdict = Literal["ok", "adjust", "unclear", "none"]


class AnswerAudit(BaseModel):
    action: Literal["keep", "revise"]
    unsupported_claims: list[str] = Field(default_factory=list, max_length=4)
    corrected_reply: str = Field(default="", max_length=1600)
    corrected_headline: str = Field(default="", max_length=80)
    corrected_verdict: Verdict = "none"
    corrected_criteria: list[CriterionResult] = Field(default_factory=list, max_length=4)
    corrected_suggestion: str | None = Field(default=None, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def usable_correction(self):
        if self.action == "revise" and not self.corrected_reply.strip():
            raise ValueError("修改回答必须给出修正后的简短回答")
        return self


class Answer(BaseModel):
    memory: MemoryCall | None = None
    research: ResearchDecision | None = None
    reply: str = Field(max_length=5000)
    headline: str = Field(default="", max_length=80)
    verdict: Verdict = "none"
    criteria: list[CriterionResult] = Field(default_factory=list, max_length=4)
    observed: list[str] = Field(default_factory=list, max_length=10)
    hypotheses: list[str] = Field(default_factory=list, max_length=10)
    missing: list[str] = Field(default_factory=list, max_length=10)
    blocking_missing: list[str] = Field(default_factory=list, max_length=1)
    checks: list[str] = Field(default_factory=list, max_length=10)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    source_ids: list[str] = Field(default_factory=list, max_length=10)
    suggestion: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def one_tool_per_turn(self):
        if self.memory is not None and self.research is not None:
            raise ValueError("每轮只允许申请一个工具：memory 与 research 不能同时设置")
        return self


class MemoryRequest(Answer):
    memory: MemoryCall
    research: None = None
    reply: str = ""

    @model_validator(mode="after")
    def sequence_tool(self):
        if self.memory.tool == "current":
            raise ValueError("过程证据需要open片段，或先browse/search定位，不能只取current")
        return self


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
