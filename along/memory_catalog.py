"""Task-wide, capture-time event directory; no visual claims are promoted to facts."""

import json

from along.indexing import current_step
from along.store import dump


class MemoryCatalog:
    def __init__(self, memory, task, cutoff):
        self.settings = memory.settings
        self.task = task
        self.cutoff = cutoff
        self.groups = {}
        store = memory.store
        task_id = task["id"]
        snapshots = store.rows(
            "SELECT created,version,json_extract(state,'$.state.plan_revision') AS revision,"
            "json_extract(state,'$.state.steps') AS steps,json_extract(state,'$.plan') AS plan "
            "FROM checkpoints WHERE task_id=? AND created<=? ORDER BY created,version",
            (task_id, cutoff),
        )
        self.completions = {}
        previous = {}
        for snapshot in snapshots:
            revision = snapshot["revision"]
            states = json.loads(snapshot["steps"] or "{}")
            for step_id, status in states.items():
                key = revision, step_id
                if status == "user_reported_done" and previous.get(key) != status:
                    self.completions[key] = snapshot["created"]
                elif status != "user_reported_done":
                    self.completions.pop(key, None)
                previous[key] = status
            self.add_plan(revision, json.loads(snapshot["plan"] or "null"), states)
        self.revision = task["state"].get("plan_revision")
        self.add_plan(self.revision, task["plan"], task["state"].get("steps", {}))
        frames = store.rows(
            "SELECT id,captured,seq,plan_revision,step_id,origin,expired FROM media "
            "WHERE task_id=? AND kind='frame' AND captured<=? ORDER BY captured,seq",
            (task_id, cutoff),
        )
        by_id = {frame["id"]: frame for frame in frames}
        for frame in frames:
            group = self.group(frame["plan_revision"], frame["step_id"])
            group["frame_count"] += 1
            group["available_frame_count"] += not frame["expired"]
            group["start"] = frame["captured"] if group["start"] is None else group["start"]
            group["end"] = frame["captured"]
        events = store.rows(
            "SELECT id,start,end,json_extract(data,'$.summary') AS summary,frame_ids FROM events "
            "WHERE task_id=? AND end<=? ORDER BY start,id",
            (task_id, cutoff),
        )
        covered = set()
        for event in events:
            ids = json.loads(event["frame_ids"])
            members = [by_id[fid] for fid in ids if fid in by_id]
            covered.update(frame["id"] for frame in members)
            keys = {(f["plan_revision"], f["step_id"]) for f in members} or {(None, None)}
            for key in keys:
                selected = [f for f in members if (f["plan_revision"], f["step_id"]) == key]
                group = self.group(*key)
                if group["start"] is None:
                    group["start"], group["end"] = event["start"], event["end"]
                elif not group["frame_count"]:
                    group["start"] = min(group["start"], event["start"])
                    group["end"] = max(group["end"], event["end"])
                group["events"].append(
                    {
                        "id": event["id"],
                        "start": event["start"],
                        "end": event["end"],
                        "frame_count": len(selected),
                        "available_frame_count": sum(not f["expired"] for f in selected),
                        "summary_not_fact": (event["summary"] or "")[:180],
                        "status": "indexed",
                        "mixed_step_event": len(keys) > 1,
                    }
                )
        failed = set()
        for job in store.rows("SELECT frame_ids FROM jobs WHERE task_id=? AND status='failed'", (task_id,)):
            failed.update(json.loads(job["frame_ids"]))
        windows = {}
        for frame in frames:
            if frame["id"] in covered:
                continue
            key = frame["plan_revision"], frame["step_id"]
            status = "index_failed" if frame["id"] in failed else "pending_summary"
            window_key = *key, frame["origin"], status, frame["expired"]
            window = windows.get(window_key)
            if (
                window is None
                or frame["captured"] - window["start"] > self.settings.event_window
                or frame["captured"] - window["end"] > max(5, 3 * self.settings.frame_interval)
            ):
                window = {
                    "id": None,
                    "start": frame["captured"],
                    "end": frame["captured"],
                    "frame_count": 0,
                    "available_frame_count": 0,
                    "status": status,
                    "origin": frame["origin"],
                    "summary_not_fact": "摘要失败，仍可查看原图"
                    if frame["id"] in failed
                    else "待整理，可按时间查看原图",
                }
                windows[window_key] = window
                self.group(*key)["events"].append(window)
            window["end"] = frame["captured"]
            window["frame_count"] += 1
            window["available_frame_count"] += not frame["expired"]
        for group in self.groups.values():
            group["events"].sort(key=lambda e: (e["start"], e["id"] or ""))
            group["event_count"] = len(group["events"])
            group["user_marked_done_at"] = self.completions.get((group["plan_revision"], group["step_id"]))
        self.indexed_events = len(events)

    def group(self, revision, step_id):
        key = revision, step_id
        if key not in self.groups:
            self.groups[key] = {
                "plan_revision": revision,
                "step_id": step_id,
                "title": "步骤归属未知",
                "user_state": "unknown",
                "frame_count": 0,
                "available_frame_count": 0,
                "start": None,
                "end": None,
                "events": [],
            }
        return self.groups[key]

    def add_plan(self, revision, plan, states):
        for step in (plan or {}).get("steps", []):
            group = self.group(revision, step["id"])
            group.update(title=step["title"], user_state=states.get(step["id"], "unknown"))

    def anchor(self, key):
        if key is None or key not in self.groups:
            return None
        group = self.groups[key]
        return {
            name: group[name]
            for name in (
                "plan_revision",
                "step_id",
                "title",
                "user_state",
                "start",
                "end",
                "user_marked_done_at",
            )
        }

    def payload(self):
        step = current_step(self.task["state"], self.task["plan"])
        current = (self.revision, step["id"]) if step else None
        completed = [
            key
            for key in self.completions
            if key[0] == self.revision and self.groups.get(key, {}).get("user_state") == "user_reported_done"
        ]
        latest = max(completed, key=self.completions.get) if completed else None
        steps = (self.task["plan"] or {}).get("steps", [])
        position = next((i for i, item in enumerate(steps) if step and item["id"] == step["id"]), None)
        previous = (self.revision, steps[position - 1]["id"]) if position else None
        groups = list(self.groups.values())
        groups.sort(key=lambda g: (g["plan_revision"] != self.revision, g["plan_revision"] or -1))
        result = {
            "mode": "full",
            "as_of": self.cutoff,
            "current_plan_revision": self.revision,
            "current_ui_step": self.anchor(current),
            "last_user_completed_step": self.anchor(latest),
            "previous_plan_step": self.anchor(previous),
            "indexed_event_count": self.indexed_events,
            "semantics": "全任务事件目录，仅供定位。完成状态是用户标记，步骤归属是拍摄时界面标签；摘要不证明动作。"
            "没有记录不代表没有执行。当前版本的操作要求见 adopted_plan；不可把当前要求套用到旧版本。"
            "待整理片段可直接按时间打开。记录可用数量来自数据库，缺失文件由打开工具核验。",
            "steps": groups,
        }
        if len(dump(result)) > self.settings.memory_catalog_chars:
            result["mode"] = "step_rollups"
            result["compression_note"] = (
                "目录详情超过预算，保留全部步骤和时间范围；用各步骤 browse 入口展开事件及待整理片段。"
            )
            result["steps"] = []
            for group in groups:
                compact = {key: value for key, value in group.items() if key != "events"}
                compact["pending_window_count"] = sum(e["id"] is None for e in group["events"])
                compact["browse"] = {
                    "tool": "browse",
                    "plan_revision": group["plan_revision"],
                    "step_id": group["step_id"],
                    "start_time": group["start"],
                    "end_time": group["end"],
                }
                result["steps"].append(compact)
            if len(dump(result)) > self.settings.memory_catalog_chars:
                result["budget_note"] = "步骤目录本身超过软预算，保留全部入口而不静默删除历史步骤。"
        return result

    def browse(self, call, limit):
        entries = []
        for group in self.groups.values():
            if call.plan_revision is not None and group["plan_revision"] != call.plan_revision:
                continue
            if call.step_id is not None and group["step_id"] != call.step_id:
                continue
            for event in group["events"]:
                if call.start_time is not None and event["end"] < call.start_time:
                    continue
                if call.end_time is not None and event["start"] > call.end_time:
                    continue
                if call.before_time is not None and event["start"] >= call.before_time:
                    continue
                entries.append(
                    {**event, "plan_revision": group["plan_revision"], "step_id": group["step_id"]}
                )
        entries.sort(key=lambda e: (e["start"], e["id"] or ""), reverse=True)
        selected = entries[call.offset : call.offset + limit]
        next_page = None
        if call.offset + limit < len(entries):
            next_page = call.model_dump(exclude_none=True)
            next_page["offset"] = call.offset + limit
        return {
            "events": [e for e in selected if e["id"]],
            "unindexed_windows": [e for e in selected if not e["id"]],
            "next_page": next_page,
            "hint": "按 next_page 原样翻页。已索引事件用 event_id；待整理片段用 start_time/end_time，并带 plan_revision、step_id。摘要不是事实。",
        }
