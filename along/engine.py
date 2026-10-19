import asyncio
import hashlib
import json
import time

from along.memory import Memory
from along.providers.vlm import ProviderError, VLMProvider
from along.providers.web import WebProvider
from along.schemas import Answer, ChatInput, Mutation, PlanDraft, Route
from along.store import Conflict, Store, dump, uid


class Engine:
    def __init__(self, store: Store, memory: Memory, vlm: VLMProvider, web: WebProvider):
        self.store, self.memory, self.vlm, self.web = store, memory, vlm, web
        self.interaction = asyncio.Lock()

    async def prepare(self, task_id: str, supplied_url: str | None = None, revision_reason: str = ""):
        task = self.store.task(task_id)
        if task["status"] == "ended":
            raise Conflict("任务已结束")
        if task["status"] == "active":
            raise Conflict("调整计划前请暂停任务，避免操作表与现场执行混淆")
        version = task["version"]
        def mark(row, state, plan, db):
            state.update(preparing=True, error=None)
            return row["status"], state, plan, "plan_preparing", {}
        task = self.store.change(task_id, version, mark)
        version = task["version"]
        try:
            sources = self.store.rows("SELECT * FROM sources WHERE task_id=?", (task_id,))
            warnings = []
            if supplied_url:
                content = await self.web.fetch(supplied_url)
                sources = [self.save_source(task_id, supplied_url, supplied_url, content)]
            elif not sources:
                results = await self.web.search(task["goal"] + " 官方说明 操作步骤 所需条件")
                for result in results[:3]:
                    try:
                        content = await self.web.fetch(result["url"])
                        sources.append(self.save_source(task_id, result["title"], result["url"], content))
                        if len(sources) >= 2:
                            break
                    except (ProviderError, ValueError):
                        warnings.append("一个搜索结果未能获取正文，未将其当作操作依据")
                if not sources:
                    raise ProviderError("未获取到可用原始资料，请提供说明书网址或上传资料后重试")
            notes = task["state"].get("notes", [])[-10:]
            plan = await self.vlm.structured(
                "依据用户目标及已获取资料生成待用户确认的操作表。不要混合不同型号或不同配方的用量与步骤。"
                "选择一套适用资料，其余仅供比较。适用型号、材料、工具或安全条件不足时在 questions 中提问。"
                "每步检查条件必须具体，但不能认证安全。步骤 ID 使用 s1、s2 等。引用仅可使用下列来源 ID。\n"
                + dump({"goal": task["goal"], "user_notes": notes, "revision_reason": revision_reason,
                        "previous_plan": task["plan"], "sources": [
                            {"id": s["id"], "url": s["url"], "content": s["content"][:16000]} for s in sources]}),
                PlanDraft)
            allowed = {s["id"] for s in sources}
            if len({step.id for step in plan.steps}) != len(plan.steps):
                raise ProviderError("计划存在重复步骤 ID，未采用")
            for step in plan.steps:
                if not step.source_ids or not set(step.source_ids) <= allowed:
                    raise ProviderError("操作步骤缺少有效资料引用，未采用")
            def ready(row, state, old_plan, db):
                state.update(preparing=False, error=None, plan_revision=state["plan_revision"]+1,
                             plan_confirmed=False, warnings=warnings,
                             plan_source_ids=list(dict.fromkeys(sid for step in plan.steps for sid in step.source_ids)))
                # A revised plan must not inherit completion flags from different instructions.
                state["steps"] = {s.id: "unknown" for s in plan.steps}
                return row["status"], state, plan.model_dump(), "plan_proposed", {
                    "revision": state["plan_revision"], "reason": revision_reason,
                    "previous_plan": old_plan, "new_plan": plan.model_dump()}
            return self.store.change(task_id, version, ready)
        except BaseException as exc:
            error_message = str(exc) if isinstance(exc, (ValueError, ProviderError)) else "计划准备被中断，可重试"
            try:
                current = self.store.task(task_id)
                if current["version"] == version:
                    def fail(row, state, old_plan, db):
                        state.update(preparing=False, error=error_message)
                        return row["status"], state, old_plan, "plan_failed", {"error": state["error"]}
                    self.store.change(task_id, version, fail)
            except KeyError:
                pass
            raise

    def save_source(self, task_id, title, url, content):
        source_id = uid("src")
        with self.store.tx() as db:
            db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?)",
                       (source_id, task_id, title[:500], url, content[:40000], time.time()))
        return self.store.one("SELECT * FROM sources WHERE id=?", (source_id,))

    def mutate(self, task_id, change: Mutation):
        def apply(row, state, plan, db):
            status = row["status"]
            if state.get("preparing") and change.action != "finish":
                raise Conflict("计划正在准备，请等待或结束任务")
            if status == "ended":
                raise Conflict("任务已结束，只能查看或删除")
            detail = change.model_dump()
            if change.action == "confirm_plan":
                if not plan or status not in ("draft", "paused"):
                    raise Conflict("没有可确认的计划")
                if plan.get("questions") and change.value != "已核对适用条件":
                    raise Conflict("请先核对计划中的待确认条件；不符合时补充信息并重新生成")
                state["plan_confirmed"] = True
                status = "active"
            elif change.action == "pause":
                if status != "active":
                    raise Conflict("只有进行中的任务可以暂停")
                status = "paused"
            elif change.action == "resume":
                if status != "paused" or not state.get("plan_confirmed"):
                    raise Conflict("请先确认当前计划再继续")
                status = "active"
            elif change.action == "finish":
                status = "ended"
                state["preparing"] = False
            elif change.action == "step":
                if not plan or change.target not in state["steps"]:
                    raise ValueError("步骤不存在")
                if change.value not in ("unknown", "doing", "user_reported_done", "needs_recheck"):
                    raise ValueError("无效步骤状态")
                state["steps"][change.target] = change.value
                detail["provenance"] = "user_reported_not_independently_verified"
            elif change.action == "note":
                if not change.value.strip():
                    raise ValueError("备注不能为空")
                state["notes"] = (state.get("notes", []) + [{"text": change.value, "at": time.time(),
                                                          "provenance": "user"}])[-30:]
            elif change.action == "issue":
                issue = next((i for i in state["open_issues"] if i["id"] == change.target), None)
                if not issue or change.value not in ("open", "resolved_by_user"):
                    raise ValueError("问题不存在或状态无效")
                issue["status"] = change.value
                detail["provenance"] = "user"
            elif change.action == "suggestion":
                suggestion = db.execute("SELECT * FROM suggestions WHERE id=? AND task_id=?",
                                        (change.target, task_id)).fetchone()
                if not suggestion:
                    raise KeyError("建议不存在")
                allowed = {"proposed": {"accepted", "rejected"}, "accepted": {"executed", "rejected"},
                           "executed": {"observed_success", "observed_failure", "needs_recheck"},
                           "observed_success": {"needs_recheck"}, "observed_failure": {"needs_recheck"},
                           "needs_recheck": {"observed_success", "observed_failure"}}
                if change.value not in allowed.get(suggestion["status"], set()):
                    raise Conflict("不能跳过接受、执行和结果确认阶段")
                history = json.loads(suggestion["history"]) + [{"status": change.value, "at": time.time(),
                                                               "provenance": "user"}]
                db.execute("UPDATE suggestions SET status=?,history=? WHERE id=?",
                           (change.value, dump(history), change.target))
            return status, state, plan, change.action, detail
        return self.store.change(task_id, change.version, apply)

    async def chat(self, request: ChatInput):
        fingerprint = hashlib.sha256(dump(request.model_dump()).encode()).hexdigest()
        cached = self.store.begin_request(request.request_id, fingerprint)
        if cached is not None:
            return cached
        if self.interaction.locked():
            self.store.complete_request(request.request_id)
            raise Conflict("已有问答或计划生成正在处理，请等待完成")
        async with self.interaction:
            self.memory.user_busy = True
            try:
                result = await self._chat(request)
                self.store.complete_request(request.request_id, result)
                return result
            except BaseException:
                self.store.complete_request(request.request_id)
                raise
            finally:
                self.memory.user_busy = False

    async def _chat(self, request):
        task = self.store.task(request.task_id) if request.task_id else self.store.current()
        images = []
        if request.frame_id:
            row, path = self.memory.media.path(request.frame_id)
            if row["kind"] != "frame" or (row["conversation_id"] != request.conversation_id and
                                           (not task or row["task_id"] != task["id"])):
                raise ValueError("图片不属于当前会话或任务")
            if image := self.memory.image(request.frame_id):
                images = [image]
        history = self.store.messages(request.conversation_id)
        route = Route(intent="ask")
        if request.mode != "instant":
            task_history = self.store.messages(request.conversation_id, task_id=task["id"]) if task else []
            route = await self.vlm.structured(
                "判断用户意图，不执行操作。start 仅当用户明确请求一起开始或带他完成一件事，goal 必须有具体任务；"
                "只有'我们开始吧'且上下文没有任务时用 clarify。普通图片提问用 ask。"
                "已有任务时无关问题 related_to_task=false，不改变任务。"
                "需要回看之前的操作才设置 needs_history=true。继续、暂停、结束用相应 intent。\n"
                + dump({"text": request.text, "recent_instant": history, "recent_task": task_history,
                        "task": {"goal": task["goal"], "status": task["status"]} if task else None}), Route)
        if route.intent == "clarify" or (route.intent == "start" and not route.goal.strip()):
            result = {"kind": "clarify", "reply": route.clarification or "你想一起完成什么事情？"}
        elif route.intent == "start":
            if task and task["status"] != "ended":
                result = {"kind": "clarify", "reply": "已有未结束任务。继续当前任务，或先结束它再开始新任务。",
                          "task_id": task["id"]}
            else:
                created = self.store.create_task(route.goal)
                self.store.message(request.conversation_id, created["id"], "user", request.text)
                try:
                    prepared = await self.prepare(created["id"])
                    result = {"kind": "plan", "reply": "操作表已准备，请核对资料、适用条件和步骤，再确认开始。",
                              "task_id": prepared["id"], "version": prepared["version"]}
                except (ProviderError, ValueError) as exc:
                    result = {"kind": "plan_error", "reply": str(exc), "task_id": created["id"]}
                self.store.message(request.conversation_id, created["id"], "assistant", result["reply"], result)
                return result
        elif route.intent in ("pause", "resume", "finish"):
            result = {"kind": "action", "action": route.intent, "task_id": task["id"] if task else None,
                      "reply": "请使用任务面板的对应按钮确认操作。" if task else "目前没有进行中的任务。"}
        else:
            related = bool(task and request.mode != "instant" and route.related_to_task)
            events, warnings = [], []
            if related and route.needs_history:
                events, historical_images, warnings = await self.memory.retrieve(task["id"], request.text)
                seen = {im.id for im in images}
                images.extend(im for im in historical_images if im.id not in seen)
            elif related and not images:
                images = self.memory.recent_images(task["id"], 2)
            if not images and not related:
                previous = self.store.rows(
                    "SELECT data FROM messages WHERE conversation_id=? AND task_id IS NULL AND role='assistant' "
                    "AND data IS NOT NULL ORDER BY created DESC LIMIT 1", (request.conversation_id,))
                if previous:
                    frame_ids = json.loads(previous[0]["data"]).get("retrieval", {}).get("frames", [])
                    images = [im for fid in frame_ids[:2] if (im := self.memory.image(fid))]
            images = images[:self.memory.settings.max_images]
            sources = self.store.rows("SELECT * FROM sources WHERE task_id=?", (task["id"],)) if related else []
            if related:
                adopted = set(task["state"].get("plan_source_ids", []))
                sources = [s for s in sources if s["id"] in adopted]
            compact_state = None
            if related:
                compact_state = {**task["state"], "notes": task["state"].get("notes", [])[-6:]}
            context = {
                "question": request.text,
                "recent_messages": self.store.messages(request.conversation_id,
                                                       task_id=task["id"] if related else None),
                "task_state": compact_state,
                "adopted_plan": task["plan"] if related else None,
                "event_indexes_not_facts": [{"id": e["id"], "start": e["start"],
                                             "data": json.loads(e["data"])} for e in events],
                "reference_sources": [{"id": s["id"], "url": s["url"], "content": s["content"][:6000]}
                                      for s in sources[-3:]],
                "recording_limits": "抽帧可能漏动作，画面可能遮挡。请按证据时间区分历史与当前。没有画面时不要声称看见。",
            }
            answer = await self.vlm.structured(
                "回答用户问题。reply 先简短直接回答，证据、猜测、缺失条件与下一步检查分别列出。"
                "普通识物问题直接回答即可，无需强行给出调整建议或无关检查，没有内容的列表留空。"
                "evidence_ids 只能引用这次实际看到的图片 ID；source_ids 只能引用给定原始资料 ID。"
                "如果给出需要用户执行的新调整方案，放入 suggestion；普通解释不产生待执行建议。"
                "参考资料不能证明用户已执行；任务状态中的完成只是用户自报。禁止用历史摘要冒充当前观察。\n"
                + dump(context), Answer, images)
            if not set(answer.evidence_ids) <= {im.id for im in images}:
                raise ProviderError("回答引用了未提供的图片，已拒绝展示该结果")
            if not set(answer.source_ids) <= {s["id"] for s in sources[-3:]}:
                raise ProviderError("回答引用了不存在的资料，已拒绝展示该结果")
            # Reject stale task answers after a concurrent pause/edit/delete.
            if related and self.store.task(task["id"])["version"] != task["version"]:
                raise Conflict("回答期间任务发生变化，请重新提问")
            result = {"kind": "answer", **answer.model_dump(), "warnings": warnings,
                      "task_id": task["id"] if related else None,
                      "version": task["version"] if related else None,
                      "retrieval": {"events": [e["id"] for e in events], "frames": [im.id for im in images]}}
            if related and task["status"] != "ended":
                suggestion_id = uid("suggest") if answer.suggestion else None
                def record_answer(row, state, plan, db):
                    known = {issue["text"] for issue in state["open_issues"] if issue["status"] == "open"}
                    for missing in answer.missing:
                        if missing not in known:
                            state["open_issues"].append({"id": uid("issue"), "text": missing,
                                "status": "open", "provenance": "ai_unconfirmed", "evidence_ids": answer.evidence_ids})
                    state["open_issues"] = state["open_issues"][-30:]
                    if suggestion_id:
                        db.execute("INSERT INTO suggestions VALUES(?,?,?,'proposed',?,?)", (
                            suggestion_id, task["id"], answer.suggestion,
                            dump([{"status": "proposed", "at": time.time(), "provenance": "ai"}]),
                            dump(answer.evidence_ids)))
                    return row["status"], state, plan, "answer_recorded", {
                        "suggestion_id": suggestion_id, "suggestion": answer.suggestion,
                        "missing": answer.missing, "evidence_ids": answer.evidence_ids}
                updated = self.store.change(task["id"], task["version"], record_answer)
                result["version"] = updated["version"]
                if suggestion_id:
                    result["suggestion_id"] = suggestion_id
        scope = result.get("task_id")
        self.store.message(request.conversation_id, scope, "user", request.text)
        self.store.message(request.conversation_id, scope, "assistant", result["reply"], result)
        return result
