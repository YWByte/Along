// Run against a local ALONG server: NODE_PATH=<browser node_modules> node --test tests/ui.test.cjs
// API responses and camera/microphone are simulated; no user task data is changed.
const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { chromium } = require("playwright");
let browser;
before(async () => {
  browser = await chromium.launch({
    channel: "chrome",
    headless: true,
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
    ],
  });
});
after(async () => {
  await browser?.close();
});
const base = process.env.ALONG_TEST_URL || "http://127.0.0.1:8766";
function draft() {
  return {
    id: "task-test",
    goal: "整理工作台",
    version: 1,
    status: "draft",
    state: {
      plan_confirmed: false,
      steps: { s1: "unknown", s2: "unknown" },
      open_issues: [],
    },
    sources: [],
    suggestions: [],
    plan: {
      prerequisites: ["准备两个收纳盒"],
      questions: ["确认物品可以移动"],
      cautions: ["不要触碰通电设备"],
      steps: [
        {
          id: "s1",
          title: "分类物品",
          instruction: "把物品分为两类",
          checks: ["确认没有遗漏"],
          source_ids: [],
        },
        {
          id: "s2",
          title: "放入收纳盒",
          instruction: "把两类物品分别放好",
          checks: ["确认放置稳定"],
          source_ids: [],
        },
      ],
    },
  };
}
async function setup(t, initial = null, options = {}) {
  const context = await browser.newContext({
    permissions: ["camera", "microphone"],
    locale: options.locale || "zh-CN",
  });
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  t.after(() => assert.deepEqual(errors, []));
  const model = {
    task: initial,
    requests: [],
    media: [],
    failChat: false,
    failChange: false,
    failPrepare: false,
  };
  await page.route("**/bootstrap", (route) =>
    route.fulfill({
      json: {
        token: "test",
        frame_interval: options.frameInterval ?? 0.15,
        max_storage_mb: 100,
      },
    }),
  );
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    const data = req.headers()["content-type"]?.includes("application/json")
      ? req.postDataJSON()
      : null;
    model.requests.push({
      path,
      data,
      language: req.headers()["x-along-language"],
    });
    if (path === "/api/state")
      return route.fulfill({
        json: {
          task: model.task,
          tasks: model.task ? [model.task] : [],
          busy: false,
          jobs: [],
          media: { bytes: 0 },
          model: "test",
          semantic_enabled: true,
        },
      });
    if (path.startsWith("/api/messages/")) return route.fulfill({ json: [] });
    if (path.startsWith("/api/media/"))
      return route.fulfill({
        contentType: "image/jpeg",
        body: Buffer.alloc(0),
      });
    if (path === "/api/media") {
      model.media.push(req.postData());
      return route.fulfill({ json: { id: `frame-${model.media.length}` } });
    }
    if (path === "/api/chat") {
      if (options.chatDelay)
        await new Promise((r) => setTimeout(r, options.chatDelay));
      if (model.failChat)
        return route.fulfill({
          status: 503,
          json: { detail: "模型暂时不可用" },
        });
      if (data.mode === "task" && !model.task) model.task = draft();
      return route.fulfill({
        json: {
          reply:
            data.mode === "instant" ? "这是一个测试画面。" : "方案已准备好。",
          ...(options.chatResult || {}),
          checks: ["核对眼前状态"],
          evidence_ids: ["frame-1"],
        },
      });
    }
    if (path.endsWith("/change")) {
      if (model.failChange)
        return route.fulfill({
          status: 409,
          json: { detail: "任务已在其他页面更新" },
        });
      model.task.version++;
      if (data.action === "confirm_plan" || data.action === "resume") {
        model.task.status = "active";
        model.task.state.plan_confirmed = true;
      }
      if (data.action === "pause") model.task.status = "paused";
      if (data.action === "step")
        model.task.state.steps[data.target] = data.value;
      const result = structuredClone(model.task);
      if (data.action === "finish") {
        model.task = null;
        result.status = "ended";
      }
      return route.fulfill({ json: result });
    }
    if (path.endsWith("/prepare")) {
      if (model.failPrepare)
        return route.fulfill({
          status: 503,
          json: { detail: "搜索失败，请重试" },
        });
      model.task = draft();
      return route.fulfill({ json: model.task });
    }
    if (path === "/api/speech")
      return route.fulfill({ json: { text: "这个东西是什么" } });
    throw new Error(`Unexpected API: ${path}`);
  });
  if (options.blockStorage)
    await page.addInitScript(() => {
      Object.defineProperty(window, "localStorage", {
        get() {
          throw new DOMException("Storage blocked", "SecurityError");
        },
      });
    });
  if (options.denyCamera)
    await page.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = async () => {
        throw new DOMException("Permission denied", "NotAllowedError");
      };
    });
  page.on("dialog", (dialog) =>
    options.cancelDialogs ? dialog.dismiss() : dialog.accept(),
  );
  await page.goto(base);
  await page.waitForFunction(
    () => !document.getElementById("enter-task").disabled,
  );
  return { page, model };
}
async function ask(page, text) {
  await page.fill("#question", text);
  await page.click("#send");
  await page.waitForFunction(() => !document.getElementById("send").disabled);
}

