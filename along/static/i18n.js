"use strict";
window.AlongI18n = (() => {
  const translations = {
    active: "In progress",
    draft: "Draft",
    paused: "Paused",
    ended: "Finished",
    "通用知识，未经资料核验": "General knowledge · not source-verified",
    依据参考资料: "Based on reference material",
    部分步骤有资料依据: "Some steps have reference sources",
    "正在想下一步…": "Thinking about the next step…",
    一起做: "Together",
    一起操作: "In progress",
    补充条件: "A little more context",
    确认方案: "Review the plan",
    说目标: "Your goal",
    尚未开始任务: "No task started",
    查看任务: "View task",
    任务已暂停: "Task paused",
    确认方案后才能暂停: "Start the plan before pausing",
    "先告诉我具体目标，确认方案后开始。":
      "Tell me your goal, then review the plan to begin.",
    "说说你想完成的事…": "What would you like to do?",
    "回答上面的问题…": "Answer the question above…",
    "哪里不合适？直接说，我来改": "Want to change anything? Tell me…",
    "哪里不确定？问我…": "Not sure about something? Ask me…",
    "正在恢复对话…": "Restoring your conversation…",
    "任务已暂停，回来点“继续”即可":
      "Task paused. Select Resume when you return.",
    "请允许使用相机…": "Please allow camera access…",
    "相机没有打开，检查权限后重试":
      "Camera unavailable. Check permissions and try again.",
    "原始记录 {0}": "Original recording {0}",
    做对了: "Looks right",
    调整一下: "Needs an adjustment",
    看不清: "Not enough visual evidence",
    "回看 {0}–{1}": "Reviewed {0}–{1}",
    "{0} 帧": "{0} frames",
    "{0} 张画面": "{0} images",
    "{0} 份资料": "{0} sources",
    这次回答的依据: "Evidence for this answer",
    "本次核对：{0}": "Review focus: {0}",
    "以上是画面观察，不是动作或安全认证。":
      "These are visual observations, not certification of technique or safety.",
    "对比帧 {0}": "Comparison frame {0}",
    引用的画面: "Referenced images",
    "过程图片 {0}": "Recorded image {0}",
    "参考资料 {0}": "Reference {0}",
    回看: "Review",
    当前画面: "Current view",
    浏览目录: "Browse memory",
    搜索记录: "Search recordings",
    提问时画面: "View when you asked",
    "拼图 {0} 帧": "Contact sheet · {0} frames",
    "{0} 条事件": "{0} events",
    引脚细节原图: "Full-resolution pin detail",
    "{0} 第 {1} 帧 {2}": "{0} · frame {1} · {2}",
    "{0} 拼图": "{0} · contact sheet",
    "AI 看过的图片 · {0} 张{1}": "Images seen by AI · {0}{1}",
    " · {0} 次检索": " · {0} retrievals",
    "试试：{0}": "Try: {0}",
    "依据 · {0}": "Evidence · {0}",
    查看: "View",
    收起回答: "Dismiss answer",
    "存储已满，已关闭采集；待补传数据仍保留在本机":
      "Storage full. Recording stopped; pending uploads remain on this device.",
    "一段缓存记录无法补传：{0}":
      "A cached recording could not be uploaded: {0}",
    "本地待补传 {0} 段{1}": "Pending local uploads: {0}{1}",
    "任务进行中：画面按间隔保存，事件在后台整理":
      "Task active: images are recorded periodically and events are organised in the background.",
    "准备阶段按需取图；任务暂停时不持续记录":
      "Images are captured as needed. Paused tasks are not continuously recorded.",
    "离线缓存达到 80 MB，已关闭采集；请恢复连接后再开启":
      "Offline cache reached 80 MB. Recording stopped; reconnect before restarting.",
    "当前浏览器不支持相机，请在 localhost 用 Chrome 或 Safari":
      "Camera access is unavailable. Open localhost in Chrome or Safari.",
    "摄像头已断开，画面记录已停止": "Camera disconnected. Recording stopped.",
    已暂停: "Paused",
    相机未开启: "Camera off",
    "正在理解目标，准备方案…": "Understanding your goal and preparing a plan…",
    "正在调整方案…": "Updating your plan…",
    "“{0}” 正在想下一步…": "“{0}” · thinking about the next step…",
    "正在看这张照片…": "Looking at this image…",
    "调整说明请控制在 2000 字以内":
      "Keep revision requests under 2,000 characters.",
    "方案改好了，看看是否合适。":
      "The plan is updated. See if it works for you.",
    "请带我一起完成这个任务：{0}": "Help me complete this task: {0}",
    没有当前任务: "No current task.",
    "正在保存…": "Saving…",
    接线表: "Wiring table",
    从: "From",
    接到: "To",
    建议线色: "Suggested wire colour",
    不限: "Any",
    准备中: "Preparing",
    "正在判断要不要查资料，然后写方案…":
      "Deciding whether references are needed, then preparing your plan…",
    "开始前，先确认一件事": "One thing to confirm before we start",
    "在下面直接回答就好。": "Just answer below.",
    "{0} 步": "{0} steps",
    准备: "Preparation",
    接线参考: "Wiring reference",
    步骤: "Steps",
    来源: "Source",
    注意: "Keep in mind",
    都做完了: "All done",
    "所有步骤都由你确认完成。": "You have marked every step as complete.",
    做到这样就可以: "What to check",
    "← 上一步": "← Previous",
    完成任务: "Finish task",
    完成这一步: "Complete this step",
    "下一步 →": "Next →",
    " · 已完成": " · Done",
    回到这一步: "Return to this step",
    "准备：{0}": "Preparation: {0}",
    采用: "Accept",
    不采用: "Decline",
    已照做: "Done",
    取消: "Cancel",
    有效: "Worked",
    没用: "Did not work",
    说不准: "Not sure",
    已解决: "Resolved",
    "暂时没有。": "Nothing here yet.",
    "结束这次任务并停止记录？": "Finish this task and stop recording?",
    这次任务结束了: "Task finished.",
    处理中: "Working",
    "模型：{0}；媒体 {1} MB / {2} MB；语义检索{3}；事件 {4}{5}":
      "Model: {0}; media {1} MB / {2} MB; semantic retrieval: {3}; events: {4}{5}",
    启用: "on",
    关闭: "off",
    暂无: "none",
    选择任务: "Select a task",
    "连接中断，记录暂存本机": "Connection lost. Recordings are cached locally.",
    "正在准备方案…": "Preparing your plan…",
    "暂无已整理事件；原始图片可能已保存，请查看下方记录。":
      "No organised events yet. Raw images may already be saved; check below.",
    "未知：{0}": "Unknown: {0}",
    查看原图: "View original",
    "最近原始媒体（{0} 条）": "Recent raw media ({0})",
    " · 已过期": " · Expired",
    决策记录与计划修订: "Decisions and plan revisions",
    查看该任务保存的操作表: "View the saved task plan",
    查看该任务对话: "View this task's conversation",
    需要先开启相机才能开始: "Turn on the camera before starting.",
    已核对适用条件: "Applicable conditions checked",
    "说明书不得超过 10 MB": "Manuals must be no larger than 10 MB.",
    "资料已保存，请重新生成计划":
      "Reference saved. Please regenerate the plan.",
    "永久删除所选任务、对话及本地原始媒体？":
      "Permanently delete this task, its conversation and local raw media?",
    已重新排队: "Queued again.",
    "正在听写…": "Transcribing…",
    按住说话: "Hold to talk",
    "没听清，再说一次": "I couldn't hear that. Please try again.",
    松开发送: "Release to send",
    "连接失败，正在重试：{0}": "Connection failed. Retrying: {0}",
    返回首页: "Back to home",
    连接状态: "Connection status",
    连接中: "Connecting",
    任务菜单: "Task menu",
    今天想一起: "What shall we",
    "做点什么？": "do together today?",
    "说出你想完成的事。一起动手，随时问我。":
      "Tell me what you want to do. Let's work through it together.",
    我们开始吧: "Let's begin",
    "过程有记录，回答有依据。":
      "A record of your progress. Evidence behind each answer.",
    一起开始: "Let's begin",
    "想完成什么？": "What would you like to do?",
    "告诉我你的目标，也可以把材料放在镜头前。":
      "Tell me your goal. You can also show your materials to the camera.",
    "正在打开相机…": "Opening camera…",
    开启相机: "Turn on camera",
    记录中: "Recording",
    "开始后，相机每秒记录一张画面，只保存在本机；你提问时，相关画面会发给模型分析；生成过程事件时也会发送帧拼图。AI 的判断不等于安全认证。":
      "Once started, the camera records one image per second and stores it on this device. Relevant images are sent to the AI when you ask a question; contact sheets are also sent to summarise progress. AI judgments are not safety certification.",
    开始: "Start",
    重新生成方案: "Regenerate plan",
    步骤进度: "Step progress",
    "暂停期间不记录画面。": "No images are recorded while paused.",
    继续: "Resume",
    "或者打字…": "Or type here…",
    问题或任务目标: "Question or task goal",
    发送: "Send",
    任务: "Task",
    暂停: "Pause",
    结束任务: "Finish task",
    对话: "Conversation",
    完整方案: "Full plan",
    建议与待确认: "Suggestions and open questions",
    "补充资料、调整方案": "References and revisions",
    "说明书或教程网址（可选）": "Manual or tutorial URL (optional)",
    资料网址: "Reference URL",
    需要调整什么: "What needs to change?",
    调整原因: "Reason for revision",
    "导入说明书（PDF / TXT / MD）": "Import a manual (PDF / TXT / MD)",
    "调整需先暂停，新方案要重新开始。":
      "Pause before revising. A revised plan must be started again.",
    过程记录: "Recordings",
    历史任务: "Task history",
    查看记录: "View recordings",
    删除所选任务: "Delete selected task",
    运行状态: "System status",
    重试失败的记录整理: "Retry failed event summaries",
  };
  let language = "zh";
  try {
    const saved = localStorage.getItem("along-language");
    language =
      saved === "en" || saved === "zh"
        ? saved
        : navigator.language?.startsWith("zh")
          ? "zh"
          : "en";
  } catch {
    language = navigator.language?.startsWith("zh") ? "zh" : "en";
  }
  const singular = {
    "{0} 帧": "{0} frame",
    "{0} 张画面": "{0} image",
    "{0} 份资料": "{0} source",
    "{0} 条事件": "{0} event",
    "{0} 步": "{0} step",
    "拼图 {0} 帧": "Contact sheet · {0} frame",
    " · {0} 次检索": " · {0} retrieval",
  };
  const chineseStatuses = {
    active: "进行中",
    draft: "草稿",
    paused: "已暂停",
    ended: "已结束",
  };
  function t(key, values = []) {
    key = String(key ?? "");
    const catalog = language === "en" ? translations : chineseStatuses;
    let text = Object.hasOwn(catalog, key) ? catalog[key] : key;
    if (
      language === "en" &&
      Number(values[0]) === 1 &&
      Object.hasOwn(singular, key)
    )
      text = singular[key];
    return text.replace(/\{(\d+)\}/g, (_, i) =>
      String(values[Number(i)] ?? ""),
    );
  }

  function apply() {
    document.documentElement.lang = language === "en" ? "en-GB" : "zh-CN";
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.dataset.i18n);
    });
    for (const attr of ["aria-label", "placeholder", "title"]) {
      document.querySelectorAll(`[data-i18n-${attr}]`).forEach((el) => {
        el.setAttribute(attr, t(el.getAttribute(`data-i18n-${attr}`)));
      });
    }
    const toggle = document.getElementById("language-toggle");
    toggle.textContent = language === "zh" ? "EN" : "中文";
    toggle.setAttribute(
      "aria-label",
      language === "zh" ? "Switch to English" : "切换到中文",
    );
  }
  function setLanguage(next) {
    if (!["zh", "en"].includes(next)) return;
    language = next;
    try {
      localStorage.setItem("along-language", language);
    } catch {
      /* Private browsing may disable storage. */
    }
    apply();
  }
  return {
    t,
    apply,
    setLanguage,
    get language() {
      return language;
    },
  };
})();
