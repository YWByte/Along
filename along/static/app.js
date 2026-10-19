"use strict";
const $ = (id) => document.getElementById(id);
const conversation = (sessionStorage.alongConversation ||= crypto.randomUUID());
let token = "",
  config = {},
  current = null,
  state = null,
  stream = null,
  pickedImage = null;
let busy = false,
  uploading = false,
  captureTimer,
  voiceRecorder,
  voiceStream,
  voiceTimer;
let clipRecorder = null,
  clipTimer = null,
  captureGeneration = 0,
  seq = 0,
  lastTaskRender = "";
let recordingTask = null,
  queueDB,
  queueSending = false,
  queueError = "";
const blobURLs = new Set();

function element(tag, text, className) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (className) e.className = className;
  return e;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    $("toast").hidden = true;
  }, 7000);
}
function errorText(e) {
  return e.message || String(e);
}
function bind(id, event, handler) {
  $(id).addEventListener(event, (e) =>
    Promise.resolve(handler(e)).catch((err) => toast(errorText(err))),
  );
}
async function api(path, options = {}) {
  const headers = { "X-Along-Token": token, ...options.headers };
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const timeout =
    path === "/api/chat" || path.endsWith("/prepare")
      ? 310000
      : path === "/api/speech"
        ? 120000
        : 20000;
  const response = await fetch(path, {
    ...options,
    headers,
    signal: options.signal || AbortSignal.timeout(timeout),
  });
  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = `HTTP ${response.status}`;
    }
    const err = new Error(
      typeof detail === "string" ? detail : JSON.stringify(detail),
    );
    err.status = response.status;
    throw err;
  }
  return response;
}
async function json(path, options = {}) {
  return (await api(path, options)).json();
}
function setBusy(value, message = "正在结合现场与记录分析…") {
  busy = value;
  $("send").disabled = value;
  $("activity").textContent = value ? message : "";
}
function speak(text) {
  if (!$("speak").checked || !("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "zh-CN";
  speechSynthesis.speak(u);
}
function linkButton(label, callback) {
  const b = element("button", label);
  b.type = "button";
  b.onclick = () =>
    Promise.resolve(callback()).catch((e) => toast(errorText(e)));
  return b;
}
async function viewMedia(id, kind = "frame") {
  const response = await api(`/api/media/${id}`);
  const url = URL.createObjectURL(await response.blob());
  blobURLs.add(url);
  const content = $("evidence-content");
  content.replaceChildren(element("p", `原始记录：${id}`));
  const el = element(kind === "frame" ? "img" : "video");
  el.src = url;
  if (kind !== "frame") el.controls = true;
  content.append(el);
  $("evidence-dialog").showModal();
}
async function viewSource(id) {
  const s = await json(`/api/sources/${id}`);
  $("evidence-content").replaceChildren(
    element("h3", s.title),
    element("p", s.url),
    element("pre", s.content),
  );
  $("evidence-dialog").showModal();
}
function message(role, text, data = {}) {
  const box = element("article", undefined, role);
  box.append(element("p", text));
  for (const [key, label] of [
    ["observed", "观察"],
    ["hypotheses", "可能解释"],
    ["missing", "还缺什么"],
    ["checks", "继续前检查"],
  ]) {
    if (data[key]?.length) {
      const d = element("details");
      if (key === "checks") d.open = true;
      d.append(element("summary", label));
      const ul = element("ul");
      data[key].forEach((t) => ul.append(element("li", t)));
      d.append(ul);
      box.append(d);
    }
  }
  if (data.evidence_ids?.length) {
    const d = element("div");
    data.evidence_ids.forEach((id, i) =>
      d.append(linkButton(`图片依据 ${i + 1}`, () => viewMedia(id))),
    );
    box.append(d);
  }
  if (data.source_ids?.length)
    data.source_ids.forEach((id, i) =>
      box.append(linkButton(`参考资料 ${i + 1}`, () => viewSource(id))),
    );
  if (data.warnings?.length)
    box.append(element("p", data.warnings.join("；"), "warning"));
  $("messages").append(box);
  $("messages").scrollTop = $("messages").scrollHeight;
}
async function refreshMessages(taskId = null) {
  const rows = await json(
    `/api/messages/${conversation}${taskId ? `?task_id=${taskId}` : ""}`,
  );
  $("messages").replaceChildren();
  if (!rows.length)
    message(
      "assistant",
      "可以问“这个是什么”，也可以说“我们一起做……”。任务开始前，我会先找资料，与你确认操作表。",
    );
  rows.forEach((m) =>
    message(m.role, m.text, m.data ? JSON.parse(m.data) : {}),
  );
}

async function openQueue() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("along-capture", 1);
    req.onupgradeneeded = () =>
      req.result.createObjectStore("uploads", { keyPath: "id" });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
function queueOp(mode, operation) {
  return new Promise((resolve, reject) => {
    const tx = queueDB.transaction("uploads", mode);
    const req = operation(tx.objectStore("uploads"));
    tx.oncomplete = () => resolve(req.result);
    tx.onerror = () => reject(tx.error);
  });
}
async function flushQueue() {
  if (queueSending || !queueDB) return;
  queueSending = true;
  try {
    const rows = await queueOp("readonly", (s) => s.getAll());
    rows.sort((a, b) => a.captured - b.captured);
    for (const row of rows) {
      try {
        await uploadBlob(
          row.blob,
          row.taskId,
          row.kind,
          row.id,
          row.captured,
          row.seq,
          row.conversationId || conversation,
        );
        await queueOp("readwrite", (s) => s.delete(row.id));
        queueError = "";
      } catch (e) {
        queueError = errorText(e);
        if (e.status === 507) {
          await stopCamera();
          toast("存储已满，已关闭采集；待补传数据仍保留在本机");
          break;
        }
        if ([404, 409, 422].includes(e.status)) {
          toast(`一段缓存记录无法补传：${queueError}`);
          await queueOp("readwrite", (s) => s.delete(row.id));
          continue;
        }
        break;
      }
    }
  } finally {
    queueSending = false;
    await showQueue();
  }
}
async function showQueue() {
  if (!queueDB) return;
  const rows = await queueOp("readonly", (s) => s.getAll());
  $("queue-status").textContent = rows.length
    ? `本地待补传 ${rows.length} 段${queueError ? " · " + queueError : ""}`
    : current?.status === "active"
      ? "任务进行中：画面按间隔保存，事件在后台整理"
      : "即时模式按需取图；任务暂停时不持续记录";
}
async function enqueue(blob, taskId, kind, captured = Date.now() / 1000) {
  const rows = await queueOp("readonly", (s) => s.getAll());
  if (
    rows.reduce((n, r) => n + r.blob.size, 0) + blob.size >
    80 * 1024 * 1024
  ) {
    await stopCamera();
    throw new Error("离线缓存达到 80 MB，已关闭采集；请恢复连接后再开启");
  }
  await queueOp("readwrite", (s) =>
    s.put({
      id: crypto.randomUUID(),
      blob,
      taskId,
      kind,
      captured,
      conversationId: conversation,
      seq: Date.now() * 1000 + (seq++ % 1000),
    }),
  );
  flushQueue().catch((e) => toast(errorText(e)));
}
async function uploadBlob(
  blob,
  taskId = null,
  kind = "frame",
  id = crypto.randomUUID(),
  captured = Date.now() / 1000,
  sequence = Date.now() * 1000 + (seq++ % 1000),
  conversationId = conversation,
) {
  const form = new FormData();
  form.append("file", blob, kind === "frame" ? "frame.jpg" : "clip.webm");
  form.append("media_id", id);
  form.append("conversation_id", conversationId);
  form.append("captured", captured);
  form.append("seq", sequence);
  form.append("kind", kind);
  if (taskId) form.append("task_id", taskId);
  form.append(
    "origin",
    kind === "frame" && blob === pickedImage ? "upload" : "camera",
  );
  return json("/api/media", { method: "POST", body: form });
}
async function snapshot() {
  if (pickedImage) return pickedImage;
  if (!stream || !$("camera").videoWidth) return null;
  const c = document.createElement("canvas");
  const v = $("camera");
  const scale = Math.min(1, 1280 / v.videoWidth);
  c.width = v.videoWidth * scale;
  c.height = v.videoHeight * scale;
  c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
  return new Promise((resolve) => c.toBlob(resolve, "image/jpeg", 0.85));
}
async function tickCapture() {
  if (uploading || !stream || pickedImage || current?.status !== "active")
    return;
  uploading = true;
  const taskId = current.id;
  const captured = Date.now() / 1000;
  try {
    const blob = await snapshot();
    if (blob) await enqueue(blob, taskId, "frame", captured);
  } catch (e) {
    toast(errorText(e));
  } finally {
    uploading = false;
  }
}
async function startCamera() {
  if (!navigator.mediaDevices)
    throw new Error(
      "当前浏览器不支持相机，请使用 localhost 上的 Chrome 或 Safari",
    );
  stream = await navigator.mediaDevices.getUserMedia({
    video: { width: { ideal: 1280 }, height: { ideal: 720 } },
    audio: false,
  });
  pickedImage = null;
  $("uploaded-preview").hidden = true;
  $("clear-image").hidden = true;
  $("camera").srcObject = stream;
  $("camera-placeholder").hidden = true;
  $("camera-toggle").textContent = "关闭相机";
  clearInterval(captureTimer);
  captureTimer = setInterval(tickCapture, (config.frame_interval || 2) * 1000);
  syncCapture();
  stream.getVideoTracks()[0].onended = () => {
    stopCamera();
    toast("摄像头已断开，画面记录已停止");
  };
}
async function stopCamera() {
  clearInterval(captureTimer);
  captureGeneration++;
  stopClip();
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  $("camera").srcObject = null;
  $("camera-toggle").textContent = "开启相机";
  $("camera-placeholder").hidden = !!pickedImage;
  $("capture-status").textContent = "相机未开启";
}
function stopClip() {
  clearTimeout(clipTimer);
  if (clipRecorder && clipRecorder.state !== "inactive") clipRecorder.stop();
  clipRecorder = null;
  recordingTask = null;
}
function startClip() {
  if (
    !stream ||
    current?.status !== "active" ||
    !$("record-clips").checked ||
    !window.MediaRecorder
  )
    return;
  const taskId = current.id,
    generation = captureGeneration,
    start = Date.now() / 1000,
    chunks = [];
  const mime = ["video/webm;codecs=vp8", "video/webm", "video/mp4"].find((x) =>
    MediaRecorder.isTypeSupported(x),
  );
  if (!mime) {
    toast("浏览器不支持录像；仍会正常保存抽帧");
    return;
  }
  const rec = new MediaRecorder(stream, {
    mimeType: mime,
    videoBitsPerSecond: 700000,
  });
  clipRecorder = rec;
  recordingTask = taskId;
  rec.ondataavailable = (e) => {
    if (e.data.size) chunks.push(e.data);
  };
  rec.onstop = async () => {
    if (chunks.length) {
      try {
        await enqueue(new Blob(chunks, { type: mime }), taskId, "clip", start);
      } catch (e) {
        toast(errorText(e));
      }
    }
    if (
      generation === captureGeneration &&
      current?.id === taskId &&
      current?.status === "active" &&
      stream &&
      $("record-clips").checked
    ) {
      clipRecorder = null;
      startClip();
    }
  };
  rec.start();
  clipTimer = setTimeout(() => {
    if (rec.state !== "inactive") rec.stop();
  }, 10000);
}
function syncCapture() {
  $("capture-status").textContent = stream
    ? current?.status === "active"
      ? "● 正在记录任务"
      : "相机就绪 · 按需取图"
    : "相机未开启";
  if (!stream || current?.status !== "active" || !$("record-clips").checked) {
    if (clipRecorder) {
      captureGeneration++;
      stopClip();
    }
  } else if (!clipRecorder) startClip();
}

async function submitQuestion(e) {
  e?.preventDefault();
  if (busy) return;
  const text = $("question").value.trim();
  if (!text) return;
  setBusy(true);
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  message("user", text);
  try {
    const blob = await snapshot();
    let frame = null;
    if (blob)
      frame = await uploadBlob(
        blob,
        current?.status === "active" ? current.id : null,
      );
    const result = await json("/api/chat", {
      method: "POST",
      body: {
        request_id: crypto.randomUUID(),
        conversation_id: conversation,
        text,
        mode: $("mode").value,
        task_id: current?.id || null,
        frame_id: frame?.id || null,
      },
    });
    message("assistant", result.reply, result);
    $("question").value = "";
    speak(result.reply);
    await refresh();
  } catch (e) {
    message("error", errorText(e));
  } finally {
    setBusy(false);
  }
}
async function mutate(action, target = "", value = "") {
  if (!current) throw new Error("没有当前任务");
  current = await json(`/api/tasks/${current.id}/change`, {
    method: "POST",
    body: { version: current.version, action, target, value },
  });
  await refresh();
}
function renderTask(task) {
  const key = JSON.stringify(task);
  if (key === lastTaskRender) return;
  lastTaskRender = key;
  const content = $("task-content");
  content.replaceChildren();
  $("task-controls").hidden = !task;
  $("start-form").hidden = !!task;
  const names = {
    draft: "等待确认",
    active: "进行中",
    paused: "已暂停",
    ended: "已结束",
  };
  $("task-status").textContent = task
    ? `${names[task.status]} · v${task.state.plan_revision || 0}`
    : "没有进行中的任务";
  if (!task) return;
  content.append(element("h3", task.goal));
  if (task.state.preparing)
    content.append(element("p", "正在检索资料并生成操作表，请稍候…", "muted"));
  if (task.state.error)
    content.append(element("p", task.state.error, "warning"));
  if (task.state.notes?.length) {
    const d = element("details");
    d.append(element("summary", "用户说明（不是独立验证结果）"));
    task.state.notes.forEach((n) => d.append(element("p", n.text)));
    content.append(d);
  }
  const plan = task.plan;
  if (plan) {
    content.append(element("p", `适用准备：${plan.prerequisites.join("；")}`));
    if (plan.questions.length)
      content.append(
        element("p", `开始前请核对：${plan.questions.join("；")}`, "warning"),
      );
    if (plan.cautions.length)
      content.append(
        element("p", `注意：${plan.cautions.join("；")}`, "warning"),
      );
    const steps = element("div", undefined, "steps");
    for (const s of plan.steps) {
      const card = element("div", undefined, "step");
      card.append(
        element("h4", `${s.id} · ${s.title}`),
        element("p", s.instruction),
      );
      const ul = element("ul");
      s.checks.forEach((c) => ul.append(element("li", c)));
      card.append(ul);
      const select = element("select");
      [
        ["unknown", "未确认"],
        ["doing", "正在做"],
        ["user_reported_done", "我已做完（用户自报）"],
        ["needs_recheck", "需要重查 / 返工"],
      ].forEach(([v, t]) => {
        const o = element("option", t);
        o.value = v;
        select.append(o);
      });
      select.value = task.state.steps[s.id] || "unknown";
      select.onchange = () =>
        mutate("step", s.id, select.value).catch((e) => toast(errorText(e)));
      card.append(select);
      s.source_ids.forEach((id) =>
        card.append(linkButton("步骤来源", () => viewSource(id))),
      );
      steps.append(card);
    }
    content.append(steps);
  }
  if (task.state.open_issues?.length) {
    const issues = element("details");
    issues.append(element("summary", "待确认问题（AI 提出，不等于已证实）"));
    task.state.open_issues.forEach((issue) => {
      const row = element("div");
      row.append(element("p", issue.text));
      row.append(
        linkButton(issue.status === "open" ? "我已确认解决" : "重新打开", () =>
          mutate(
            "issue",
            issue.id,
            issue.status === "open" ? "resolved_by_user" : "open",
          ),
        ),
      );
      issues.append(row);
    });
    content.append(issues);
  }
  const sources = element("div", undefined, "sources");
  task.sources.forEach((s) =>
    sources.append(linkButton(s.title, () => viewSource(s.id))),
  );
  content.append(sources);
  const transitions = {
    proposed: [
      ["accepted", "接受建议"],
      ["rejected", "拒绝"],
    ],
    accepted: [
      ["executed", "我已执行"],
      ["rejected", "取消"],
    ],
    executed: [
      ["observed_success", "结果符合预期"],
      ["observed_failure", "结果不符合"],
      ["needs_recheck", "尚不确定"],
    ],
    observed_success: [["needs_recheck", "重新检查"]],
    observed_failure: [["needs_recheck", "重新检查"]],
    needs_recheck: [
      ["observed_success", "结果符合预期"],
      ["observed_failure", "结果不符合"],
    ],
  };
  task.suggestions.forEach((s) => {
    const card = element("div", undefined, "suggestion");
    card.append(
      element("p", s.text),
      element(
        "small",
        `建议状态：${s.status} · 接受不等于已执行，结果由用户确认`,
      ),
    );
    (transitions[s.status] || []).forEach(([value, label]) =>
      card.append(linkButton(label, () => mutate("suggestion", s.id, value))),
    );
    content.append(card);
  });
  $("confirm-plan").hidden = !(plan && !task.state.plan_confirmed);
  $("pause-task").hidden = task.status !== "active";
  $("resume-task").hidden = !(
    task.status === "paused" && task.state.plan_confirmed
  );
  $("finish-task").hidden = task.status === "ended";
}
async function refresh() {
  try {
    state = await json("/api/state");
    current = state.task;
    renderTask(current);
    syncCapture();
    $("connection").textContent = state.busy ? "正在处理" : "已连接";
    $("diagnostics").textContent =
      `模型：${state.model}；媒体 ${(state.media.bytes / 1048576).toFixed(1)} MB / ${config.max_storage_mb} MB；语义检索${state.semantic_enabled ? "启用" : "关闭（关键词与模型扩展仍可用）"}；事件 ${state.jobs.map((j) => `${j.status}:${j.count}`).join(" / ") || "暂无"}${state.worker_error ? "；" + state.worker_error : ""}`;
    const select = $("history-task"),
      selected = select.value;
    select.replaceChildren(new Option("选择任务", ""));
    state.tasks.forEach((t) =>
      select.add(new Option(`${t.goal} · ${t.status}`, t.id)),
    );
    select.value = state.tasks.some((t) => t.id === selected)
      ? selected
      : current?.id || "";
    await showQueue();
  } catch (e) {
    $("connection").textContent = "连接中断 · 记录暂存本机";
    queueError = errorText(e);
  }
}
async function prepareTask() {
  if (!current) throw new Error("没有当前任务");
  setBusy(true, "正在搜索、读取来源并准备操作表…");
  try {
    await json(`/api/tasks/${current.id}/prepare`, {
      method: "POST",
      body: {
        url: $("source-url").value || null,
        reason: $("revision-reason").value,
      },
    });
  } finally {
    setBusy(false);
    await refresh();
  }
}
async function timeline() {
  const id = $("history-task").value;
  if (!id) return;
  const data = await json(`/api/tasks/${id}/timeline`);
  const root = $("timeline");
  root.replaceChildren();
  if (!data.events.length)
    root.append(
      element(
        "p",
        "暂无已整理事件；原始图片可能已保存，请查看下方记录。",
        "muted",
      ),
    );
  for (const e of data.events) {
    const d = element("div", undefined, "timeline-event");
    d.append(
      element(
        "small",
        `${new Date(e.start * 1000).toLocaleTimeString()} – ${new Date(e.end * 1000).toLocaleTimeString()}`,
      ),
      element("p", e.data.summary),
    );
    if (e.data.unknowns.length)
      d.append(element("p", `未知：${e.data.unknowns.join("；")}`, "warning"));
    for (const f of [e.frame_ids[0], e.frame_ids.at(-1)].filter(Boolean))
      d.append(linkButton("查看原图", () => viewMedia(f)));
    root.append(d);
  }
  const raw = element("details");
  raw.append(element("summary", `最近原始媒体（${data.media.length} 条）`));
  data.media.slice(0, 40).forEach((m) => {
    const b = linkButton(
      `${new Date(m.captured * 1000).toLocaleTimeString()} ${m.kind}${m.expired ? " · 已过期" : ""}`,
      () => viewMedia(m.id, m.kind),
    );
    b.disabled = !!m.expired;
    raw.append(b);
  });
  root.append(raw);
  const log = element("details");
  log.append(element("summary", "决策记录与计划修订"));
  data.ledger.forEach((l) =>
    log.append(
      element(
        "p",
        `${new Date(l.created * 1000).toLocaleTimeString()} · ${l.kind}\n${l.data}`,
      ),
    ),
  );
  root.append(log);
  const oldTask = await json(`/api/tasks/${id}`);
  if (oldTask.plan) {
    const p = element("details");
    p.append(
      element("summary", "查看该任务保存的操作表"),
      element("pre", JSON.stringify(oldTask.plan, null, 2)),
    );
    root.append(p);
  }
  root.append(linkButton("查看该任务对话", () => refreshMessages(id)));
}

bind("camera-toggle", "click", () => (stream ? stopCamera() : startCamera()));
bind("image-input", "change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  if (f.size > 12 * 1024 * 1024) throw new Error("图片不得超过 12 MB");
  await stopCamera();
  pickedImage = f;
  const url = URL.createObjectURL(f);
  $("uploaded-preview").src = url;
  $("uploaded-preview").hidden = false;
  $("camera-placeholder").hidden = true;
  $("clear-image").hidden = false;
});
bind("clear-image", "click", () => {
  pickedImage = null;
  $("image-input").value = "";
  $("uploaded-preview").hidden = true;
  $("clear-image").hidden = true;
  $("camera-placeholder").hidden = false;
});
bind("record-clips", "change", syncCapture);
bind("chat-form", "submit", submitQuestion);
bind("question", "keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    return submitQuestion();
  }
});
bind("stop-speech", "click", () => window.speechSynthesis?.cancel());
bind("start-form", "submit", async (e) => {
  e.preventDefault();
  if (busy) return;
  current = await json("/api/tasks", {
    method: "POST",
    body: { goal: $("goal").value },
  });
  await refresh();
  await prepareTask();
});
bind("confirm-plan", "click", async () => {
  if (
    !confirm("确认资料适用于你的任务、已核对待确认条件，并同意开始持续记录？")
  )
    return;
  await mutate("confirm_plan", "", "已核对适用条件");
});
bind("pause-task", "click", () => mutate("pause"));
bind("resume-task", "click", () => mutate("resume"));
bind("finish-task", "click", async () => {
  if (confirm("结束本次任务并停止持续记录？")) await mutate("finish");
});
bind("note-form", "submit", async (e) => {
  e.preventDefault();
  await mutate("note", "", $("note").value);
  $("note").value = "";
});
bind("prepare-form", "submit", async (e) => {
  e.preventDefault();
  await prepareTask();
});
bind("source-file", "change", async (e) => {
  const f = e.target.files[0];
  if (!f || !current) return;
  if (f.size > 10 * 1024 * 1024) throw new Error("说明书不得超过 10 MB");
  const form = new FormData();
  form.append("file", f);
  await json(`/api/tasks/${current.id}/documents`, {
    method: "POST",
    body: form,
  });
  toast("资料已保存，请重新生成计划");
  await refresh();
});
bind("timeline-refresh", "click", timeline);
bind("delete-task", "click", async () => {
  const id = $("history-task").value;
  if (id && confirm("永久删除所选任务、对话及本地原始媒体？")) {
    await json(`/api/tasks/${id}`, { method: "DELETE" });
    if (queueDB) {
      const rows = await queueOp("readonly", (s) => s.getAll());
      for (const row of rows.filter((r) => r.taskId === id))
        await queueOp("readwrite", (s) => s.delete(row.id));
    }
    $("timeline").replaceChildren();
    await refresh();
  }
});
bind("retry-jobs", "click", async () => {
  await json("/api/memory/retry", { method: "POST" });
  toast("已重新排队");
});
bind("close-dialog", "click", () => {
  $("evidence-dialog").close();
  blobURLs.forEach((url) => URL.revokeObjectURL(url));
  blobURLs.clear();
});
bind("mic", "click", async () => {
  if (voiceRecorder?.state === "recording") {
    voiceRecorder.stop();
    return;
  }
  if (busy) return;
  window.speechSynthesis?.cancel();
  voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const chunks = [];
  voiceRecorder = new MediaRecorder(voiceStream);
  $("mic").textContent = "停止录音";
  voiceRecorder.ondataavailable = (e) => {
    if (e.data.size) chunks.push(e.data);
  };
  voiceRecorder.onstop = async () => {
    clearTimeout(voiceTimer);
    voiceStream.getTracks().forEach((t) => t.stop());
    $("mic").textContent = "正在识别…";
    $("mic").disabled = true;
    try {
      const form = new FormData();
      form.append(
        "file",
        new Blob(chunks, { type: voiceRecorder.mimeType }),
        "voice.webm",
      );
      const result = await json("/api/speech", { method: "POST", body: form });
      $("question").value = result.text;
      toast("语音已识别，请确认后发送");
    } catch (e) {
      toast(errorText(e));
    } finally {
      $("mic").textContent = "按下说话";
      $("mic").disabled = false;
    }
  };
  voiceRecorder.start();
  voiceTimer = setTimeout(() => {
    if (voiceRecorder.state === "recording") voiceRecorder.stop();
  }, 60000);
});
window.addEventListener("beforeunload", () => {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  if (voiceStream) voiceStream.getTracks().forEach((t) => t.stop());
});
window.addEventListener("online", () => flushQueue());
(async () => {
  try {
    config = await (await fetch("/bootstrap")).json();
    token = config.token;
    queueDB = await openQueue();
    await refresh();
    await refreshMessages(current?.id || null);
    await flushQueue();
    setInterval(refresh, 3000);
    setInterval(flushQueue, 5000);
  } catch (e) {
    toast(errorText(e));
  }
})();