test("通用入口只有我们开始吧；目标阶段预览相机，不持续记录", async (t) => {
  const { page, model } = await setup(t);
  assert.equal(await page.locator("#home button:visible").count(), 1);
  assert.equal(await page.locator("#language-toggle").isVisible(), true);
  assert.match(await page.locator("#enter-task").textContent(), /我们开始吧/);
  assert.equal(await page.locator("#enter-instant").count(), 0);
  await page.click("#enter-task");
  await page.waitForFunction(
    () => document.getElementById("camera").videoWidth > 0,
  );
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(model.media.length, 0);
  await ask(page, "我要完成一个任务");
  assert.equal(model.media.length, 1);
  const req = model.requests.find((r) => r.path === "/api/chat").data;
  assert.equal(req.mode, "task");
  assert.ok(req.frame_id);
});

test("任务从目标到方案、当前步骤、暂停恢复、完成；暂停时不持续采集", async (t) => {
  const { page, model } = await setup(t);
  await page.click("#enter-task");
  assert.equal(await page.locator("#camera").isVisible(), true);
  await ask(page, "整理工作台");
  assert.equal(await page.locator("#stage").textContent(), "确认方案");
  assert.ok(model.media.length <= 2);
  await page.click("#confirm-plan");
  await page.waitForFunction(
    () => document.getElementById("stage").textContent === "一起操作",
  );
  assert.equal(await page.locator(".current-step").count(), 1);
  assert.match(await page.locator(".current-step").textContent(), /分类物品/);
  await page.waitForFunction(
    () => document.getElementById("camera").videoWidth > 0,
  );
  await new Promise((r) => setTimeout(r, 500));
  assert.ok(model.media.length > 0);
  await page.click("#next-step");
  await page.waitForFunction(() =>
    document.querySelector(".current-step").textContent.includes("放入收纳盒"),
  );
  await page.click("#back-home");
  assert.equal(model.task.status, "paused");
  await page.waitForFunction(() => !uploading && !queueSending);
  await page.evaluate(() => flushQueue());
  const uploads = model.media.length;
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(model.media.length, uploads);
  await page.click("#enter-task");
  assert.equal(await page.locator("#messages article").count(), 0);
  await page.click("#resume-task");
  await page.waitForFunction(
    () => document.getElementById("paused-overlay").hidden,
  );
  await page.getByRole("button", { name: "完成这一步" }).click();
  await page.waitForFunction(
    () => document.getElementById("next-step").textContent === "完成任务",
  );
  await page.click("#next-step");
  await page.waitForFunction(
    () => document.getElementById("stage").textContent === "尚未开始任务",
  );
  assert.equal(model.task, null);
});

test("相机拒绝授权可以重试，仍可描述任务", async (t) => {
  const { page, model } = await setup(t, null, { denyCamera: true });
  await page.click("#enter-task");
  await page.waitForFunction(
    () => !document.getElementById("camera-toggle").disabled,
  );
  assert.match(
    await page.locator("#camera-placeholder").textContent(),
    /相机没有打开/,
  );
  assert.equal(await page.locator("#send").isDisabled(), false);
  await page.click("#camera-toggle");
  await page.waitForFunction(
    () => !document.getElementById("back-home").disabled,
  );
  await page.click("#back-home");
  assert.equal(model.media.length, 0);
});

