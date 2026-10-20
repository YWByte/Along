"""Bounded, task-scoped memory tools. Index results are not visual evidence."""

import json
import time
from collections import Counter

import numpy as np

from along.indexing import terms
from along.memory_catalog import MemoryCatalog
from along.providers.vlm import ProviderError
from along.schemas import MemoryCall


class MemoryTools:
    def __init__(self, memory, task_id, cutoff=None):
        self.memory = memory
        self.store = memory.store
        self.settings = memory.settings
        self.task_id = task_id
        self._catalog = None
        self.cutoff = cutoff if cutoff is not None else time.time()

    def overview(self):
        row = self.store.one(
            "SELECT COUNT(*) AS frames,MIN(captured) AS first,MAX(captured) AS last "
            "FROM media WHERE task_id=? AND kind='frame' AND expired=0 AND captured<=?",
            (self.task_id, self.cutoff),
        )
        row["events"] = self.store.one(
            "SELECT COUNT(*) AS n FROM events WHERE task_id=? AND end<=?", (self.task_id, self.cutoff)
        )["n"]
        row["latest_age_seconds"] = round(self.cutoff - row["last"], 1) if row["last"] else None
        row["note"] = (
            "先根据任务事件目录选择步骤和时间段，再打开原图。current 只读最近相机记录，不触发新拍摄。"
        )
        return row

    def filters(self, call, alias="e"):
        clauses, args = [f"{alias}.task_id=?", f"{alias}.end<=?"], [self.task_id, self.cutoff]
        if call.start_time is not None:
            clauses.append(f"{alias}.end>=?")
            args.append(call.start_time)
        if call.end_time is not None:
            clauses.append(f"{alias}.start<=?")
            args.append(call.end_time)
        if call.before_time is not None:
            clauses.append(f"{alias}.start<?")
            args.append(call.before_time)
        if call.step_id is not None or call.plan_revision is not None:
            nested = [
                f"m.task_id={alias}.task_id",
                f"m.captured BETWEEN {alias}.start AND {alias}.end",
                f"m.id IN (SELECT value FROM json_each({alias}.frame_ids))",
            ]
            for key in ("step_id", "plan_revision"):
                if getattr(call, key) is not None:
                    nested.append(f"m.{key}=?")
                    args.append(getattr(call, key))
            clauses.append("EXISTS (SELECT 1 FROM media m WHERE " + " AND ".join(nested) + ")")
        return " AND ".join(clauses), args

    def describe(self, event):
        data = json.loads(event["data"])
        tags = self.store.rows(
            "SELECT DISTINCT plan_revision,step_id,origin FROM media WHERE task_id=? "
            "AND id IN (SELECT value FROM json_each(?)) LIMIT 8",
            (self.task_id, event["frame_ids"]),
        )
        return {
            "id": event["id"],
            "start": event["start"],
            "end": event["end"],
            "summary_not_fact": data.get("summary", "")[:600],
            "objects": data.get("objects", [])[:10],
            "unknowns": data.get("unknowns", [])[:3],
            "capture_ui_tags_not_actions": tags,
        }

    async def search(self, call):
        where, args = self.filters(call)
        # Chinese unigrams/bigrams share the tokenizer used when writing the index.
        tokens = list(dict.fromkeys(terms(call.query)))[:80]
        lexical = []
        if tokens:
            expression = " OR ".join('"' + token + '"' for token in tokens)
            lexical = self.store.rows(
                "SELECT e.* FROM event_search JOIN events e ON e.id=event_search.event_id "
                "WHERE event_search MATCH ? AND " + where + " ORDER BY rank LIMIT 40",
                [expression, *args],
            )
        by_id = {e["id"]: e for e in lexical}
        scores = Counter({e["id"]: 1 / (60 + i) for i, e in enumerate(lexical)})
        warnings = []
        try:
            vector = await self.memory.embeddings.encode([call.query])
            if vector:
                # Bounded semantic scan spans the full timeline; keyword lookup has no age cutoff.
                candidates = self.store.rows(
                    "WITH candidates AS (SELECT e.*,ROW_NUMBER() OVER (ORDER BY start) AS rn,"
                    "COUNT(*) OVER () AS total FROM events e WHERE "
                    + where
                    + " AND embedding IS NOT NULL AND embedding_model=?) "
                    "SELECT * FROM candidates WHERE (rn-1) % MAX(1,CAST((total+?-1)/? AS INTEGER))=0 LIMIT ?",
                    [*args, self.settings.embedding_model, *([self.settings.memory_candidates] * 3)],
                )
                ranked = []
                for event in candidates:
                    try:
                        embedding = np.asarray(json.loads(event["embedding"]), dtype=float)
                        if embedding.shape == (len(vector[0]),) and np.isfinite(embedding).all():
                            score = float(np.dot(vector[0], embedding))
                            if score > 0:
                                ranked.append((event, score))
                    except (ValueError, TypeError):
                        continue
                for index, (event, _) in enumerate(
                    sorted(ranked, key=lambda pair: pair[1], reverse=True)[:40]
                ):
                    by_id[event["id"]] = event
                    scores[event["id"]] += 1 / (60 + index)
                if candidates and candidates[0]["total"] > self.settings.memory_candidates:
                    warnings.append(
                        "语义检索按时间抽样候选，关键词检索仍覆盖全部范围；未命中不代表没有发生，可缩小时间或步骤范围。"
                    )
        except ProviderError:
            warnings.append("语义检索不可用，保留关键词检索；可按时间或步骤浏览。")
        selected = [by_id[eid] for eid, _ in scores.most_common(self.settings.memory_results)]
        return {
            "events": [self.describe(e) for e in selected],
            "warnings": warnings,
            "overview": self.overview(),
            "hint": "摘要仅供定位；空结果时先核对目录的步骤和时段，再 browse 或按目标范围 open，不自动退到最近片段。",
        }, []

    def catalog(self, task=None):
        if self._catalog is None:
            self._catalog = MemoryCatalog(self.memory, task or self.store.task(self.task_id), self.cutoff)
        return self._catalog

    def browse(self, call):
        if not self.store.one("SELECT id FROM tasks WHERE id=?", (self.task_id,)):
            return {"events": [], "unindexed_windows": [], "error": "任务不存在"}, []
        return self.catalog().browse(call, self.settings.memory_results), []

    def open(self, call, capacity):
        event = None
        if call.event_id:
            event = self.store.one(
                "SELECT * FROM events WHERE task_id=? AND id=? AND end<=?",
                (self.task_id, call.event_id, self.cutoff),
            )
            if not event:
                return {"error": "片段不存在或不属于当前任务。"}, []
        elif call.tool == "open" and call.step_id is not None and call.plan_revision is None:
            return {"error": "按步骤打开时须同时指定 plan_revision，避免不同版本同名步骤混用。"}, []
        clauses = ["task_id=?", "kind='frame'", "expired=0", "captured<=?"]
        args = [self.task_id, self.cutoff]
        for key in ("step_id", "plan_revision"):
            if getattr(call, key) is not None:
                clauses.append(f"{key}=?")
                args.append(getattr(call, key))
        if event:
            tags = self.store.rows(
                "SELECT DISTINCT plan_revision,step_id FROM media WHERE task_id=? "
                "AND id IN (SELECT value FROM json_each(?))",
                (self.task_id, event["frame_ids"]),
            )
            if len(tags) == 1:
                for key in ("step_id", "plan_revision"):
                    if getattr(call, key) is None:
                        clauses.append(f"{key} IS ?")
                        args.append(tags[0][key])
        if call.tool == "current":
            clauses.append("origin='camera'")
        where = " AND ".join(clauses)
        latest = self.store.one("SELECT MAX(captured) AS t FROM media WHERE " + where, args)["t"]
        if latest is None:
            return {"error": "此范围没有可用图片，不能据此判断动作未发生。"}, []
        low, high = call.start_time, call.end_time
        if event:
            low, high = event["start"] - 2, event["end"] + 2
        elif call.tool == "open" and call.step_id is not None and low is None and high is None:
            low = self.store.one("SELECT MIN(captured) AS t FROM media WHERE " + where, args)["t"]
            high = latest
        if call.tool == "current":
            low = high = latest
        else:
            if high is None:
                high = min(self.cutoff, low + call.seconds) if low is not None else latest
            if low is None:
                seconds = call.seconds if "seconds" in call.model_fields_set else self.settings.clip_seconds
                low = high - seconds
        if high < low or high - low > 120:
            return {
                "error": "该范围超过120秒或时间反向，请根据目录选择事件或更小时间段。",
                "start": low,
                "end": high,
            }, []
        count = min(capacity, call.frames if "frames" in call.model_fields_set else self.settings.clip_frames)
        if call.tool == "current":
            count = min(count, 1)
        if count <= 0:
            return {"error": "本轮图片预算已用完，请依据已查看图片回答。"}, []
        rows = self.store.rows(
            "SELECT id,captured,seq,origin,plan_revision,step_id FROM media WHERE "
            + where
            + " AND captured BETWEEN ? AND ? ORDER BY captured,seq",
            [*args, low, min(high, self.cutoff)],
        )
        images, frames = [], []
        missing = 0
        # Uniform time coverage preserves the start/middle/end, never significance-ranked single frames.
        for index in (
            sorted(set(np.linspace(0, len(rows) - 1, min(len(rows), count), dtype=int))) if rows else []
        ):
            row = rows[index]
            image = self.memory.image(row["id"])
            if image:
                images.append(image)
                frames.append(row)
            else:
                missing += 1
        gaps = [b["captured"] - a["captured"] for a, b in zip(rows, rows[1:])]
        return {
            "event_id": event["id"] if event else None,
            "start": low,
            "end": min(high, self.cutoff),
            "available_frames": len(rows),
            "frames": frames,
            "missing_files": missing,
            "largest_recorded_gap_seconds": round(max(gaps), 3) if gaps else None,
            "last_frame_age_seconds": round(self.cutoff - frames[-1]["captured"], 1) if frames else None,
            "sampling": "按时间排序的离散图片，不是连续视频。不能推断未拍到的动作、精确次数/速度、痛感或安全。界面步骤不代表实际动作。",
            "error": None if images else "此范围原图不可用或没有记录；不能当作动作未发生。",
        }, images

    async def execute(self, call: MemoryCall, capacity):
        if call.tool == "browse":
            return self.browse(call)
        if call.tool == "search":
            return await self.search(call)
        return self.open(call, capacity)
