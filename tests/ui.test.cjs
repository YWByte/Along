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
      json: { token: "test", frame_interval: 0.15, max_storage_mb: 100 },
    }),
  );
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    const data = req.headers()["content-type"]?.includes("application/json")
      ? req.postDataJSON()
      : null;
    model.requests.push({ path, data });
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
    () => !document.getElementById("enter-instant").disabled,
  );
  return { page, model };
}
async function photo(page) {
  await page.click("#enter-instant");
  await page.waitForFunction(
    () => document.getElementById("camera").videoWidth > 0,
  );
  await page.click("#take-photo");
  await page.waitForFunction(
    () => !document.getElementById("question").disabled,
  );
}
async function ask(page, text) {
  await page.fill("#question", text);
  await page.click("#send");
  await page.waitForFunction(() => !document.getElementById("send").disabled);
}

test("入口只有两个按钮；识别模式拍照前不能提问，重拍清空旧图上下文", async (t) => {
  const { page, model } = await setup(t);
  assert.equal(await page.locator("button:visible").count(), 2);
  await page.click("#enter-instant");
  assert.equal(await page.locator("#question").isDisabled(), true);
  await page.waitForFunction(
    () => document.getElementById("camera").videoWidth > 0,
  );
  assert.equal(model.media.length, 0);
  await page.click("#take-photo");
  await page.waitForFunction(
    () => !document.getElementById("question").disabled,
  );
  assert.equal(
    await page.evaluate(() => document.getElementById("camera").srcObject),
    null,
  );
  await ask(page, "这是什么？");
  const first = model.requests.find((r) => r.path === "/api/chat").data;
  assert.equal(first.mode, "instant");
  assert.equal(first.task_id, null);
  assert.equal(await page.locator("#messages details").count(), 0);
  assert.equal(await page.locator("#task-panel").isVisible(), false);
  await ask(page, "它是什么颜色？");
  assert.equal(model.media.length, 2);
  assert.equal(
    model.requests.filter((r) => r.path === "/api/chat").at(-1).data
      .conversation_id,
    first.conversation_id,
  );
  await page.click("#retake-photo");
  assert.equal(await page.locator("#messages article").count(), 0);
  await page.waitForFunction(
    () => document.getElementById("camera").videoWidth > 0,
  );
  await page.click("#take-photo");
  await ask(page, "新图是什么？");
  assert.notEqual(
    model.requests.filter((r) => r.path === "/api/chat").at(-1).data
      .conversation_id,
    first.conversation_id,
  );
  await page.click("#back-home");
  assert.equal(await page.locator("button:visible").count(), 2);
});

test("任务从目标到方案、当前步骤、暂停恢复、完成；识别与任务采集隔离", async (t) => {
  const { page, model } = await setup(t);
  await page.click("#enter-task");
  assert.equal(await page.locator("#camera-panel").isVisible(), false);
  await ask(page, "整理工作台");
  assert.equal(await page.locator("#stage").textContent(), "确认方案");
  assert.equal(model.media.length, 0);
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
  await page.getByRole("button", { name: "已检查，下一步" }).click();
  await page.waitForFunction(() =>
    document.querySelector(".current-step").textContent.includes("放入收纳盒"),
  );
  await page.click("#back-home");
  assert.equal(model.task.status, "paused");
  await page.waitForFunction(() => !uploading && !queueSending);
  await page.evaluate(() => flushQueue());
  const uploads = model.media.length;
  await page.click("#enter-instant");
  await page.waitForFunction(
    () => document.getElementById("camera").videoWidth > 0,
  );
  await new Promise((r) => setTimeout(r, 500));
  assert.equal(model.media.length, uploads);
  await page.click("#take-photo");
  await ask(page, "这是什么？");
  assert.equal(
    model.requests.filter((r) => r.path === "/api/chat").at(-1).data.task_id,
    null,
  );
  await page.click("#back-home");
  await page.click("#enter-task");
  assert.equal(await page.locator("#messages article").count(), 0);
  await page.click("#resume-task");
  await page.waitForFunction(
    () => document.getElementById("task-status").textContent === "进行中",
  );
  await page.getByRole("button", { name: "已检查，完成这一步" }).click();
  await page.getByRole("button", { name: "完成任务", exact: true }).click();
  await page.waitForFunction(
    () => document.getElementById("stage").textContent === "说目标",
  );
  assert.equal(model.task, null);
});