test("模型失败保留问题，可重试；请求期间不允许离开", async (t) => {
  const { page, model } = await setup(t, null, { chatDelay: 150 });
  await page.click("#enter-task");
  model.failChat = true;
  await page.fill("#question", "这是什么？");
  await page.click("#send");
  assert.equal(await page.locator("#back-home").isDisabled(), true);
  await page.waitForFunction(() => !document.getElementById("send").disabled);
  assert.equal(await page.locator("#question").inputValue(), "这是什么？");
  assert.equal(await page.locator("#camera").isVisible(), true);
  model.failChat = false;
  await ask(page, "这是什么？");
  assert.match(await page.locator("#messages").textContent(), /方案已准备好/);
});

test("修改方案不推进步骤；生成失败允许重试；点击开始即开启记录", async (t) => {
  const { page, model } = await setup(t, draft());
  await page.click("#enter-task");
  model.failPrepare = true;
  await ask(page, "换成三个盒子");
  assert.match(await page.locator("#messages").textContent(), /搜索失败/);
  model.failPrepare = false;
  await ask(page, "换成三个盒子");
  assert.equal(model.task.state.plan_confirmed, false);
  assert.ok(model.media.length <= 2);
  await page.click("#confirm-plan");
  await page.waitForFunction(
    () => document.getElementById("stage").textContent === "一起操作",
  );
  assert.equal(model.task.status, "active");
});

test("按住说话松开即发送；轻点切换录音；空格键同样可用", async (t) => {
  const task = draft();
  task.state.plan_confirmed = true;
  task.status = "active";
  const { page, model } = await setup(t, task);
  await page.click("#enter-task");
  await page.waitForFunction(() => !document.getElementById("mic").disabled);
  const chats = () => model.requests.filter((r) => r.path === "/api/chat");
  const box = await page.locator("#mic").boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForFunction(() =>
    document.getElementById("mic").classList.contains("recording"),
  );
  assert.equal(await page.locator("#back-home").isDisabled(), true);
  await page.waitForTimeout(450);
  await page.mouse.up();
  await page.waitForFunction(
    () => document.querySelectorAll("#messages article").length === 2,
  );
  assert.equal(chats().length, 1);
  assert.equal(chats()[0].data.text, "这个东西是什么");
  await page.click("#mic");
  await page.waitForFunction(() =>
    document.getElementById("mic").classList.contains("recording"),
  );
  await page.waitForTimeout(200);
  await page.click("#mic");
  await page.waitForFunction(
    () => document.querySelectorAll("#messages article").length === 4,
  );
  assert.equal(chats().length, 2);
  await page.keyboard.down("Space");
  await page.waitForFunction(() =>
    document.getElementById("mic").classList.contains("recording"),
  );
  await page.waitForTimeout(450);
  await page.keyboard.up("Space");
  await page.waitForFunction(
    () => document.querySelectorAll("#messages article").length === 6,
  );
  assert.equal(chats().length, 3);
});

test("移动端无横向溢出；已有任务不会在首页自动采集", async (t) => {
  const task = draft();
  task.status = "active";
  task.state.plan_confirmed = true;
  const { page, model } = await setup(t, task);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(model.media.length, 0);
  await page.click("#enter-task");
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  assert.equal(model.media.length, 0);
  await page.click("#back-home");
  await page.click("#enter-task");
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
});

test("版本冲突不前进、不丢失当前步骤", async (t) => {
  const task = draft();
  task.status = "active";
  task.state.plan_confirmed = true;
  const { page, model } = await setup(t, task);
  await page.click("#enter-task");
  model.failChange = true;
  await page.click("#next-step");
  await page.waitForFunction(() =>
    document.getElementById("toast").textContent.includes("其他页面"),
  );
  assert.match(await page.locator(".current-step").textContent(), /分类物品/);
  assert.equal(model.task.state.steps.s1, "unknown");
});

