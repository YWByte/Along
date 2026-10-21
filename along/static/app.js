"use strict";
const { t: tr } = window.AlongI18n;
window.AlongI18n.apply();
const $ = (id) => document.getElementById(id);
let mode = "home";
let conversation = "";
let cameraPending = false;
let voiceBusy = false;
let ready = false;
let refreshNumber = 0;
function conversationFor(name) {
  const key = `along-${name}-conversation`;
  return (sessionStorage[key] ||= crypto.randomUUID());
}
let token = "",
  config = {},
  current = null,
  state = null,
  stream = null;
let busy = false,
  uploading = false,
  captureTimer,
  voiceRecorder,
  voiceStream,
  voiceTimer;
let seq = 0,
  lastTaskRender = "";
let queueDB,
  queueSending = false,
  queueError = "";
let lastAnswer = null;
let voiceHold = { pressed: 0, release: false, toggle: false };
const blobURLs = new Set();
const BASIS = {
  general_knowledge: "通用知识，未经资料核验",
  references: "依据参考资料",
  mixed: "部分步骤有资料依据",
};
function element(tag, text, className) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  if (className) e.className = className;
  return e;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    $("toast").hidden = true;
  }, 6000);
}
function errorText(e) {
  return tr(e.message || String(e));
}
function bind(id, event, handler) {
  $(id).addEventListener(event, (e) =>
    Promise.resolve(handler(e)).catch((err) => toast(errorText(err))),
  );
}
function place(node, slotId) {
  const slot = $(slotId);
  if (slot && node.parentElement !== slot) slot.append(node);
}
function clock(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString(
    document.documentElement.lang,
    {
      hour12: false,
    },
  );
}
function two(n) {
  return String(n).padStart(2, "0");
}
async function api(path, options = {}) {
  const headers = {
    "X-Along-Token": token,
    "X-Along-Language": window.AlongI18n.language,
    ...options.headers,
  };
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
function setBusy(value, message = tr("正在想下一步…")) {
  busy = value;
  if (value) refreshNumber++;
  $("activity").textContent = value ? message : "";
  document.body.classList.toggle("thinking", value);
  if (!value && $("answer").classList.contains("thinking")) hideAnswer();
  updateControls();
}
function taskStage() {
  if (!current) return "goal";
  if (current.state.plan_confirmed) return "run";
  return "plan";
}
function updateControls() {
  const locked = busy || voiceBusy || cameraPending;
  const preparing = mode === "task" && !!current?.state.preparing;
  const recording = voiceRecorder?.state === "recording";
  $("language-toggle").disabled = locked || preparing;
  $("back-home").disabled = locked;
  $("enter-task").disabled = !ready || locked;
  $("question").disabled = busy || preparing;
  $("send").disabled = locked || preparing;
  $("mic").disabled =
    busy || preparing || cameraPending || (voiceBusy && !recording);
  $("camera-toggle").disabled = locked;
  for (const el of document.querySelectorAll(
    "#task button:not(#mic), #drawer button, #drawer input, #drawer select",
  ))
    el.disabled = locked || preparing;
  if (preparing) $("finish-task").disabled = locked;
  $("pause-task").disabled =
    locked ||
    preparing ||
    mode !== "task" ||
    !current?.state.plan_confirmed ||
    !["active", "paused"].includes(current?.status);
  $("open-menu").disabled = false;
  $("close-menu").disabled = false;
}
function renderScreen() {
  const stage = taskStage();
  const running = mode === "task" && stage === "run";
  const active = running && current?.status === "active";
  document.body.dataset.mode = mode;
  document.body.dataset.stage = mode === "task" ? stage : "";
  $("home").hidden = mode !== "home";
  $("task").hidden = mode !== "task";
  $("back-home").hidden = mode === "home";
  $("mode-title").textContent = mode === "task" ? tr("一起做") : "";
  $("stage").textContent =
    mode !== "task"
      ? ""
      : stage === "run"
        ? current.status === "paused"
          ? tr("任务已暂停")
          : tr("一起操作")
        : stage === "plan"
          ? current.state.pending_questions?.length
            ? tr("补充条件")
            : tr("确认方案")
          : tr("尚未开始任务");
  $("task-controls").hidden = mode !== "task";
  $("pause-task").hidden = false;
  $("pause-task").textContent =
    current?.status === "paused" ? tr("继续") : tr("暂停");
  $("pause-task").title = !current
    ? tr("尚未开始任务")
    : !current.state.plan_confirmed
      ? tr("确认方案后才能暂停")
      : "";
  $("open-menu").setAttribute("aria-expanded", String(!$("drawer").hidden));
  $("drawer-empty").hidden = !!current;
  document.querySelectorAll("[data-requires-task]").forEach((el) => {
    el.hidden = !current;
  });
  $("task-goal").hidden = stage !== "goal";
  $("task-plan").hidden = stage !== "plan";
  $("task-run").hidden = stage !== "run";
  $("paused-overlay").hidden = !running || current.status !== "paused";
  $("chat-form").hidden = mode === "home";
  if (mode === "task") {
    place($("viewfinder"), running ? "pip-slot" : "goal-view-slot");
    place($("chat-form"), `${stage}-composer-slot`);
    place(
      $("messages"),
      running || !$("drawer").hidden ? "drawer-thread" : `${stage}-thread`,
    );
  }
  $("camera-toggle").hidden = !!stream || cameraPending || mode !== "task";
  $("capture-status").hidden = !(active && stream);
  const pending = current?.state.pending_questions?.length;
  $("question").placeholder =
    stage === "goal"
      ? tr("说说你想完成的事…")
      : stage === "plan"
        ? pending
          ? tr("回答上面的问题…")
          : tr("哪里不合适？直接说，我来改")
        : tr("哪里不确定？问我…");
  $("messages").hidden = !$("messages").children.length;
  updateControls();
}
async function enterMode(next) {
  if (!ready || busy || voiceBusy || cameraPending || mode !== "home") return;
  mode = next;
  conversation = conversationFor(next);
  hideAnswer();
  $("question").value = "";
  $("messages").replaceChildren();
  lastTaskRender = "";
  renderTask(current);
  renderScreen();
  await openCamera();
  if (next === "task") {
    setBusy(true, tr("正在恢复对话…"));
    try {
      await refreshMessages(current?.id || null);
    } finally {
      setBusy(false);
    }
  }
}
async function goHome() {
  if (busy || voiceBusy || cameraPending) return;
  closeMenu();
  if (mode === "task" && current?.status === "active") {
    // Stop capture before the pause round-trip so no frame lands after leaving.
    await stopCamera();
    await mutate("pause");
    toast(tr("任务已暂停，回来点“继续”即可"));
  }
  await stopCamera();
  hideAnswer();
  mode = "home";
  renderScreen();
}
function cameraPlaceholder(key) {
  $("camera-placeholder").dataset.i18n = key;
  $("camera-placeholder").textContent = tr(key);
}
async function openCamera() {
  if (stream || cameraPending) return;
  cameraPending = true;
  cameraPlaceholder("请允许使用相机…");
  $("camera-placeholder").hidden = false;
  renderScreen();
  try {
    await startCamera();
  } catch (e) {
    cameraPlaceholder("相机没有打开，检查权限后重试");
    toast(errorText(e));
  } finally {
    cameraPending = false;
    renderScreen();
  }
}
function linkButton(label, callback, className) {
  const b = element("button", label, className);
  b.type = "button";
  b.onclick = () =>
    Promise.resolve(callback()).catch((e) => toast(errorText(e)));
  return b;
}
async function mediaURL(id) {
  const response = await api(`/api/media/${id}`);
  const url = URL.createObjectURL(await response.blob());
  blobURLs.add(url);
  return url;
}
async function viewMedia(id, kind = "frame") {
  const url = await mediaURL(id);
  const content = $("evidence-content");
  content.replaceChildren(element("p", tr("原始记录 {0}", [id]), "eyebrow"));
  const el = element(kind === "frame" ? "img" : "video");
  el.src = url;
  if (kind !== "frame") el.controls = true;
  content.append(el);
  if (!$("evidence-dialog").open) $("evidence-dialog").showModal();
}
async function viewSource(id) {
  const s = await json(`/api/sources/${id}`);
  $("evidence-content").replaceChildren(
    element("h3", s.title),
    element("p", s.url, "muted"),
    element("pre", s.content),
  );
  if (!$("evidence-dialog").open) $("evidence-dialog").showModal();
}
const VERDICTS = { ok: "做对了", adjust: "调整一下" };
const MARKS = { met: "✓", not_met: "✗", unseen: "?" };
function headlineOf(data, text) {
  const h = (data.headline || "").trim();
  if (h) return h;
  const first = (text || "").split(/(?<=[。！？!?])/)[0].trim();
  return first.length <= 40 ? first : "";
}
function evidenceFrames(data) {
  const frames = [];
  for (const t of data.retrieval?.tools || [])
    for (const f of t.result?.frames || []) frames.push(f);
  return frames;
}
function hasEvidence(data) {
  return !!(
    data.evidence_ids?.length ||
    data.source_ids?.length ||
    data.retrieval?.visual_reviews?.some((r) => r.result)
  );
}
function evidenceLine(data) {
  const frames = evidenceFrames(data);
  const parts = [];
  if (frames.length) {
    const times = frames.map((f) => f.captured).sort((a, b) => a - b);
    parts.push(
      tr("回看 {0}–{1}", [clock(times[0]), clock(times.at(-1))]),
      tr("{0} 帧", [new Set(frames.map((f) => f.id)).size]),
    );
  } else if (data.evidence_ids?.length)
    parts.push(tr("{0} 张画面", [data.evidence_ids.length]));
  if (data.source_ids?.length)
    parts.push(tr("{0} 份资料", [data.source_ids.length]));
  return parts.join(" · ");
}
async function thumb(id, label) {
  const b = element("button", undefined, "thumb");
  b.type = "button";
  b.setAttribute("aria-label", label);
  b.onclick = () => viewMedia(id).catch((e) => toast(errorText(e)));
  const img = element("img");
  img.alt = label;
  b.append(img, element("span", label));
  mediaURL(id)
    .then((url) => (img.src = url))
    .catch(() => b.classList.add("missing"));
  return b;
}
async function openEvidence(data) {
  const root = $("evidence-content");
  root.replaceChildren(element("p", tr("这次回答的依据"), "eyebrow"));
  if (data.review_focus?.subject)
    root.append(
      element("h3", tr("本次核对：{0}", [data.review_focus.subject])),
    );
  for (const review of data.retrieval?.visual_reviews || []) {
    if (!review.result) continue;
    root.append(
      element("p", review.result.summary),
      element("p", tr("以上是画面观察，不是动作或安全认证。"), "muted"),
    );
    for (const change of review.result.changes || []) {
      const group = element("div", undefined, "change");
      group.append(element("p", change.claim));
      const row = element("div", undefined, "thumbs");
      for (const [i, id] of change.frame_ids.entries())
        row.append(await thumb(id, tr("对比帧 {0}", [i + 1])));
      group.append(row);
      root.append(group);
    }
  }
  if (data.evidence_ids?.length) {
    root.append(element("p", tr("引用的画面"), "eyebrow"));
    const row = element("div", undefined, "thumbs");
    for (const [i, id] of data.evidence_ids.entries())
      row.append(await thumb(id, tr("过程图片 {0}", [i + 1])));
    root.append(row);
  }
  data.source_ids?.forEach((id, i) =>
    root.append(
      linkButton(tr("参考资料 {0}", [i + 1]), () => viewSource(id), "chip"),
    ),
  );
  if (!$("evidence-dialog").open) $("evidence-dialog").showModal();
}
const TOOL_NAMES = {
  open: "回看",
  current: "当前画面",
  browse: "浏览目录",
  search: "搜索记录",
};
function contextSteps(data) {
  const steps = [];
  const asked = data.retrieval?.question_frames || [];
  if (asked.length)
    steps.push({ label: tr("提问时画面"), frames: asked, sheet: null });
  for (const t of data.retrieval?.tools || []) {
    const r = t.result || {},
      c = t.call || {};
    const frames = r.frames || [];
    const parts = [tr(TOOL_NAMES[c.tool] || c.tool)];
    if (c.step_id) parts.push(c.step_id);
    if (c.query) parts.push(`“${c.query}”`);
    if (frames.length)
      parts.push(
        `${clock(frames[0].captured)}–${clock(frames.at(-1).captured)}`,
        r.layout === "sheet"
          ? tr("拼图 {0} 帧", [frames.length])
          : tr("{0} 帧", [frames.length]),
      );
    else if (r.events) parts.push(tr("{0} 条事件", [r.events.length]));
    steps.push({
      label: parts.join(" · "),
      frames,
      sheet: r.layout === "sheet" ? r.sheet_id : null,
      error: frames.length ? null : r.error,
    });
    if (r.detail_frames?.length)
      steps.push({
        label: tr("引脚细节原图"),
        frames: r.detail_frames,
        sheet: null,
      });
  }
  return steps;
}
async function sheetURL(id) {
  const response = await api(`/api/sheets/${id}`);
  const url = URL.createObjectURL(await response.blob());
  blobURLs.add(url);
  return url;
}
function contextImage(load, label, onOpen) {
  const b = element("button", undefined, "ctx-img");
  b.type = "button";
  b.setAttribute("aria-label", label);
  const img = element("img");
  img.alt = label;
  b.append(img);
  load()
    .then((url) => {
      img.src = url;
      b.onclick = () => onOpen(url);
    })
    .catch(() => b.classList.add("missing"));
  return b;
}
function showImage(url, title) {
  const el = element("img");
  el.src = url;
  $("evidence-content").replaceChildren(element("p", title, "eyebrow"), el);
  if (!$("evidence-dialog").open) $("evidence-dialog").showModal();
}
function renderContext(steps, root) {
  for (const step of steps) {
    const group = element("div", undefined, "ctx-step");
    group.append(element("p", step.label, "ctx-label"));
    if (step.error) group.append(element("p", step.error, "ctx-error"));
    const row = element("div", undefined, "ctx-row");
    // A sheet is the exact image the model saw; fall back to its frames once the cache is gone.
    const sheetFailed = () => {
      row.replaceChildren();
      tiles();
    };
    const tiles = () =>
      step.frames.forEach((f, i) =>
        row.append(
          contextImage(
            () => mediaURL(f.id),
            tr("{0} 第 {1} 帧 {2}", [step.label, i + 1, clock(f.captured)]),
            (url) => showImage(url, `${clock(f.captured)} · ${f.id}`),
          ),
        ),
      );
    if (step.sheet) {
      const b = contextImage(
        () =>
          sheetURL(step.sheet).catch((e) => (sheetFailed(), Promise.reject(e))),
        tr("{0} 拼图", [step.label]),
        (url) => showImage(url, step.label),
      );
      b.classList.add("sheet");
      row.append(b);
    } else tiles();
    if (step.frames.length) group.append(row);
    root.append(group);
  }
}
function contextPanel(data) {
  const steps = contextSteps(data);
  const images = steps.reduce((n, s) => n + (s.sheet ? 1 : s.frames.length), 0);
  if (!steps.length) return null;
  const panel = element("details", undefined, "ctx");
  const lookups = steps.filter((s) => s.label !== tr("提问时画面")).length;
  panel.append(
    element(
      "summary",
      tr("AI 看过的图片 · {0} 张{1}", [
        images,
        lookups ? tr(" · {0} 次检索", [lookups]) : "",
      ]),
    ),
  );
  panel.addEventListener("toggle", () => {
    if (panel.open && panel.children.length === 1) renderContext(steps, panel);
  });
  return panel;
}
function message(role, text, data = {}) {
  const box = element("article", undefined, role);
  box.alongMessage = { role, text, data };
  if (role === "assistant") {
    const headline = headlineOf(data, text);
    if (VERDICTS[data.verdict])
      box.append(
        element(
          "span",
          tr(VERDICTS[data.verdict]),
          `verdict v-${data.verdict}`,
        ),
      );
    if (headline) box.append(element("strong", headline, "headline"));
    if (text && text.trim() !== headline) box.append(element("p", text));
    if (mode === "task" && data.suggestion)
      box.append(element("p", tr("试试：{0}", [data.suggestion]), "try"));
    if (mode === "task" && hasEvidence(data))
      box.append(
        linkButton(
          tr("依据 · {0}", [evidenceLine(data) || tr("查看")]),
          () => openEvidence(data),
          "evidence-link",
        ),
      );
    const panel = mode === "task" && contextPanel(data);
    if (panel) box.append(panel);
  } else box.append(element("p", text));
  if (data.warnings?.length)
    box.append(element("p", data.warnings.join("；"), "warning"));
  $("messages").append(box);
  $("messages").hidden = false;
  $("messages").scrollTop = $("messages").scrollHeight;
  const slot = $("messages").parentElement;
  if (slot) slot.scrollTop = slot.scrollHeight;
}
function hideAnswer() {
  lastAnswer = null;
  clearTimeout(hideAnswer.timer);
  $("answer").hidden = true;
  $("answer").replaceChildren();
}
function showThinking(text) {
  const card = $("answer");
  card.className = "answer thinking";
  card.replaceChildren(
    element("span", undefined, "dots"),
    element("strong", text, "headline"),
  );
  card.hidden = false;
}
function showAnswer(data, error = "") {
  const card = $("answer");
  clearTimeout(hideAnswer.timer);
  card.className = error ? "answer error" : "answer";
  card.replaceChildren();
  const close = linkButton("✕", hideAnswer, "ghost close");
  close.setAttribute("aria-label", tr("收起回答"));
  if (error) {
    card.append(close, element("strong", error, "headline"));
    card.hidden = false;
    return;
  }
  lastAnswer = data;
  const headline = headlineOf(data, data.reply) || data.reply;
  const verdict = tr(VERDICTS[data.verdict] || "");
  if (verdict) card.classList.add(`verdict-${data.verdict}`);
  card.append(close);
  if (verdict) card.append(element("span", verdict, "verdict"));
  card.append(element("strong", headline, "headline"));
  const detail = element("div", undefined, "detail");
  if (data.connection_checks?.length) {
    const ul = element("ul", undefined, "criteria");
    for (const check of data.connection_checks) {
      const connection = current?.plan?.connections?.find(
        (c) => c.id === check.connection_id,
      );
      const label = connection
        ? `${connection.from_pin} → ${connection.to_pin}`
        : check.connection_id;
      const li = element("li", label, `c-${check.status}`);
      li.prepend(element("span", MARKS[check.status], "mark"));
      ul.append(li);
    }
    detail.append(ul);
  }
  if (data.criteria?.length) {
    const ul = element("ul", undefined, "criteria");
    for (const c of data.criteria) {
      const li = element("li", c.criterion, `c-${c.status}`);
      li.prepend(element("span", MARKS[c.status], "mark"));
      ul.append(li);
    }
    detail.append(ul);
  }
  if (data.reply && data.reply.trim() !== headline)
    detail.append(element("p", data.reply));
  if (data.suggestion)
    detail.append(element("p", tr("试试：{0}", [data.suggestion]), "try"));
  if (data.checks?.length) {
    const ul = element("ul", undefined, "checks");
    data.checks.forEach((c) => ul.append(element("li", c)));
    detail.append(ul);
  }
  if (data.warnings?.length)
    detail.append(element("p", data.warnings.join("；"), "warning"));
  if (detail.children.length) card.append(detail);
  if (hasEvidence(data))
    card.append(
      linkButton(
        tr("依据 · {0}", [evidenceLine(data) || tr("查看")]),
        () => openEvidence(data),
        "evidence-link",
      ),
    );
  card.onclick = (e) => {
    if (e.target === card || e.target.classList.contains("headline"))
      card.classList.toggle("compact");
  };
  card.hidden = false;
  // Collapse rather than vanish: the user may glance back mid-action.
  hideAnswer.timer = setTimeout(() => card.classList.add("compact"), 25000);
}
async function refreshMessages(taskId = null) {
  const rows = await json(
    `/api/messages/${conversation}${taskId ? `?task_id=${taskId}` : ""}`,
  );
  $("messages").replaceChildren();
  rows.forEach((m) =>
    message(m.role, m.text, m.data ? JSON.parse(m.data) : {}),
  );
  renderScreen();
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
          toast(tr("存储已满，已关闭采集；待补传数据仍保留在本机"));
          break;
        }
        if ([404, 409, 422].includes(e.status)) {
          toast(tr("一段缓存记录无法补传：{0}", [queueError]));
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
    ? tr("本地待补传 {0} 段{1}", [
        rows.length,
        queueError ? " · " + queueError : "",
      ])
    : current?.status === "active"
      ? tr("任务进行中：画面按间隔保存，事件在后台整理")
      : tr("准备阶段按需取图；任务暂停时不持续记录");
}
async function enqueue(blob, taskId, kind, captured = Date.now() / 1000) {
  const rows = await queueOp("readonly", (s) => s.getAll());
  if (
    rows.reduce((n, r) => n + r.blob.size, 0) + blob.size >
    80 * 1024 * 1024
  ) {
    await stopCamera();
    throw new Error(tr("离线缓存达到 80 MB，已关闭采集；请恢复连接后再开启"));
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
    taskId && current?.status !== "active" ? "reference" : "camera",
  );
  return json("/api/media", { method: "POST", body: form });
}
async function snapshot() {
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
  if (mode !== "task" || uploading || !stream || current?.status !== "active")
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
      tr("当前浏览器不支持相机，请在 localhost 用 Chrome 或 Safari"),
    );
  stream = await navigator.mediaDevices.getUserMedia({
    video: { width: { ideal: 1280 }, height: { ideal: 720 } },
    audio: false,
  });
  $("camera").srcObject = stream;
  $("camera-placeholder").hidden = true;
  clearInterval(captureTimer);
  captureTimer = setInterval(tickCapture, (config.frame_interval || 1) * 1000);
  syncCapture();
  stream.getVideoTracks()[0].onended = () => {
    stopCamera();
    toast(tr("摄像头已断开，画面记录已停止"));
  };
}
async function stopCamera() {
  clearInterval(captureTimer);
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  $("camera").srcObject = null;
  cameraPlaceholder(
    mode === "task" && current?.status !== "active" ? "已暂停" : "相机未开启",
  );
  $("camera-placeholder").hidden = false;
  renderScreen();
}
function syncCapture() {
  renderScreen();
}
async function submitQuestion(e) {
  e?.preventDefault();
  if (
    busy ||
    voiceBusy ||
    cameraPending ||
    mode === "home" ||
    (mode === "task" && current?.state.preparing)
  )
    return;
  const text = $("question").value.trim();
  if (!text) return;
  const running = mode === "task" && current?.state.plan_confirmed;
  setBusy(
    true,
    mode === "task" && !current
      ? tr("正在理解目标，准备方案…")
      : mode === "task" && !running
        ? tr("正在调整方案…")
        : running
          ? tr("“{0}” 正在想下一步…", [
              text.length > 18 ? text.slice(0, 18) + "…" : text,
            ])
          : tr("正在看这张照片…"),
  );
  message("user", text);
  if (running) showThinking($("activity").textContent);
  try {
    if (mode === "task" && current && !current.state.plan_confirmed) {
      if (text.length > 2000)
        throw new Error(tr("调整说明请控制在 2000 字以内"));
      await json(`/api/tasks/${current.id}/change`, {
        method: "POST",
        body: { version: current.version, action: "note", value: text },
      });
      const blob = await snapshot();
      const reference = blob
        ? await uploadBlob(
            blob,
            current.id,
            "frame",
            crypto.randomUUID(),
            Date.now() / 1000,
          )
        : null;
      const prepared = await json(`/api/tasks/${current.id}/prepare`, {
        method: "POST",
        body: { reason: text, frame_id: reference?.id || null },
      });
      message(
        "assistant",
        prepared.state.pending_questions?.length
          ? prepared.state.pending_questions.join("；")
          : tr("方案改好了，看看是否合适。"),
      );
    } else {
      const blob = await snapshot();
      let frame = null;
      if (blob)
        frame = await uploadBlob(
          blob,
          current?.id || null,
          "frame",
          crypto.randomUUID(),
          Date.now() / 1000,
        );
      const result = await json("/api/chat", {
        method: "POST",
        body: {
          request_id: crypto.randomUUID(),
          conversation_id: conversation,
          text:
            mode === "task" && !current
              ? tr("请带我一起完成这个任务：{0}", [text])
              : text,
          mode,
          task_id: mode === "task" ? current?.id || null : null,
          frame_id: frame?.id || null,
        },
      });
      message("assistant", result.reply, result);
      if (running) showAnswer(result);
    }
    $("question").value = "";
    autosize();
  } catch (e) {
    message("error", errorText(e));
    if (running) showAnswer({}, errorText(e));
  } finally {
    await refresh();
    setBusy(false);
  }
}
async function mutate(action, target = "", value = "") {
  if (!current) throw new Error(tr("没有当前任务"));
  if (busy) return;
  setBusy(true, tr("正在保存…"));
  try {
    current = await json(`/api/tasks/${current.id}/change`, {
      method: "POST",
      body: { version: current.version, action, target, value },
    });
    if (current.status !== "active") await stopCamera();
  } finally {
    await refresh();
    setBusy(false);
  }
}
function list(items, className) {
  const ul = element("ul", undefined, className);
  items.forEach((t) => ul.append(element("li", t)));
  return ul;
}
function currentIndex(task) {
  return task.plan.steps.findIndex(
    (s) => task.state.steps[s.id] !== "user_reported_done",
  );
}
function connectionTable(plan) {
  if (!plan?.connections?.length) return null;
  const wrap = element("div", undefined, "connection-table");
  const table = element("table");
  table.setAttribute("aria-label", tr("接线表"));
  const head = element("tr");
  [tr("从"), tr("接到"), tr("建议线色")].forEach((text) =>
    head.append(element("th", text)),
  );
  const thead = element("thead");
  thead.append(head);
  table.append(thead);
  const body = element("tbody");
  plan.connections.forEach((c) => {
    const row = element("tr");
    [c.from_pin, c.to_pin, c.wire_color || tr("不限")].forEach((text) =>
      row.append(element("td", text)),
    );
    body.append(row);
  });
  table.append(body);
  wrap.append(table);
  return wrap;
}
function renderPlanCard(task, questions) {
  const plan = questions.length ? null : task.plan;
  $("plan-title").textContent = task.goal;
  const body = $("plan-body");
  body.replaceChildren();
  if (task.state.preparing) {
    $("plan-meta").textContent = tr("准备中");
    body.append(
      element("p", tr("正在判断要不要查资料，然后写方案…"), "muted pulse"),
    );
  } else if (questions.length) {
    $("plan-meta").textContent = tr("开始前，先确认一件事");
    body.append(list(questions, "questions"));
    body.append(element("p", tr("在下面直接回答就好。"), "muted"));
  } else if (plan) {
    const basis =
      plan.basis ||
      (plan.steps.some((s) => s.source_ids.length)
        ? "references"
        : "general_knowledge");
    $("plan-meta").textContent = [
      tr("{0} 步", [plan.steps.length]),
      tr(BASIS[basis]),
    ]
      .filter(Boolean)
      .join(" · ");
    if (plan.prerequisites.length) {
      body.append(element("p", tr("准备"), "label"));
      body.append(list(plan.prerequisites, "ticks"));
    }
    const connections = connectionTable(plan);
    if (connections)
      body.append(element("p", tr("接线参考"), "label"), connections);
    body.append(element("p", tr("步骤"), "label"));
    const ol = element("ol", undefined, "plan-steps");
    plan.steps.forEach((s) => {
      const li = element("li");
      li.append(element("strong", s.title), element("p", s.instruction));
      s.source_ids.forEach((id) =>
        li.append(linkButton(tr("来源"), () => viewSource(id), "chip tiny")),
      );
      ol.append(li);
    });
    body.append(ol);
    const notes = [...plan.questions, ...plan.cautions];
    if (notes.length) {
      body.append(element("p", tr("注意"), "label"));
      body.append(list(notes, "notes"));
    }
  }
  if (task.state.error) body.append(element("p", task.state.error, "warning"));
  $("confirm-plan").hidden = !plan || task.state.preparing;
  $("record-consent").hidden = !plan || task.state.preparing;
  $("retry-plan").hidden = !!plan || task.state.preparing || !!questions.length;
}
function renderRun(task) {
  const plan = task.plan;
  const progress = $("progress");
  progress.replaceChildren();
  const index = currentIndex(task);
  plan.steps.forEach((s, i) => {
    const li = element("li");
    li.title = s.title;
    if (task.state.steps[s.id] === "user_reported_done") li.className = "done";
    if (i === index) li.className = "now";
    progress.append(li);
  });
  const content = $("task-content");
  content.replaceChildren();
  if (index < 0) {
    const done = element("div", undefined, "current-step finished");
    done.append(
      element(
        "p",
        `${two(plan.steps.length)} / ${two(plan.steps.length)}`,
        "count",
      ),
      element("h2", tr("都做完了")),
      element("p", tr("所有步骤都由你确认完成。"), "instruction"),
    );
    content.append(done);
  } else {
    const step = plan.steps[index];
    const card = element("div", undefined, "current-step");
    card.append(
      element("p", `${two(index + 1)} / ${two(plan.steps.length)}`, "count"),
      element("h2", step.title),
      element("p", step.instruction, "instruction"),
    );
    const marks = [...(step.look_for || []), ...step.checks].slice(0, 3);
    if (marks.length) {
      card.append(element("p", tr("做到这样就可以"), "label"));
      card.append(list(marks, "ticks"));
    }
    const connections = connectionTable(plan);
    if (connections) card.append(connections);
    content.append(card);
  }
  $("prev-step").hidden = index === 0;
  $("prev-step").textContent = tr("← 上一步");
  $("next-step").textContent =
    index < 0
      ? tr("完成任务")
      : index === plan.steps.length - 1
        ? tr("完成这一步")
        : tr("下一步 →");
}
function renderOutline(task) {
  const root = $("plan-outline");
  root.replaceChildren();
  const plan = task?.plan;
  if (!plan) return;
  const ol = element("ol", undefined, "outline");
  plan.steps.forEach((s) => {
    const li = element("li");
    const done = task.state.steps[s.id] === "user_reported_done";
    li.append(element("strong", `${s.title}${done ? tr(" · 已完成") : ""}`));
    li.append(element("p", s.instruction, "muted"));
    s.source_ids.forEach((id) =>
      li.append(linkButton(tr("来源"), () => viewSource(id), "chip tiny")),
    );
    if (task.state.plan_confirmed && done && task.status === "active")
      li.append(
        linkButton(
          tr("回到这一步"),
          () => mutate("step", s.id, "needs_recheck"),
          "chip tiny",
        ),
      );
    ol.append(li);
  });
  root.append(ol);
  if (plan.prerequisites.length)
    root.append(
      element("p", tr("准备：{0}", [plan.prerequisites.join("；")]), "muted"),
    );
  if (plan.cautions.length)
    root.append(element("p", plan.cautions.join("；"), "warning"));
  task.sources.forEach((s) =>
    root.append(linkButton(s.title, () => viewSource(s.id), "chip tiny")),
  );
}
const TRANSITIONS = {
  proposed: [
    ["accepted", "采用"],
    ["rejected", "不采用"],
  ],
  accepted: [
    ["executed", "已照做"],
    ["rejected", "取消"],
  ],
  executed: [
    ["observed_success", "有效"],
    ["observed_failure", "没用"],
    ["needs_recheck", "说不准"],
  ],
  needs_recheck: [
    ["observed_success", "有效"],
    ["observed_failure", "没用"],
  ],
};
function renderFollowups(task) {
  const root = $("followups");
  root.replaceChildren();
  if (!task) return 0;
  const issues =
    task.state.open_issues?.filter((i) => i.status === "open") || [];
  issues.forEach((issue) => {
    const card = element("div", undefined, "followup");
    card.append(
      element("p", issue.text),
      linkButton(
        tr("已解决"),
        () => mutate("issue", issue.id, "resolved_by_user"),
        "chip tiny",
      ),
    );
    root.append(card);
  });
  const suggestions = task.suggestions.filter((s) => TRANSITIONS[s.status]);
  suggestions.forEach((s) => {
    const card = element("div", undefined, "followup");
    card.append(element("p", s.text));
    TRANSITIONS[s.status].forEach(([value, label]) =>
      card.append(
        linkButton(
          tr(label),
          () => mutate("suggestion", s.id, value),
          "chip tiny",
        ),
      ),
    );
    root.append(card);
  });
  const count = issues.length + suggestions.length;
  if (!count) root.append(element("p", tr("暂时没有。"), "muted"));
  return count;
}
function renderTask(task) {
  const key = JSON.stringify(task);
  if (key === lastTaskRender) return;
  lastTaskRender = key;
  renderOutline(task);
  const pendingFollowups = renderFollowups(task);
  $("open-menu").dataset.count = pendingFollowups || "";
  if (!task) return;
  const questions = task.state.pending_questions || [];
  if (task.state.plan_confirmed && task.plan) renderRun(task);
  else renderPlanCard(task, questions);
  $("finish-task").hidden = task.status === "ended";
}
async function stepForward() {
  if (!current?.plan || current.status !== "active") return;
  const index = currentIndex(current);
  if (index < 0) return finishTask();
  hideAnswer();
  await mutate("step", current.plan.steps[index].id, "user_reported_done");
}
async function stepBack() {
  if (!current?.plan || current.status !== "active") return;
  const index = currentIndex(current);
  const target = (index < 0 ? current.plan.steps.length : index) - 1;
  if (target < 0) return;
  hideAnswer();
  await mutate("step", current.plan.steps[target].id, "needs_recheck");
}
async function finishTask() {
  if (!confirm(tr("结束这次任务并停止记录？"))) return;
  closeMenu();
  await mutate("finish");
  await stopCamera();
  hideAnswer();
  $("messages").replaceChildren();
  $("question").value = "";
  conversation = sessionStorage["along-task-conversation"] =
    crypto.randomUUID();
  renderScreen();
  toast(tr("这次任务结束了"));
}
async function refresh() {
  const request = ++refreshNumber;
  try {
    const next = await json("/api/state");
    if (request !== refreshNumber) return;
    state = next;
    const previous = current;
    current = state.task;
    if (
      mode === "task" &&
      previous?.status === "active" &&
      current?.status !== "active"
    )
      await stopCamera();
    ready = true;
    renderTask(current);
    syncCapture();
    $("connection").textContent = state.busy ? tr("处理中") : "";
    $("connection").dataset.state = state.busy ? "busy" : "ok";
    $("diagnostics").textContent = tr(
      "模型：{0}；媒体 {1} MB / {2} MB；语义检索{3}；事件 {4}{5}",
      [
        state.model,
        (state.media.bytes / 1048576).toFixed(1),
        config.max_storage_mb,
        state.semantic_enabled ? tr("启用") : tr("关闭"),
        state.jobs.map((j) => `${j.status}:${j.count}`).join(" / ") ||
          tr("暂无"),
        state.worker_error ? "；" + state.worker_error : "",
      ],
    );
    const select = $("history-task"),
      selected = select.value;
    select.replaceChildren(new Option(tr("选择任务"), ""));
    state.tasks.forEach((t) =>
      select.add(new Option(`${t.goal} · ${tr(t.status)}`, t.id)),
    );
    select.value = state.tasks.some((t) => t.id === selected)
      ? selected
      : current?.id || "";
    await showQueue();
  } catch (e) {
    if (request !== refreshNumber) return;
    $("connection").textContent = tr("连接中断，记录暂存本机");
    $("connection").dataset.state = "down";
    queueError = errorText(e);
    updateControls();
  }
}
async function prepareTask() {
  if (!current) throw new Error(tr("没有当前任务"));
  if (busy) return;
  closeMenu();
  setBusy(true, tr("正在准备方案…"));
  try {
    await json(`/api/tasks/${current.id}/prepare`, {
      method: "POST",
      body: {
        url: $("source-url").value || null,
        reason: $("revision-reason").value,
      },
    });
  } finally {
    await refresh();
    setBusy(false);
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
        tr("暂无已整理事件；原始图片可能已保存，请查看下方记录。"),
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
      d.append(
        element("p", tr("未知：{0}", [e.data.unknowns.join("；")]), "warning"),
      );
    for (const f of [e.frame_ids[0], e.frame_ids.at(-1)].filter(Boolean))
      d.append(linkButton(tr("查看原图"), () => viewMedia(f)));
    root.append(d);
  }
  const raw = element("details");
  raw.append(
    element("summary", tr("最近原始媒体（{0} 条）", [data.media.length])),
  );
  data.media.slice(0, 40).forEach((m) => {
    const b = linkButton(
      `${new Date(m.captured * 1000).toLocaleTimeString()} ${m.kind}${m.expired ? tr(" · 已过期") : ""}`,
      () => viewMedia(m.id, m.kind),
    );
    b.disabled = !!m.expired;
    raw.append(b);
  });
  root.append(raw);
  const log = element("details");
  log.append(element("summary", tr("决策记录与计划修订")));
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
      element("summary", tr("查看该任务保存的操作表")),
      element("pre", JSON.stringify(oldTask.plan, null, 2)),
    );
    root.append(p);
  }
  root.append(linkButton(tr("查看该任务对话"), () => refreshMessages(id)));
}
function autosize() {
  const q = $("question");
  q.style.height = "auto";
  q.style.height = `${Math.min(q.scrollHeight, 160)}px`;
}
function openMenu() {
  $("drawer").hidden = false;
  renderScreen();
}
function closeMenu() {
  $("drawer").hidden = true;
  renderScreen();
}
bind("enter-task", "click", () => enterMode("task"));
bind("back-home", "click", goHome);
bind("camera-toggle", "click", openCamera);
bind("open-menu", "click", openMenu);
bind("close-menu", "click", closeMenu);
bind("drawer", "click", (e) => {
  if (e.target === $("drawer")) closeMenu();
});
bind("next-step", "click", stepForward);
bind("prev-step", "click", stepBack);
bind("chat-form", "submit", submitQuestion);
bind("question", "input", autosize);
bind("question", "keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    return submitQuestion();
  }
});
document.querySelectorAll("[data-example]").forEach((b) =>
  b.addEventListener("click", () => {
    $("question").value = b.dataset.example;
    autosize();
    $("question").focus();
  }),
);
async function beginTask(action) {
  await openCamera();
  if (!stream) {
    toast(tr("需要先开启相机才能开始"));
    return;
  }
  try {
    // The plan card already shows the recording notice; clicking “开始” is the consent.
    await mutate(
      action,
      "",
      action === "confirm_plan" ? tr("已核对适用条件") : "",
    );
  } catch (e) {
    await stopCamera();
    throw e;
  }
}
bind("confirm-plan", "click", () => beginTask("confirm_plan"));
bind("resume-task", "click", () => beginTask("resume"));
bind("pause-task", "click", async () => {
  if (!current?.state.plan_confirmed) return;
  closeMenu();
  if (current.status === "paused") await beginTask("resume");
  else if (current.status === "active") await mutate("pause");
});
bind("finish-task", "click", finishTask);
bind("retry-plan", "click", prepareTask);
bind("prepare-form", "submit", async (e) => {
  e.preventDefault();
  await prepareTask();
});
bind("source-file", "change", async (e) => {
  const f = e.target.files[0];
  if (!f || !current) return;
  if (f.size > 10 * 1024 * 1024) throw new Error(tr("说明书不得超过 10 MB"));
  const form = new FormData();
  form.append("file", f);
  await json(`/api/tasks/${current.id}/documents`, {
    method: "POST",
    body: form,
  });
  toast(tr("资料已保存，请重新生成计划"));
  await refresh();
});
bind("timeline-refresh", "click", timeline);
bind("delete-task", "click", async () => {
  const id = $("history-task").value;
  if (id && confirm(tr("永久删除所选任务、对话及本地原始媒体？"))) {
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
  toast(tr("已重新排队"));
});
bind("close-dialog", "click", () => {
  $("evidence-dialog").close();
});
$("evidence-dialog").addEventListener("close", () => {
  blobURLs.forEach((url) => URL.revokeObjectURL(url));
  blobURLs.clear();
});
function micLabel(text) {
  $("mic-label").textContent = text;
}
async function startVoice() {
  if (voiceRecorder?.state === "recording") return;
  if (busy || voiceBusy || cameraPending) return;
  voiceBusy = true;
  updateControls();
  try {
    voiceStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    voiceRecorder = new MediaRecorder(voiceStream);
    voiceRecorder.ondataavailable = (e) => {
      if (e.data.size) chunks.push(e.data);
    };
    voiceRecorder.onstop = async () => {
      clearTimeout(voiceTimer);
      voiceStream.getTracks().forEach((t) => t.stop());
      $("mic").classList.remove("recording");
      micLabel(tr("正在听写…"));
      $("mic").disabled = true;
      let text = "";
      let speechFailed = false;
      try {
        const form = new FormData();
        form.append(
          "file",
          new Blob(chunks, { type: voiceRecorder.mimeType }),
          voiceRecorder.mimeType.includes("mp4")
            ? "voice.m4a"
            : voiceRecorder.mimeType.includes("ogg")
              ? "voice.ogg"
              : "voice.webm",
        );
        text = (await json("/api/speech", { method: "POST", body: form })).text;
      } catch (e) {
        speechFailed = true;
        toast(errorText(e));
      } finally {
        voiceBusy = false;
        micLabel(tr("按住说话"));
        updateControls();
      }
      if (text?.trim()) {
        $("question").value = text;
        autosize();
        await submitQuestion();
      } else if (!text && !speechFailed) toast(tr("没听清，再说一次"));
    };
    voiceRecorder.start();
    $("mic").classList.add("recording");
    micLabel(tr("松开发送"));
    updateControls();
    voiceTimer = setTimeout(() => {
      if (voiceRecorder.state === "recording") voiceRecorder.stop();
    }, 60000);
    if (voiceHold.release) stopVoice();
  } catch (e) {
    voiceStream?.getTracks().forEach((t) => t.stop());
    voiceBusy = false;
    $("mic").classList.remove("recording");
    micLabel(tr("按住说话"));
    updateControls();
    throw e;
  }
}
function stopVoice() {
  if (voiceRecorder?.state === "recording") voiceRecorder.stop();
}
// Hold to talk; a quick tap toggles recording so the button also works without holding.
function pressVoice() {
  if (voiceRecorder?.state === "recording") {
    voiceHold.toggle = false;
    stopVoice();
    return;
  }
  voiceHold = { pressed: Date.now(), release: false, toggle: false };
  startVoice().catch((e) => toast(errorText(e)));
}
function releaseVoice() {
  if (!voiceHold.pressed) return;
  const held = Date.now() - voiceHold.pressed;
  voiceHold.pressed = 0;
  if (held < 350) {
    voiceHold.toggle = true;
    return;
  }
  if (voiceRecorder?.state === "recording") stopVoice();
  else voiceHold.release = true;
}
$("mic").addEventListener("pointerdown", (e) => {
  if ($("mic").disabled) return;
  e.preventDefault();
  pressVoice();
});
$("mic").addEventListener("pointerup", releaseVoice);
$("mic").addEventListener("pointerleave", () => {
  if (voiceHold.pressed && Date.now() - voiceHold.pressed >= 350)
    releaseVoice();
});
$("mic").addEventListener("click", (e) => e.preventDefault());
function typing(e) {
  return ["TEXTAREA", "INPUT", "SELECT"].includes(e.target.tagName);
}
document.addEventListener("keydown", (e) => {
  if (typing(e) || e.repeat || $("evidence-dialog").open) return;
  if (!$("drawer").hidden) {
    if (e.key === "Escape") closeMenu();
    return;
  }
  if (e.code === "Space" && mode !== "home" && !$("mic").disabled) {
    e.preventDefault();
    pressVoice();
  }
  const running = mode === "task" && current?.state.plan_confirmed;
  if (!running || busy) return;
  if (e.key === "ArrowRight")
    stepForward().catch((err) => toast(errorText(err)));
  if (e.key === "ArrowLeft") stepBack().catch((err) => toast(errorText(err)));
  if (e.key === "Escape") hideAnswer();
});
document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && !typing(e) && voiceHold.pressed) {
    e.preventDefault();
    releaseVoice();
  }
});
window.addEventListener("beforeunload", () => {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  if (voiceStream) voiceStream.getTracks().forEach((t) => t.stop());
});
window.addEventListener("online", () => flushQueue());
async function initialize() {
  try {
    config = await (await fetch("/bootstrap")).json();
    token = config.token;
    queueDB = await openQueue();
    await refresh();
    await flushQueue();
    setInterval(() => {
      if (!busy) refresh();
    }, 3000);
    setInterval(flushQueue, 5000);
  } catch (e) {
    toast(tr("连接失败，正在重试：{0}", [errorText(e)]));
    setTimeout(initialize, 3000);
  }
}
bind("language-toggle", "click", async () => {
  const rows = Array.from(
    $("messages").children,
    (node) => node.alongMessage,
  ).filter(Boolean);
  const answer = lastAnswer;
  window.AlongI18n.setLanguage(
    window.AlongI18n.language === "zh" ? "en" : "zh",
  );
  micLabel(tr("按住说话"));
  lastTaskRender = "";
  renderTask(current);
  renderScreen();
  syncCapture();
  $("messages").replaceChildren();
  rows.forEach((row) => message(row.role, row.text, row.data));
  if (answer) showAnswer(answer);
  if (!$("timeline").children.length) await showQueue();
  else await timeline();
  await refresh();
});
renderScreen();
initialize();