test("相机拒绝授权可以重试，未拍照不能发送", async (t) => {
  const { page, model } = await setup(t, null, { denyCamera: true });
  await page.click("#enter-instant");
  await page.waitForFunction(
    () => !document.getElementById("camera-toggle").disabled,
  );
  assert.match(
    await page.locator("#camera-placeholder").textContent(),
    /无法打开相机/,
  );
  assert.equal(await page.locator("#send").isDisabled(), true);
  await page.click("#camera-toggle");
  await page.waitForFunction(
    () => !document.getElementById("back-home").disabled,
  );
  await page.click("#back-home");
  assert.equal(model.media.length, 0);
});

test("模型失败保留问题和照片，可重试；请求期间不允许切换模式", async (t) => {
  const { page, model } = await setup(t, null, { chatDelay: 150 });
  await photo(page);
  model.failChat = true;
  await page.fill("#question", "这是什么？");
  await page.click("#send");
  assert.equal(await page.locator("#back-home").isDisabled(), true);
  await page.waitForFunction(() => !document.getElementById("send").disabled);
  assert.equal(await page.locator("#question").inputValue(), "这是什么？");
  assert.equal(await page.locator("#uploaded-preview").isVisible(), true);
  model.failChat = false;
  await ask(page, "这是什么？");
  assert.match(await page.locator("#messages").textContent(), /测试画面/);
});

test("修改方案不推进步骤；生成失败允许重试；取消开始不打开相机", async (t) => {
  const { page, model } = await setup(t, draft(), { cancelDialogs: true });
  await page.click("#enter-task");
  model.failPrepare = true;
  await ask(page, "换成三个盒子");
  assert.match(await page.locator("#messages").textContent(), /搜索失败/);
  model.failPrepare = false;
  await ask(page, "换成三个盒子");
  assert.equal(model.task.state.plan_confirmed, false);
  await page.click("#confirm-plan");
  assert.equal(model.task.status, "draft");
  assert.equal(model.media.length, 0);
});

test("语音识别回填输入框，需要用户确认发送", async (t) => {
  const { page, model } = await setup(t);
  await photo(page);
  await page.click("#mic");
  await page.waitForFunction(
    () => document.getElementById("mic").textContent === "停止录音",
  );
  assert.equal(await page.locator("#back-home").isDisabled(), true);
  await page.waitForTimeout(300);
  await page.click("#mic");
  await page.waitForFunction(
    () => document.getElementById("question").value === "这个东西是什么",
  );
  assert.equal(model.requests.filter((r) => r.path === "/api/chat").length, 0);
  assert.equal(await page.locator("#send").isDisabled(), false);
});

test("移动端无横向溢出；已有任务不会在首页或识别模式自动采集", async (t) => {
  const task = draft();
  task.status = "active";
  task.state.plan_confirmed = true;
  const { page, model } = await setup(t, task);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(model.media.length, 0);
  await photo(page);
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
  await page.getByRole("button", { name: "已检查，下一步" }).click();
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
  assert.equal(await page.locator(".plan-overview").count(), 0);
  assert.match(await page.locator("#task-content").textContent(), /型号是什么/);
  await ask(page, "只整理桌面，不拆设备");
  assert.equal(await page.locator("#confirm-plan").isVisible(), true);
  assert.equal(model.media.length, 0);
  assert.match(
    await page.locator("#task-content").textContent(),
    /通用知识建议/,
  );
});