test("待澄清时不展示旧方案或允许开始，回答条件后恢复确认方案", async (t) => {
  const task = draft();
  task.state.pending_questions = ["设备的型号是什么？"];
  const { page, model } = await setup(t, task);
  await page.click("#enter-task");
  await page.waitForFunction(() => !document.getElementById("send").disabled);
  assert.equal(await page.locator("#stage").textContent(), "补充条件");
  assert.equal(await page.locator("#confirm-plan").isVisible(), false);
  assert.equal(await page.locator(".plan-steps").count(), 0);
  assert.match(await page.locator("#plan-body").textContent(), /型号是什么/);
  await ask(page, "只整理桌面，不拆设备");
  assert.equal(await page.locator("#confirm-plan").isVisible(), true);
  assert.ok(model.media.length <= 1);
  assert.match(await page.locator("#plan-meta").textContent(), /通用知识/);
});

test("任务采集遵守每秒一帧，暂停后停止", async (t) => {
  const { page, model } = await setup(t, null, { frameInterval: 1 });
  await page.click("#enter-task");
  await ask(page, "整理工作台");
  await page.click("#confirm-plan");
  await page.waitForFunction(
    () => document.getElementById("camera").videoWidth > 0,
  );
  await page.waitForFunction(() => seq >= 4);
  const captured = model.media
    .map((raw) => {
      const match = raw.match(/name="captured"\r\n\r\n([0-9.]+)/);
      return match ? Number(match[1]) : null;
    })
    .filter((value) => value !== null);
  assert.ok(captured.length >= 3);
  for (let i = 1; i < captured.length; i++) {
    assert.ok(captured[i] - captured[i - 1] >= 0.8);
    assert.ok(captured[i] - captured[i - 1] <= 1.5);
  }
  await page.click("#back-home");
  await page.waitForFunction(() => !uploading && !queueSending);
  const uploads = model.media.length;
  await new Promise((resolve) => setTimeout(resolve, 1200));
  assert.equal(model.media.length, uploads);
});

test("场景与跨帧依据折叠展示，不增加首页入口", async (t) => {
  const task = draft();
  task.state.scenario = "installation";
  task.state.plan_confirmed = true;
  task.status = "paused";
  const { page } = await setup(t, task, {
    chatResult: {
      review_focus: { subject: "连接件方向" },
      retrieval: {
        visual_reviews: [
          {
            result: {
              summary: "连接件前后朝向不同，尚不能确认内部连接。",
              changes: [
                { claim: "连接件发生转向", frame_ids: ["frame-1", "frame-2"] },
              ],
            },
          },
        ],
      },
    },
  });
  await page.click("#enter-task");
  assert.equal(await page.getByText(/设备安装与使用/).count(), 0);
  await page.fill("#question", "刚才接好了吗");
  await page.click("#send");
  await page.locator("#answer .evidence-link").click();
  assert.equal(
    await page.getByText("本次核对：连接件方向", { exact: true }).count(),
    1,
  );
  assert.equal(
    await page.getByRole("button", { name: "对比帧 1", exact: true }).count(),
    1,
  );
});

test("侧边栏对话展示每次回答 AI 看过的图片与检索过程", async (t) => {
  const task = draft();
  task.status = "active";
  task.state.plan_confirmed = true;
  const frames = [1, 2, 3].map((i) => ({ id: `f${i}`, captured: 1000 + i }));
  const { page } = await setup(t, task, {
    chatResult: {
      headline: "做对了",
      reply: "手腕抬起再落下。",
      retrieval: {
        question_frames: [{ id: "q1", captured: 1000 }],
        tools: [
          { call: { tool: "browse", step_id: "s1" }, result: { events: [{}] } },
          {
            call: { tool: "open", step_id: "s1", sheet: true },
            result: { frames, layout: "sheet", sheet_id: "sheet_abc" },
          },
        ],
      },
    },
  });
  await page.route("**/api/sheets/**", (route) =>
    route.fulfill({ contentType: "image/jpeg", body: Buffer.alloc(0) }),
  );
  await page.click("#enter-task");
  await page.waitForFunction(
    () => document.getElementById("camera").videoWidth > 0,
  );
  await ask(page, "刚刚对了吗");
  await page.click("#open-menu");
  const panel = page.locator("#drawer-thread .ctx");
  assert.match(await panel.locator("summary").textContent(), /2 张 · 2 次检索/);
  await panel.locator("summary").click();
  await panel.locator(".ctx-step").nth(2).waitFor();
  assert.equal(await panel.locator(".ctx-step").count(), 3);
  assert.match(
    await panel.locator(".ctx-label").nth(2).textContent(),
    /回看 · s1 · .* · 拼图 3 帧/,
  );
  assert.equal(await panel.locator(".ctx-img.sheet").count(), 1);
  await page.screenshot({ path: "/tmp/along-build/ui/context.png" });
});

