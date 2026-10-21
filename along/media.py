import hashlib
import io
import math
import os
import shutil
import time

from PIL import Image, ImageOps, UnidentifiedImageError

from along.config import Settings
from along.store import Conflict, Store


class CapacityError(ValueError):
    pass


class MediaStore:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        (store.directory / "media").mkdir(mode=0o700, exist_ok=True)

    def save(
        self,
        media_id: str,
        raw: bytes,
        task_id: str | None,
        conversation_id: str,
        captured: float,
        seq: int,
        kind="frame",
        origin="camera",
    ):
        if origin not in ("camera", "upload", "replay", "reference"):
            raise ValueError("无效媒体来源")
        if not math.isfinite(captured) or abs(captured - time.time()) > 86400:
            raise ValueError("采集时间无效或与服务端相差超过一天")
        if seq < 0 or len(raw) > (12 * 1024 * 1024 if kind == "frame" else 32 * 1024 * 1024):
            raise ValueError("媒体大小或序号超出限制")
        digest = hashlib.sha256(raw).hexdigest()
        existing = self.store.one("SELECT * FROM media WHERE id=?", (media_id,))
        if existing:
            if (existing["digest"], existing["task_id"], existing["conversation_id"]) != (
                digest,
                task_id,
                conversation_id,
            ):
                raise Conflict("媒体 ID 已存在但内容或所属会话不一致")
            return existing
        if origin == "reference" and (kind != "frame" or not task_id):
            raise ValueError("参考图必须是任务内图片")
        if task_id:
            task = self.store.task(task_id)
            if origin == "reference" and task["status"] not in ("draft", "paused"):
                raise Conflict("参考图仅用于任务准备，不属于执行记录")
            if origin != "reference" and task["status"] != "active":
                transitions = self.store.rows(
                    "SELECT kind,created FROM ledger WHERE task_id=? "
                    "AND kind IN ('confirm_plan','resume','pause','finish') ORDER BY created",
                    (task_id,),
                )
                started = None
                was_active = False
                for transition in transitions:
                    if transition["kind"] in ("confirm_plan", "resume"):
                        started = transition["created"]
                    elif started is not None:
                        if started <= captured <= transition["created"]:
                            was_active = True
                        started = None
                if not was_active:
                    raise Conflict("该图片不在已确认的任务记录时段内；暂停后不接受新的采集")
        if kind == "frame":
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > 24_000_000:
                        raise ValueError("图片像素过大")
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    image.thumbnail((1280, 1280))
                    output = io.BytesIO()
                    image.save(output, "JPEG", quality=82)
                    raw = output.getvalue()
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise ValueError("无法读取图片") from exc
        else:
            if not (raw.startswith(b"\x1aE\xdf\xa3") or raw[4:8] == b"ftyp"):
                raise ValueError("不支持的视频片段格式")
        suffix = "jpg" if kind == "frame" else ("mp4" if raw[4:8] == b"ftyp" else "webm")
        relative = f"media/{media_id}.{suffix}"
        target = self.store.directory / relative
        temporary = target.with_suffix(".part")
        # Serialize quota checks and file insertion across concurrent upload requests.
        with self.store.tx() as db:
            duplicate = db.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
            if duplicate:
                if (duplicate["digest"], duplicate["task_id"], duplicate["conversation_id"]) != (
                    digest,
                    task_id,
                    conversation_id,
                ):
                    raise Conflict("重复媒体 ID 的内容不一致")
                return dict(duplicate)
            total = db.execute("SELECT COALESCE(SUM(bytes),0) FROM media WHERE expired=0").fetchone()[0]
            if total + len(raw) > self.settings.max_storage_mb * 1024 * 1024:
                raise CapacityError("媒体存储额度已满，请删除旧任务；不会静默覆盖证据")
            if shutil.disk_usage(self.store.directory).free < len(raw) + 100 * 1024 * 1024:
                raise CapacityError("磁盘剩余空间不足，采集已拒绝")
            revision, step = self.store.capture_context(task_id, captured) if task_id else (None, None)
            try:
                temporary.write_bytes(raw)
                os.replace(temporary, target)
                db.execute(
                    "INSERT INTO media(id,task_id,conversation_id,kind,path,captured,received,seq,bytes,digest,origin,plan_revision,step_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        media_id,
                        task_id,
                        conversation_id,
                        kind,
                        relative,
                        captured,
                        time.time(),
                        seq,
                        len(raw),
                        digest,
                        origin,
                        revision,
                        step,
                    ),
                )
            except BaseException:
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                raise
        return self.store.one("SELECT * FROM media WHERE id=?", (media_id,))

    def cleanup(self):
        cutoff = time.time() - self.settings.retention_days * 86400
        # Active/paused tasks keep evidence regardless of age. Instant captures expire after one day.
        rows = self.store.rows(
            "SELECT m.* FROM media m LEFT JOIN tasks t ON t.id=m.task_id "
            "WHERE m.expired=0 AND ((m.task_id IS NULL AND m.received<?) OR "
            "(t.status='ended' AND t.updated<?))",
            (time.time() - 86400, cutoff),
        )
        for row in rows:
            with self.store.tx() as db:
                db.execute("UPDATE media SET expired=1 WHERE id=?", (row["id"],))
            (self.store.directory / row["path"]).unlink(missing_ok=True)
        # Contact sheets are regenerable caches; drop them after a day so deleted frames do not linger.
        sheets = self.store.directory / "media" / "sheets"
        for path in sheets.glob("*.jpg") if sheets.is_dir() else []:
            if path.stat().st_mtime < time.time() - 86400:
                path.unlink(missing_ok=True)

    def path(self, media_id):
        row = self.store.one("SELECT * FROM media WHERE id=?", (media_id,))
        if not row or row["expired"]:
            raise KeyError("原始媒体不存在或已过保留期")
        path = self.store.directory / row["path"]
        if not path.is_file():
            raise KeyError("原始文件已丢失，不能当作可用证据")
        return row, path
