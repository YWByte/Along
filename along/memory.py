import asyncio
import hashlib
import json
import logging
import time
from collections import Counter

import numpy as np

from along.config import Settings
from along.indexing import terms
from along.media import MediaStore
from along.providers.vlm import ImageInput, ProviderError, VLMProvider
from along.sheets import contact_sheet, sheet_id
from along.schemas import EventDraft, FrameSelection, Rank, SearchQuery
from along.store import Store, dump

log = logging.getLogger(__name__)


class Embeddings:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = None
        self.lock = asyncio.Lock()

    async def encode(self, texts: list[str]):
        if not self.settings.semantic_enabled:
            return None
        async with self.lock:

            def run():
                if self.model is None:
                    from sentence_transformers import SentenceTransformer

                    self.model = SentenceTransformer(self.settings.embedding_model, local_files_only=True)
                return self.model.encode(texts, normalize_embeddings=True).tolist()

            try:
                return await asyncio.to_thread(run)
            except Exception as exc:
                raise ProviderError(
                    "本地语义模型不可用；请运行 along prepare-embeddings，或显式关闭语义检索"
                ) from exc


class Memory:
    def __init__(self, store: Store, media: MediaStore, vlm: VLMProvider, settings: Settings):
        self.store, self.media, self.vlm, self.settings = store, media, vlm, settings
        self.embeddings = Embeddings(settings)
        self.stopping = asyncio.Event()
        self.worker_task = None
        self.last_error = None
        self.user_busy = False

    def schedule(self):
        tasks = self.store.rows("SELECT id,status FROM tasks")
        max_gap = max(5, 3 * self.settings.frame_interval)
        for task in tasks:
            if self.store.one(
                "SELECT 1 FROM jobs WHERE task_id=? AND status IN ('pending','running')",
                (task["id"],),
            ):
                continue
            frames = self.store.rows(
                "SELECT * FROM media WHERE task_id=? AND kind='frame' AND indexed=0 "
                "AND expired=0 ORDER BY captured,seq",
                (task["id"],),
            )
            if not frames:
                continue
            core = []
            closed = False
            for frame in frames:
                if core and (
                    any(frame[key] != core[0][key] for key in ("plan_revision", "step_id", "origin"))
                    or frame["captured"] - core[-1]["captured"] > max_gap
                ):
                    closed = True
                    break
                core.append(frame)
                if len(core) == self.settings.event_frames:
                    closed = True
                    break
            if (
                task["status"] == "active"
                and not closed
                and time.time() - core[-1]["captured"] < self.settings.event_window
            ):
                continue
            previous = self.store.rows(
                "SELECT * FROM media WHERE task_id=? AND kind='frame' AND indexed=1 "
                "AND expired=0 AND captured<? AND origin=? ORDER BY captured DESC LIMIT ?",
                (
                    task["id"],
                    core[0]["captured"],
                    core[0]["origin"],
                    self.settings.event_overlap_frames,
                ),
            )
            previous = previous[::-1]
            if previous and core[0]["captured"] - previous[-1]["captured"] > max_gap:
                previous = []
            ids = [f["id"] for f in previous] + [f["id"] for f in core]
            job_id = "evt_" + hashlib.sha256(dump(ids).encode()).hexdigest()[:24]
            with self.store.tx() as db:
                db.execute(
                    "INSERT OR IGNORE INTO jobs(id,task_id,frame_ids,status,available,context_count) "
                    "VALUES(?,?,?,'pending',?,?)",
                    (job_id, task["id"], dump(ids), time.time(), len(previous)),
                )
                db.executemany("UPDATE media SET indexed=1 WHERE id=?", [(f["id"],) for f in core])

    def image(self, frame_id):
        try:
            row, path = self.media.path(frame_id)
            label = (
                "用户上传图片，实际拍摄时间未知"
                if row["origin"] == "upload"
                else ("录像回放，不是实时现场" if row["origin"] == "replay" else "相机采集")
            )
            return ImageInput(
                frame_id,
                path,
                f"{label}；记录时间 Unix {row['captured']:.3f}；顺序 {row['seq']}；"
                f"界面计划版本 {row['plan_revision']} 步骤 {row['step_id']}（不代表实际动作）",
            )
        except KeyError:
            return None

    def sheet(self, images, frames):
        ids = [image.id for image in images]
        sid = sheet_id(ids)
        target = self.store.directory / "media" / "sheets" / f"{sid}.jpg"
        if not target.is_file():
            contact_sheet([image.path for image in images], [f["captured"] for f in frames], target)
        start, end = frames[0]["captured"], frames[-1]["captured"]
        tiles = "；".join(f"{i}={fid}" for i, fid in enumerate(ids, 1))
        return ImageInput(
            sid,
            target,
            f"带时间标注的拼图，{len(ids)} 帧，记录时间 Unix {start:.3f}–{end:.3f}，"
            f"每格左上角为序号和相对首帧秒数，阅读顺序从左到右、从上到下。序号对应帧ID：{tiles}",
            tuple(ids),
        )

    async def process(self, job):
        ids = json.loads(job["frame_ids"])
        context_count = min(job.get("context_count", 0), len(ids))
        context_ids, core_ids = ids[:context_count], ids[context_count:]
        if not core_ids:
            raise ProviderError("事件没有核心帧，未生成摘要")
        rows = self.store.rows(
            "SELECT id,captured,seq,origin,plan_revision,step_id FROM media WHERE id IN ("
            + ",".join("?" * len(ids))
            + ") ORDER BY captured,seq",
            ids,
        )
        by_id = {row["id"]: row for row in rows}
        ordered_rows = [by_id[fid] for fid in ids if fid in by_id]
        core_rows = [by_id[fid] for fid in core_ids if fid in by_id]
        if not core_rows:
            raise ProviderError("事件核心图片均不可用，未生成摘要")
        images = [image for fid in ids if (image := self.image(fid))]
        if not images:
            raise ProviderError("事件原始图片均不可用，未生成摘要")
        evidence = [self.sheet(images, [by_id[image.id] for image in images])] if len(images) > 1 else images
        core_start, core_end = core_rows[0]["captured"], core_rows[-1]["captured"]
        visual_start, visual_end = ordered_rows[0]["captured"], ordered_rows[-1]["captured"]
        speech = self.store.rows(
            "SELECT text FROM messages WHERE task_id=? AND role='user' AND created BETWEEN ? AND ?",
            (job["task_id"], visual_start, core_end + 1),
        )
        task = self.store.task(job["task_id"])
        revision = core_rows[0]["plan_revision"]
        plan = task["plan"] if task["state"].get("plan_revision") == revision else None
        if plan is None:
            snapshot = self.store.one(
                "SELECT json_extract(state,'$.plan') AS plan FROM checkpoints WHERE task_id=? "
                "AND json_extract(state,'$.state.plan_revision') IS ? ORDER BY created DESC LIMIT 1",
                (job["task_id"], revision),
            )
            plan = json.loads(snapshot["plan"]) if snapshot and snapshot["plan"] else None
        previous = self.store.one(
            "SELECT id,start,end,data FROM events WHERE task_id=? AND end<? ORDER BY end DESC LIMIT 1",
            (job["task_id"], core_start),
        )
        previous_index = None
        if previous:
            previous_data = json.loads(previous["data"])
            previous_index = {
                "id": previous["id"],
                "start": previous["start"],
                "end": previous["end"],
                "summary_not_fact": previous_data.get("summary", ""),
                "observations_not_fact": previous_data.get("observations", []),
                "unknowns": previous_data.get("unknowns", []),
            }
        task_context = {
            "goal_intent_not_fact": task["goal"],
            "plan_intent_not_fact": plan,
            "capture_plan_revision": revision,
            "capture_step_id": core_rows[0]["step_id"],
            "owned_range": {"start": core_start, "end": core_end, "frame_ids": core_ids},
            "continuity_context": {
                "start": visual_start,
                "end": visual_end,
                "frame_ids": context_ids,
                "previous_event_index_not_fact": previous_index,
            },
        }
        draft = await self.vlm.structured(
            "生成一条供后续检索的视觉事件。拼图按时间排列，必须查看全部格子。"
            "owned_range 是本事件负责描述的新帧；continuity_context 中的前置帧和上一事件只用于识别跨边界变化，"
            "不得把它们重复写成当前时段发生的事件。任务目标、计划、界面步骤和上一事件都是意图或可错索引，"
            "不证明动作实际发生。只写画面可见事实与跨帧变化，不判断做对/做错。"
            "观察必须引用拼图标签中真实的原始帧 ID（也可引用格子序号）；推测和未知单列。"
            "短暂动作可能落在采样间隙，未观察到不等于没有发生。\n"
            + dump(task_context)
            + "\n用户在视觉范围内的陈述（不是视觉事实）："
            + dump(speech),
            EventDraft,
            evidence,
        )
        if len(evidence) == 1 and evidence[0].frame_ids:
            tile_ids = evidence[0].frame_ids
            for observation in draft.observations:
                observation.frame_ids = [
                    tile_ids[int(fid) - 1] if fid.isdigit() and 1 <= int(fid) <= len(tile_ids) else fid
                    for fid in observation.frame_ids
                ]
        allowed = {image.id for image in images}
        for observation in draft.observations:
            if not observation.frame_ids or not set(observation.frame_ids) <= allowed:
                raise ProviderError("事件观察缺少有效图片引用；拒绝存入事实索引")
        data = draft.model_dump()
        data.update(
            {
                "user_statements": [r["text"] for r in speech],
                "owned_frame_ids": core_ids,
                "context_frame_ids": context_ids,
                "visual_start": visual_start,
                "visual_end": visual_end,
                "previous_event_id": previous["id"] if previous else None,
                "capture_plan_revision": revision,
                "capture_step_id": core_rows[0]["step_id"],
            }
        )
        try:
            vector = await self.embeddings.encode([dump(data)])
        except ProviderError as exc:
            vector = None
            log.warning("event saved without semantic vector: %s", exc)
        with self.store.tx() as db:
            if not db.execute("SELECT 1 FROM jobs WHERE id=?", (job["id"],)).fetchone():
                return
            db.execute(
                "INSERT INTO events(id,task_id,start,end,data,frame_ids,embedding,embedding_model) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,"
                "start=excluded.start,end=excluded.end,frame_ids=excluded.frame_ids,"
                "revision=events.revision+1,embedding=excluded.embedding,embedding_model=excluded.embedding_model",
                (
                    job["id"],
                    job["task_id"],
                    core_start,
                    core_end,
                    dump(data),
                    dump(ids),
                    dump(vector[0]) if vector else None,
                    self.settings.embedding_model if vector else None,
                ),
            )
            db.execute("UPDATE jobs SET status='done',error=NULL WHERE id=?", (job["id"],))
        log.info(
            "event indexed id=%s core_frames=%d context_frames=%d",
            job["id"],
            len(core_ids),
            len(context_ids),
        )

    async def worker(self):
        last_cleanup = 0
        while not self.stopping.is_set():
            try:
                self.schedule()
                if time.time() - last_cleanup > 600:
                    self.media.cleanup()
                    last_cleanup = time.time()
                job = (
                    None
                    if self.user_busy
                    else self.store.one(
                        "SELECT * FROM jobs WHERE status='pending' AND available<=? ORDER BY available LIMIT 1",
                        (time.time(),),
                    )
                )
                if job:
                    with self.store.tx() as db:
                        db.execute(
                            "UPDATE jobs SET status='running',attempts=attempts+1 WHERE id=?", (job["id"],)
                        )
                    try:
                        await self.process(job)
                        self.last_error = None
                    except asyncio.CancelledError:
                        with self.store.tx() as db:
                            db.execute("UPDATE jobs SET status='pending' WHERE id=?", (job["id"],))
                        raise
                    except Exception as exc:
                        message = str(exc) if isinstance(exc, ProviderError) else "后台事件处理失败"
                        self.last_error = message
                        attempts = job["attempts"] + 1
                        with self.store.tx() as db:
                            db.execute(
                                "UPDATE jobs SET status=?,error=?,available=? WHERE id=?",
                                (
                                    "failed" if attempts >= 3 else "pending",
                                    message,
                                    time.time() + min(60, 5 * 2**attempts),
                                    job["id"],
                                ),
                            )
                        log.warning("event failed id=%s attempt=%d error=%s", job["id"], attempts, message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "记忆后台异常，请检查数据库和磁盘"
                log.exception("memory worker failed")
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=1)
            except TimeoutError:
                pass

    async def retrieve(self, task_id: str, question: str):
        events = self.store.rows("SELECT * FROM events WHERE task_id=? ORDER BY start", (task_id,))
        warnings = []
        try:
            query = await self.vlm.structured(
                "为现场问题生成最多三种检索表述与可能相关对象。只扩展搜索，不作诊断。"
                "只有用户明确要求某时间段时才给出 start_time/end_time 的 Unix 秒，否则为 null。"
                f"当前 Unix 秒 {time.time()}。问题：" + question,
                SearchQuery,
            )
            queries = [question] + query.queries + query.objects
        except ProviderError:
            query = SearchQuery()
            queries = [question]
            warnings.append("查询扩展失败，已使用原问题检索")
        if not events:
            return [], self.recent_images(task_id, 6), warnings
        query_terms = Counter(terms(" ".join(queries)))
        lexical = []
        object_scores = []
        for index, event in enumerate(events):
            data = json.loads(event["data"])
            counts = Counter(terms(dump(data)))
            score = sum(min(counts[t], n) for t, n in query_terms.items()) / max(1, len(counts)) ** 0.4
            lexical.append((index, score))
            object_scores.append(
                (index, sum(1 for t in terms(dump(data.get("objects", []))) if t in query_terms))
            )
        rankings = [
            sorted(lexical, key=lambda x: x[1], reverse=True),
            sorted(object_scores, key=lambda x: x[1], reverse=True),
        ]
        try:
            vector = await self.embeddings.encode([question])
            if vector:
                semantic = [
                    (i, float(np.dot(vector[0], json.loads(event["embedding"]))))
                    for i, event in enumerate(events)
                    if event["embedding"] and event["embedding_model"] == self.settings.embedding_model
                ]
                rankings.append(sorted(semantic, key=lambda x: x[1], reverse=True))
        except ProviderError as exc:
            warnings.append(str(exc))
        if query.start_time is not None or query.end_time is not None:
            low = query.start_time if query.start_time is not None else events[0]["start"]
            high = query.end_time if query.end_time is not None else time.time()
            if low <= high:
                temporal = [
                    (i, 1.0 / (1 + max(low - e["end"], e["start"] - high, 0))) for i, e in enumerate(events)
                ]
                rankings.append(sorted(temporal, key=lambda item: item[1], reverse=True))
        scores = Counter()
        for ranking in rankings:
            for rank, (index, score) in enumerate(ranking[:20]):
                if score > 0:
                    scores[index] += 1 / (60 + rank)
        # Recent events form a separate recall route, not a hard cutoff on older evidence.
        for rank, index in enumerate(range(len(events) - 1, max(-1, len(events) - 5), -1)):
            scores[index] += 1 / (60 + rank)
        seeds = [i for i, _ in scores.most_common(14)]
        indexes = set(seeds)
        for index in seeds[:6]:
            indexes.update(i for i in (index - 1, index + 1) if 0 <= i < len(events))
        candidates = [events[i] for i in sorted(indexes)]
        selected = [events[i] for i in seeds[:5]]
        if len(candidates) > 5:
            overview = []
            for event in selected[:5]:
                ids = json.loads(event["frame_ids"])
                if image := self.image(ids[len(ids) // 2]):
                    overview.append(image)
            try:
                rank = await self.vlm.structured(
                    "选择最值得回查原图的事件 ID，最多五个。不要只选支持某个猜测的事件。问题："
                    + question
                    + "\n事件摘要是可错的索引："
                    + dump(
                        [
                            {"id": e["id"], "start": e["start"], "data": json.loads(e["data"])}
                            for e in candidates
                        ]
                    ),
                    Rank,
                    overview,
                )
                by_id = {e["id"]: e for e in candidates}
                ranked = [by_id[eid] for eid in dict.fromkeys(rank.event_ids) if eid in by_id][:5]
                if ranked:
                    selected = ranked
            except ProviderError:
                warnings.append("事件重排失败，保留多路召回结果")
        images = self.recent_images(task_id, 2)
        seen = {i.id for i in images}
        for event in selected[:2]:
            ids = json.loads(event["frame_ids"])
            if len(ids) <= 3:
                continue
            picks = [ids[i] for i in sorted(set(np.linspace(0, len(ids) - 1, min(8, len(ids)), dtype=int)))]
            review_images = [im for fid in picks if (im := self.image(fid))]
            try:
                chosen = await self.vlm.structured(
                    "在这一相关历史时间段中选择最能帮助回答问题的原图 ID，最多两张。"
                    "包括反驳猜测的证据，不从缺失画面推断动作。问题：" + question,
                    FrameSelection,
                    review_images,
                )
                allowed = {im.id for im in review_images}
                for fid in list(dict.fromkeys(chosen.frame_ids))[:2]:
                    if fid in allowed and fid not in seen and (im := self.image(fid)):
                        images.append(im)
                        seen.add(fid)
            except ProviderError:
                warnings.append("一个历史时间段的原图细查失败，保留常规抽帧")
        # Expand raw frame ranges, not only the event summary's keyframes.
        for event in selected:
            ids = json.loads(event["frame_ids"])
            evidence_ids = [
                fid
                for obs in json.loads(event["data"]).get("observations", [])
                for fid in obs.get("frame_ids", [])
            ]
            picks = list(dict.fromkeys(evidence_ids[:1] + [ids[len(ids) // 2], ids[-1]]))[:2]
            for fid in picks:
                if fid not in seen and (image := self.image(fid)):
                    images.append(image)
                    seen.add(fid)
        return selected, images[: self.settings.max_images - 1], warnings

    def recent_images(self, task_id, limit):
        rows = self.store.rows(
            "SELECT id FROM media WHERE task_id=? AND kind='frame' AND expired=0 "
            "ORDER BY captured DESC,seq DESC LIMIT ?",
            (task_id, limit),
        )
        return [image for row in rows[::-1] if (image := self.image(row["id"]))]

    async def close(self):
        self.stopping.set()
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