test("执行页先显示一句结论，可收起；方向键推进步骤，暂停时不推进", async (t) => {
  const task = draft();
  task.status = "active";
  task.state.plan_confirmed = true;
  const { page, model } = await setup(t, task, {
    chatResult: { headline: "看得到在分类", reply: "左右两堆已经分开。" },
  });
  await page.click("#enter-task");
  await page.waitForFunction(
    () => document.getElementById("camera").videoWidth > 0,
  );
  assert.equal(await page.locator("#messages").isVisible(), false);
  await ask(page, "这样分对吗");
  assert.equal(
    await page.locator("#answer .headline").textContent(),
    "看得到在分类",
  );
  assert.match(await page.locator("#answer .detail").textContent(), /两堆/);
  await page.click("#answer .close");
  assert.equal(await page.locator("#answer").isVisible(), false);
  await page.locator("body").click({ position: { x: 5, y: 300 } });
  await page.keyboard.press("ArrowRight");
  await page.waitForFunction(() =>
    document.querySelector(".current-step").textContent.includes("放入收纳盒"),
  );
  assert.equal(model.task.state.steps.s1, "user_reported_done");
  await page.keyboard.press("ArrowLeft");
  await page.waitForFunction(() =>
    document.querySelector(".current-step").textContent.includes("分类物品"),
  );
  assert.equal(model.task.state.steps.s1, "needs_recheck");
  await page.click("#pause-task");
  await page.waitForFunction(
    () => !document.getElementById("paused-overlay").hidden,
  );
  await page.keyboard.press("ArrowRight");
  assert.equal(model.task.state.steps.s1, "needs_recheck");
});

test("过程回答显示判断标签与逐条标准；步骤显示可见标准", async (t) => {
  const task = draft();
  task.status = "active";
  task.state.plan_confirmed = true;
  task.plan.steps[0].look_for = ["左右两堆分开摆放"];
  const { page } = await setup(t, task, {
    chatResult: {
      headline: "差一点：右边混进了一件",
      reply: "左边已分好。",
      verdict: "adjust",
      suggestion: "把右边那件移到左边",
      criteria: [
        { criterion: "左右两堆分开摆放", status: "met", frame_ids: [] },
        { criterion: "同类物品在同一堆", status: "not_met", frame_ids: [] },
        { criterion: "标签朝上", status: "unseen", frame_ids: [] },
      ],
    },
  });
  await page.click("#enter-task");
  await page.waitForFunction(
    () => document.getElementById("camera").videoWidth > 0,
  );
  assert.match(
    await page.locator(".current-step").textContent(),
    /左右两堆分开摆放/,
  );
  await ask(page, "这样对吗");
  assert.equal(
    await page.locator("#answer .verdict").textContent(),
    "调整一下",
  );
  assert.ok(
    await page
      .locator("#answer")
      .evaluate((n) => n.classList.contains("verdict-adjust")),
  );
  assert.equal(await page.locator("#answer .criteria li").count(), 3);
  assert.equal(
    await page.locator("#answer .criteria .c-not_met .mark").textContent(),
    "✗",
  );
  assert.match(await page.locator("#answer .try").textContent(), /移到左边/);
  await page.screenshot({ path: "/tmp/along-build/ui/verdict.png" });
});

