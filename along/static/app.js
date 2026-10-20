"use strict";
const $ = (id) => document.getElementById(id);
let mode = "home";
let conversation = "";
let photoCaptured = null;
let photoURL = null;
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
  stream = null,
  pickedImage = null;
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
  if (value) refreshNumber++;
  updateControls();
  $("activity").textContent = value ? message : "";
}
function updateControls() {
  const locked = busy || voiceBusy || cameraPending;
  const preparing = mode === "task" && !!current?.state.preparing;
  $("back-home").disabled = locked;
  $("enter-task").disabled = !ready || locked;
  $("enter-instant").disabled = !ready || locked;
  $("question").disabled =
    busy || preparing || (mode === "instant" && !pickedImage);
  $("send").disabled =
    locked || preparing || (mode === "instant" && !pickedImage);
  $("mic").disabled =
    busy ||
    preparing ||
    cameraPending ||
    (voiceBusy && voiceRecorder?.state !== "recording") ||
    (mode === "instant" && !pickedImage);
  $("take-photo").disabled = locked || !stream;
  $("retake-photo").disabled = locked;
  $("camera-toggle").disabled = locked;
  for (const button of document.querySelectorAll(
    "#task-panel button, #task-more button, #task-more input, #task-more select",
  )) {
    button.disabled = locked || !!current?.state.preparing;
  }
}
function renderScreen() {
  const taskMode = mode === "task";
  const executing = taskMode && current?.state.plan_confirmed;
  $("home").hidden = mode !== "home";
  $("session").hidden = mode === "home";
  $("mode-title").textContent = taskMode ? "任务模式" : "识别模式";
  $("stage").textContent = taskMode
    ? executing
      ? "一起操作"
      : current
        ? current.state.pending_questions?.length
          ? "补充条件"
          : "确认方案"
        : "说目标"
    : "";
  $("camera-panel").hidden = !(mode === "instant" || executing);
  $("workspace").classList.toggle(
    "single-column",
    !(mode === "instant" || executing),
  );
  $("task-panel").hidden = !taskMode || !current;
  $("task-more").hidden = !taskMode || !current;
  $("privacy-note").hidden = mode !== "instant";
  $("camera-title").textContent = taskMode ? "操作现场" : "拍下你想问的东西";
  $("take-photo").hidden = mode !== "instant" || !!pickedImage;
  $("retake-photo").hidden = mode !== "instant" || !pickedImage;
  $("camera-toggle").hidden = !!stream || !!pickedImage;
  $("camera-hint").textContent = taskMode
    ? current?.status === "active"
      ? stream
        ? "正在记录过程，遇到问题随时问我。"
        : "相机未开启，当前没有记录画面；请重试开启相机。"
      : "任务已暂停，不记录画面。"
    : pickedImage
      ? "将用这张照片回答；可以继续追问，也可以重拍。"
      : "先拍照，再输入问题或语音提问。";
  $("chat-heading").textContent = !taskMode
    ? "有什么想问的？"
    : !current
      ? "我们一起做什么？"
      : executing
        ? "遇到问题，随时问我"
        : "需要调整什么？";
  $("chat-hint").textContent = !taskMode
    ? ""
    : !current
      ? "告诉我目标，我会与你确认方案，必要时再查资料。"
      : executing
        ? "结合当前画面与之前的过程，帮你判断下一步。"
        : "核对准备条件；有不适用的地方，直接告诉我。";
  $("question").placeholder = !taskMode
    ? "针对这张照片提问…"
    : !current
      ? "例如：带我做一份曲奇饼干"
      : executing
        ? "这一步遇到了什么问题？"
        : "例如：我没有烤箱，需要调整方案…";
  if (taskMode && current?.state.pending_questions?.length) {
    $("chat-heading").textContent = "补充一下情况";
    $("chat-hint").textContent = "先确认关键条件，再决定是否需要查资料。";
    $("question").placeholder = "回答上面的待确认问题…";
  }
  $("messages").hidden = !$("messages").children.length;
  updateControls();
}
function clearPhoto() {
  pickedImage = null;
  photoCaptured = null;
  if (photoURL) URL.revokeObjectURL(photoURL);
  photoURL = null;
  $("uploaded-preview").removeAttribute("src");
  $("uploaded-preview").hidden = true;
}
async function enterMode(next) {
  if (!ready || busy || voiceBusy || cameraPending || mode !== "home") return;
  mode = next;
  conversation = conversationFor(next);
  clearPhoto();
  $("question").value = "";
  $("messages").replaceChildren();
  if (next === "instant") {
    // New visits must never reuse an unseen photo from an earlier session.
    conversation = sessionStorage["along-instant-conversation"] =
      crypto.randomUUID();
  }
  renderScreen();
  if (next === "instant" || current?.status === "active") await openCamera();
  if (next === "task") {
    setBusy(true, "正在恢复对话…");
    try {
      await refreshMessages(current?.id || null);
    } finally {
      setBusy(false);
    }
  }
}
async function goHome() {
  if (busy || voiceBusy || cameraPending) return;
  if (mode === "task" && current?.status === "active") {
    if (!confirm("返回首页会暂停任务并停止记录，是否继续？")) return;
    await mutate("pause");
  }
  await stopCamera();
  clearPhoto();
  mode = "home";
  $("task-more").open = false;
  renderScreen();
}
async function openCamera() {
  if (stream || cameraPending) return;
  cameraPending = true;
  $("camera-placeholder").textContent = "请允许浏览器使用相机…";
  $("camera-placeholder").hidden = false;
  updateControls();
  try {
    await startCamera();
  } catch (e) {
    $("camera-placeholder").textContent =
      "无法打开相机，请检查浏览器权限或设备连接，再点击重试。";
    toast(errorText(e));
  } finally {
    cameraPending = false;
    renderScreen();
  }
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
  if (mode === "task") {
    for (const [key, label] of [
      ["missing", "还需确认"],
      ["checks", "继续前检查"],
    ]) {
      if (data[key]?.length) {
        box.append(element("strong", label));
        const ul = element("ul");
        data[key].forEach((t) => ul.append(element("li", t)));
        box.append(ul);
      }
    }
    if (data.evidence_ids?.length || data.source_ids?.length) {
      const d = element("details");
      d.append(element("summary", "查看依据"));
      data.evidence_ids?.forEach((id, i) =>
        d.append(linkButton(`过程图片 ${i + 1}`, () => viewMedia(id))),
      );
      data.source_ids?.forEach((id, i) =>
        d.append(linkButton(`参考资料 ${i + 1}`, () => viewSource(id))),
      );
      box.append(d);
    }
  }
  if (data.warnings?.length)
    box.append(element("p", data.warnings.join("；"), "warning"));
  $("messages").append(box);
  $("messages").hidden = false;
  $("messages").scrollTop = $("messages").scrollHeight;
}
async function refreshMessages(taskId = null) {
  const rows = await json(
    `/api/messages/${conversation}${taskId ? `?task_id=${taskId}` : ""}`,
  );
  $("messages").replaceChildren();
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
  form.append("origin", "camera");
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
  if (
    mode !== "task" ||
    uploading ||
    !stream ||
    pickedImage ||
    current?.status !== "active"
  )
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
  $("camera").srcObject = stream;
  $("camera-placeholder").hidden = true;
  $("camera-toggle").textContent = "关闭相机";
  clearInterval(captureTimer);
  captureTimer = setInterval(tickCapture, (config.frame_interval || 1) * 1000);
  syncCapture();
  stream.getVideoTracks()[0].onended = () => {
    stopCamera();
    toast("摄像头已断开，画面记录已停止");
  };
}
async function stopCamera() {
  clearInterval(captureTimer);
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  $("camera").srcObject = null;
  $("camera-toggle").textContent = "开启相机";
  $("camera-placeholder").hidden = !!pickedImage;
  $("capture-status").textContent = "相机未开启";
  renderScreen();
}
function syncCapture() {
  $("capture-status").textContent = pickedImage
    ? "已拍照"
    : stream
      ? mode === "task" && current?.status === "active"
        ? "● 正在记录"
        : "相机就绪"
      : "相机未开启";
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
  if (mode === "instant" && !pickedImage) {
    toast("请先拍照，再提问");
    return;
  }
  setBusy(
    true,
    mode === "task" && !current ? "正在理解目标并准备方案…" : "正在思考…",
  );
  message("user", text);
  try {
    if (mode === "task" && current && !current.state.plan_confirmed) {
      if (text.length > 2000) throw new Error("调整说明请控制在 2000 字以内");
      await json(`/api/tasks/${current.id}/change`, {
        method: "POST",
        body: { version: current.version, action: "note", value: text },
      });
      const prepared = await json(`/api/tasks/${current.id}/prepare`, {
        method: "POST",
        body: { reason: text },
      });
      message(
        "assistant",
        prepared.state.pending_questions?.length
          ? prepared.state.pending_questions.join("；")
          : "方案已更新，请重新核对后开始。",
      );
    } else {
      const blob =
        mode === "instant"
          ? pickedImage
          : current?.status === "active"
            ? await snapshot()
            : null;
      let frame = null;
      if (blob)
        frame = await uploadBlob(
          blob,
          mode === "task" ? current.id : null,
          "frame",
          crypto.randomUUID(),
          mode === "instant" ? photoCaptured : Date.now() / 1000,
        );
      const result = await json("/api/chat", {
        method: "POST",
        body: {
          request_id: crypto.randomUUID(),
          conversation_id: conversation,
          text:
            mode === "task" && !current
              ? `请带我一起完成这个任务：${text}`
              : text,
          mode,
          task_id: mode === "task" ? current?.id || null : null,
          frame_id: frame?.id || null,
        },
      });
      message("assistant", result.reply, result);
    }
    $("question").value = "";
  } catch (e) {
    message("error", errorText(e));
  } finally {
    await refresh();
    setBusy(false);
  }
}
async function mutate(action, target = "", value = "") {
  if (!current) throw new Error("没有当前任务");
  if (busy) return;
  setBusy(true, "正在保存…");
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
function renderTask(task) {
  const key = JSON.stringify(task);
  if (key === lastTaskRender) return;
  lastTaskRender = key;
  const content = $("task-content");
  content.replaceChildren();
  if (!task) return;
  const confirmed = task.state.plan_confirmed;
  const names = {
    draft: "待确认",
    active: "进行中",
    paused: "已暂停",
    ended: "已结束",
  };
  $("task-status").textContent = names[task.status];
  $("task-heading").textContent = confirmed ? "当前步骤" : "确认方案";
  content.append(element("h3", task.goal));
  if (task.state.preparing)
    content.append(element("p", "正在判断资料需求并准备方案…", "muted"));
  if (task.state.error)
    content.append(element("p", task.state.error, "warning"));
  const questions = task.state.pending_questions || [];
  if (questions.length) {
    $("task-heading").textContent = "先补充几个条件";
    const list = element("ul");
    questions.forEach((q) => list.append(element("li", q)));
    content.append(
      list,
      element("p", "在下方回答即可，暂不生成操作步骤。", "muted"),
    );
  }
  const plan = questions.length ? null : task.plan;
  if (plan) {
    const basis =
      plan.basis ||
      (plan.steps.some((s) => s.source_ids.length)
        ? "references"
        : "general_knowledge");
    const labels = {
      general_knowledge: "通用知识建议 · 未经外部资料核验",
      references: "参考资料生成 · 不代表已独立核验",
      mixed: "部分步骤有资料依据，其余为通用建议",
    };
    content.append(element("p", labels[basis], "muted"));
    const overview = element(
      confirmed ? "details" : "div",
      undefined,
      "plan-overview",
    );
    if (confirmed) overview.append(element("summary", "查看完整计划与来源"));
    if (plan.prerequisites.length)
      overview.append(
        element("p", `需要准备：${plan.prerequisites.join("；")}`),
      );
    if (plan.questions.length)
      overview.append(
        element("p", `开始前核对：${plan.questions.join("；")}`, "warning"),
      );
    if (plan.cautions.length)
      overview.append(element("p", plan.cautions.join("；"), "warning"));
    const list = element("ol");
    plan.steps.forEach((s) => {
      const item = element("li");
      const d = element("details");
      const done = task.state.steps[s.id] === "user_reported_done";
      d.append(
        element("summary", `${s.title}${done ? " · 已完成" : ""}`),
        element("p", s.instruction),
      );
      s.source_ids.forEach((id) =>
        d.append(linkButton("查看来源", () => viewSource(id))),
      );
      if (confirmed && done)
        d.append(
          linkButton("重新检查这一步", () =>
            mutate("step", s.id, "needs_recheck"),
          ),
        );
      item.append(d);
      list.append(item);
    });
    overview.append(list);
    const sources = element("details");
    sources.append(element("summary", "参考资料"));
    task.sources.forEach((s) =>
      sources.append(linkButton(s.title, () => viewSource(s.id))),
    );
    if (task.sources.length) overview.append(sources);
    if (confirmed) {
      const index = plan.steps.findIndex(
        (s) => task.state.steps[s.id] !== "user_reported_done",
      );
      if (index >= 0) {
        const step = plan.steps[index];
        const card = element("div", undefined, "current-step");
        card.append(
          element("small", `第 ${index + 1} / ${plan.steps.length} 步`),
          element("h3", step.title),
          element("p", step.instruction),
        );
        if (step.checks.length) {
          card.append(element("strong", "继续前，检查一下"));
          const ul = element("ul");
          step.checks.forEach((c) => ul.append(element("li", c)));
          card.append(ul);
        }
        if (task.status === "active")
          card.append(
            linkButton(
              index === plan.steps.length - 1
                ? "已检查，完成这一步"
                : "已检查，下一步",
              async () => {
                if (
                  confirm(
                    "确认已完成这一步并核对检查项？这会记录为你的确认，不代表 AI 已验证。",
                  )
                )
                  await mutate("step", step.id, "user_reported_done");
              },
            ),
          );
        else
          card.append(element("p", "任务已暂停，继续后再推进步骤。", "muted"));
        content.append(card);
      } else {
        content.append(element("p", "所有步骤都已由你确认完成。"));
        content.append(linkButton("完成任务", finishTask));
      }
    }
    content.append(overview);
  }
  const issues =
    task.state.open_issues?.filter((i) => i.status === "open") || [];
  if (issues.length) {
    const d = element("details");
    d.append(element("summary", `还有 ${issues.length} 项待确认`));
    issues.forEach((issue) => {
      d.append(
        element("p", issue.text),
        linkButton("我已确认解决", () =>
          mutate("issue", issue.id, "resolved_by_user"),
        ),
      );
    });
    content.append(d);
  }
  const transitions = {
    proposed: [
      ["accepted", "采用建议"],
      ["rejected", "不采用"],
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
    needs_recheck: [
      ["observed_success", "结果符合预期"],
      ["observed_failure", "结果不符合"],
    ],
  };
  const suggestions = task.suggestions.filter((s) => transitions[s.status]);
  if (suggestions.length) {
    const d = element("details");
    d.append(element("summary", `建议跟进（${suggestions.length}）`));
    suggestions.forEach((s) => {
      const card = element("div", undefined, "suggestion");
      card.append(element("p", s.text));
      transitions[s.status].forEach(([value, label]) =>
        card.append(linkButton(label, () => mutate("suggestion", s.id, value))),
      );
      d.append(card);
    });
    content.append(d);
  }
  $("confirm-plan").hidden = !plan || confirmed || task.state.preparing;
  $("record-consent").hidden = !plan || confirmed;
  $("resume-task").hidden = task.status !== "paused" || !confirmed;
  $("retry-plan").hidden = !!plan || task.state.preparing || !!questions.length;
  $("pause-task").hidden = task.status !== "active";
  $("finish-task").hidden = task.status === "ended";
}
async function finishTask() {
  if (confirm("结束本次任务并停止记录？")) {
    await mutate("finish");
    await stopCamera();
    $("messages").replaceChildren();
    $("question").value = "";
    conversation = sessionStorage["along-task-conversation"] =
      crypto.randomUUID();
    renderScreen();
    toast("本次任务已结束");
  }
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
    if (request !== refreshNumber) return;
    $("connection").textContent = "连接中断 · 记录暂存本机";
    queueError = errorText(e);
    updateControls();
  }
}
async function prepareTask() {
  if (!current) throw new Error("没有当前任务");
  if (busy) return;
  setBusy(true, "正在判断资料需求并准备方案…");
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

bind("enter-instant", "click", () => enterMode("instant"));
bind("enter-task", "click", () => enterMode("task"));
bind("back-home", "click", goHome);
bind("camera-toggle", "click", openCamera);
bind("take-photo", "click", async () => {
  if (busy || voiceBusy || cameraPending || mode !== "instant") return;
  setBusy(true, "正在拍照…");
  try {
    const blob = await snapshot();
    if (!blob) {
      toast("相机画面尚未就绪，请稍后拍照");
      return;
    }
    photoCaptured = Date.now() / 1000;
    pickedImage = blob;
    photoURL = URL.createObjectURL(blob);
    $("uploaded-preview").src = photoURL;
    $("uploaded-preview").hidden = false;
    await stopCamera();
    syncCapture();
  } finally {
    setBusy(false);
    $("question").focus();
  }
});
bind("retake-photo", "click", async () => {
  clearPhoto();
  conversation = sessionStorage["along-instant-conversation"] =
    crypto.randomUUID();
  $("messages").replaceChildren();
  $("question").value = "";
  $("camera-placeholder").hidden = false;
  await openCamera();
});
bind("chat-form", "submit", submitQuestion);
bind("question", "keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    return submitQuestion();
  }
});
async function beginTask(action) {
  if (
    !confirm(
      action === "confirm_plan"
        ? "已核对准备条件，同意开始任务并持续记录画面？"
        : "继续任务并恢复画面记录？",
    )
  )
    return;
  await openCamera();
  if (!stream) {
    toast("请先开启相机，再开始任务");
    return;
  }
  try {
    await mutate(action, "", action === "confirm_plan" ? "已核对适用条件" : "");
  } catch (e) {
    await stopCamera();
    throw e;
  }
}
bind("confirm-plan", "click", () => beginTask("confirm_plan"));
bind("resume-task", "click", () => beginTask("resume"));
bind("pause-task", "click", () => mutate("pause"));
bind("finish-task", "click", finishTask);
bind("retry-plan", "click", prepareTask);
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
  if (busy || voiceBusy || cameraPending) return;
  voiceBusy = true;
  updateControls();
  try {
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
        const result = await json("/api/speech", {
          method: "POST",
          body: form,
        });
        $("question").value = result.text;
        toast("语音已识别，请确认后发送");
      } catch (e) {
        toast(errorText(e));
      } finally {
        voiceBusy = false;
        $("mic").textContent = "语音提问";
        updateControls();
      }
    };
    voiceRecorder.start();
    updateControls();
    voiceTimer = setTimeout(() => {
      if (voiceRecorder.state === "recording") voiceRecorder.stop();
    }, 60000);
  } catch (e) {
    voiceStream?.getTracks().forEach((t) => t.stop());
    voiceBusy = false;
    $("mic").textContent = "语音提问";
    updateControls();
    throw e;
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
    toast(`连接失败，正在重试：${errorText(e)}`);
    setTimeout(initialize, 3000);
  }
}
initialize();