test("接线表和逐根核对显示在通用任务界面", async (t) => {
  const task = draft();
  task.state.scenario = "wiring";
  task.plan.connections = [
    {
      id: "c1",
      step_id: "s1",
      from_pin: "ESP32 3V3",
      to_pin: "BME280 Vin",
      wire_color: "红",
    },
    {
      id: "c2",
      step_id: "s1",
      from_pin: "ESP32 IO21",
      to_pin: "BME280 SDI",
      wire_color: "黄",
    },
  ];
  const { page } = await setup(t, task, {
    chatResult: {
      verdict: "unclear",
      headline: "数据线端点没看清",
      connection_checks: [
        { connection_id: "c1", status: "met", frame_ids: ["frame-1"] },
        { connection_id: "c2", status: "unseen", frame_ids: [] },
      ],
    },
  });
  await page.click("#enter-task");
  assert.equal(
    await page.locator("table[aria-label='接线表'] tbody tr").count(),
    2,
  );
  assert.equal(await page.getByText(/电路板与传感器接线/).count(), 0);
  await page.click("#confirm-plan");
  await page.waitForFunction(
    () => document.getElementById("stage").textContent === "一起操作",
  );
  await ask(page, "我接对了吗");
  assert.equal(await page.locator("#answer .criteria .c-met").count(), 1);
  assert.equal(await page.locator("#answer .criteria .c-unseen").count(), 1);
});

test("中英文即时切换，刷新后保留选择", async (t) => {
  const { page, model } = await setup(t);
  await page.click("#language-toggle");
  await page.waitForFunction(() => document.documentElement.lang === "en-GB");
  assert.match(await page.locator("#enter-task").textContent(), /Let's begin/);
  assert.equal(
    await page.locator("#back-home").getAttribute("aria-label"),
    "Back to home",
  );
  await page.reload();
  await page.waitForFunction(
    () => !document.getElementById("enter-task").disabled,
  );
  assert.equal(await page.locator("html").getAttribute("lang"), "en-GB");
  await page.click("#enter-task");
  assert.equal(
    (await page.locator("#goal-title").textContent()).trim(),
    "What would you like to do?",
  );
  assert.equal(
    await page.locator("#question").getAttribute("placeholder"),
    "What would you like to do?",
  );
  await ask(page, "Help me organise the workbench");
  assert.equal(
    model.requests.find((r) => r.path === "/api/chat").language,
    "en",
  );
  assert.equal(await page.locator("#stage").textContent(), "Review the plan");
  await page.click("#language-toggle");
  assert.equal(await page.locator("#stage").textContent(), "确认方案");
  assert.equal(
    await page.locator("#question").getAttribute("placeholder"),
    "哪里不合适？直接说，我来改",
  );
});

test("运行中切换不重置步骤或相机，回答与图片上下文标签同步", async (t) => {
  const task = draft();
  task.status = "active";
  task.state.plan_confirmed = true;
  task.state.steps.s1 = "user_reported_done";
  const { page, model } = await setup(t, task, {
    chatResult: {
      verdict: "ok",
      headline: "现有回答保留原文",
      retrieval: {
        question_frames: [{ id: "frame-1", captured: 100 }],
        tools: [
          {
            call: { tool: "open" },
            result: { frames: [{ id: "frame-2", captured: 99 }] },
          },
        ],
      },
    },
  });
  await page.click("#enter-task");
  await ask(page, "上一步对吗");
  await page.click("#language-toggle");
  assert.equal(await page.locator("#stage").textContent(), "In progress");
  assert.equal(
    await page.locator("#answer .verdict").textContent(),
    "Looks right",
  );
  assert.equal(
    await page.locator("#answer .headline").textContent(),
    "现有回答保留原文",
  );
  assert.match(
    await page.locator("#messages .ctx summary").textContent(),
    /Images seen by AI/,
  );
  await page.click("#open-menu");
  await page.locator("#messages .ctx summary").click();
  await page.locator("#messages .ctx .ctx-label").last().waitFor();
  assert.match(await page.locator("#messages .ctx").textContent(), /Review/);
  assert.equal(model.task.state.steps.s1, "user_reported_done");
  assert.equal(
    await page.evaluate(
      () =>
        document.getElementById("camera").srcObject.getVideoTracks()[0]
          .readyState,
    ),
    "live",
  );
  assert.equal(
    model.requests.filter((r) => r.path.endsWith("/change")).length,
    0,
  );
});

test("英文浏览器默认英文，插值不会执行用户内容", async (t) => {
  const { page } = await setup(t, null, { locale: "en-GB" });
  assert.equal(await page.locator("html").getAttribute("lang"), "en-GB");
  assert.equal(
    await page.evaluate(() =>
      window.AlongI18n.t("试试：{0}", ["<img src=x onerror=alert(1)>{1}"]),
    ),
    "Try: <img src=x onerror=alert(1)>{1}",
  );
  assert.equal(
    await page.evaluate(() => window.AlongI18n.t("{0} 帧", [0])),
    "0 frames",
  );
});

test("模型调用时禁止切换语言，调用完成后恢复", async (t) => {
  const { page } = await setup(t, null, { chatDelay: 500 });
  await page.click("#enter-task");
  await page.fill("#question", "整理工作台");
  await page.click("#send");
  assert.equal(await page.locator("#language-toggle").isDisabled(), true);
  await page.waitForFunction(() => !document.getElementById("send").disabled);
  assert.equal(await page.locator("#language-toggle").isDisabled(), false);
});

test("本地存储不可用仍可切换；英文移动端无横向溢出", async (t) => {
  const { page } = await setup(t, null, {
    locale: "en-GB",
    blockStorage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.locator("html").getAttribute("lang"), "en-GB");
  await page.click("#language-toggle");
  assert.equal(await page.locator("html").getAttribute("lang"), "zh-CN");
  await page.click("#language-toggle");
  await page.click("#enter-task");
  await ask(page, "Help me organise the workbench");
  await page.click("#confirm-plan");
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
});

test("界面翻译目录覆盖全部静态与动态文案", async (t) => {
  const { page } = await setup(t, null, { locale: "en-GB" });
  const missing = await page.evaluate(async () => {
    const source = await (await fetch("/static/app.js")).text();
    const keys = [
      ...source.matchAll(/(?:tr|cameraPlaceholder)\(\s*("(?:[^"\\]|\\.)*")/g),
    ].map((m) => JSON.parse(m[1]));
    keys.push(
      ...Object.values(BASIS),
      ...Object.values(VERDICTS),
      ...Object.values(TOOL_NAMES),
      ...Object.values(TRANSITIONS)
        .flat()
        .map((item) => item[1]),
    );
    document
      .querySelectorAll(
        "[data-i18n], [data-i18n-aria-label], [data-i18n-placeholder], [data-i18n-title]",
      )
      .forEach((el) => {
        for (const attr of [
          "data-i18n",
          "data-i18n-aria-label",
          "data-i18n-placeholder",
          "data-i18n-title",
        ]) {
          if (el.hasAttribute(attr)) keys.push(el.getAttribute(attr));
        }
      });
    return [...new Set(keys)].filter(
      (key) =>
        /\p{Script=Han}/u.test(key) &&
        window.AlongI18n.t(key, ["{0}", "{1}", "{2}", "{3}", "{4}", "{5}"]) ===
          key,
    );
  });
  assert.deepEqual(missing, []);
  assert.deepEqual(
    await page.evaluate(() => [
      window.AlongI18n.t("__proto__"),
      window.AlongI18n.t("constructor"),
      window.AlongI18n.t(null),
    ]),
    ["__proto__", "constructor", ""],
  );
});

test("桌面展示占约八成宽度，首页与执行页放大且中英文无溢出", async (t) => {
  const { page } = await setup(t);
  for (const width of [1024, 1440, 1920, 2560]) {
    await page.setViewportSize({ width, height: 1080 });
    const main = await page.locator("main").boundingBox();
    const home = await page.locator("#home").boundingBox();
    assert.ok(main.width / width >= 0.77 && main.width / width <= 0.79);
    assert.ok(home.width >= main.width * 0.98);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.click("#enter-task");
  await ask(page, "整理工作台");
  const plan = await page.locator(".plan-pane").boundingBox();
  assert.ok(plan.width > 1100);
  await page.click("#confirm-plan");
  await page.locator(".dock").waitFor();
  for (const width of [1024, 1440, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    for (let language = 0; language < 2; language++) {
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
      );
      const dock = await page.locator(".dock").boundingBox();
      assert.ok(dock.width / width >= 0.77 && dock.width / width <= 0.79);
      const input = await page.locator("#question").boundingBox();
      assert.ok(input.width > 80);
      const next = await page.locator("#next-step").boundingBox();
      assert.ok(next.x >= 0 && next.x + next.width <= width);
      await page.click("#language-toggle");
    }
  }
  assert.ok(
    (await page
      .locator("#camera")
      .evaluate((el) => el.getBoundingClientRect().width)) > 300,
  );
  assert.ok(
    (await page
      .locator(".current-step .instruction")
      .evaluate((el) => parseFloat(getComputedStyle(el).fontSize))) >= 22,
  );
});

test("语音服务失败保留输入，不发送 Agent，恢复按钮并显示真实错误", async (t) => {
  const { page, model } = await setup(t);
  await page.click("#enter-task");
  await page.fill("#question", "保留这段文字");
  await page.route("**/api/speech", (route) =>
    route.fulfill({
      status: 502,
      json: { detail: "语音识别服务限流，请稍后重试" },
    }),
  );
  await page.waitForFunction(() => !document.getElementById("mic").disabled);
  await page.click("#mic");
  await page.waitForFunction(() =>
    document.getElementById("mic").classList.contains("recording"),
  );
  await page.waitForTimeout(200);
  await page.click("#mic");
  await page.waitForFunction(() =>
    document.getElementById("toast").textContent.includes("限流"),
  );
  assert.equal(await page.locator("#question").inputValue(), "保留这段文字");
  assert.equal(model.requests.filter((r) => r.path === "/api/chat").length, 0);
  assert.equal(await page.locator("#mic").isDisabled(), false);
  assert.match(await page.locator("#toast").textContent(), /限流/);
});

test("顶部任务入口始终可见，未开始时说明状态并禁用暂停", async (t) => {
  const { page, model } = await setup(t);
  await page.click("#enter-task");
  assert.equal(await page.locator("#stage").textContent(), "尚未开始任务");
  assert.equal(await page.locator("#open-menu").isVisible(), true);
  assert.equal(await page.locator("#pause-task").isVisible(), true);
  assert.equal(await page.locator("#pause-task").isDisabled(), true);
  await page.click("#open-menu");
  assert.equal(await page.locator("#drawer-empty").isVisible(), true);
  assert.equal(
    await page.locator("#open-menu").getAttribute("aria-expanded"),
    "true",
  );
  assert.equal(await page.locator("[data-requires-task]:visible").count(), 0);
  await page.click("#close-menu");
  await ask(page, "整理工作台");
  assert.equal(await page.locator("#pause-task").isDisabled(), true);
  await page.click("#open-menu");
  assert.equal(await page.locator("#drawer-empty").isVisible(), false);
  await page.click("#close-menu");
  assert.equal(model.task.status, "draft");
});

test("顶部直接暂停和继续，中英文及小屏保持可见，结束后回到未开始状态", async (t) => {
  const task = draft();
  task.state.plan_confirmed = true;
  task.status = "active";
  const { page, model } = await setup(t, task);
  await page.click("#enter-task");
  await page.waitForFunction(
    () => !document.getElementById("pause-task").disabled,
  );
  for (const width of [390, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (let lang = 0; lang < 2; lang++) {
      for (const id of ["#pause-task", "#open-menu"]) {
        const box = await page.locator(id).boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= width);
      }
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
      );
      await page.click("#language-toggle");
    }
  }
  await page.click("#pause-task");
  await page.waitForFunction(
    () => document.getElementById("pause-task").textContent === "继续",
  );
  assert.equal(model.task.status, "paused");
  assert.equal(await page.locator("#stage").textContent(), "任务已暂停");
  await page.waitForFunction(
    () => !document.getElementById("pause-task").disabled,
  );
  assert.equal(await page.locator("#pause-task").isDisabled(), false);
  await page.click("#language-toggle");
  assert.equal(await page.locator("#stage").textContent(), "Task paused");
  await page.click("#pause-task");
  await page.waitForFunction(
    () => document.getElementById("paused-overlay").hidden,
  );
  assert.equal(model.task.status, "active");
  assert.equal(await page.locator("#pause-task").textContent(), "Pause");
  await page.click("#open-menu");
  await page.click("#finish-task");
  await page.waitForFunction(
    () => document.getElementById("stage").textContent === "No task started",
  );
  assert.equal(await page.locator("#open-menu").isVisible(), true);
  assert.equal(await page.locator("#pause-task").isDisabled(), true);
});
