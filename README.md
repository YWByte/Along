# ALONG

ALONG 是一个陪你完成动手任务的助手。它结合任务计划、对话和辅助画面，判断当前状态并给出下一步建议。画面在后台整理为事件记忆，需要核对过去的操作时可以回看。

## 使用流程

首页只有一个入口：**我们开始吧**。进入后打开相机，说出要做的事，ALONG 生成步骤并在屏幕上展示；确认开始后持续记录过程。你可以随时提问，它会直接回答；状态不明确时只问一个必要问题，不要求所有条件都通过相机确认。

准备阶段只在提交问题时保存参考图，不持续记录。参考图参与型号识别和计划生成，不混入执行事件。

前端保持通用，后台路由到专门的场景提示。接线任务额外生成逐根接线表；核对时只有目标连接都有明确图像证据才可判为正确。颜色只是识别辅助，不能替代两端引脚证据。

任务开始时会自动路由到场景：`installation`（设备安装与使用）、`exercise`（锻炼类）、`wiring`（低压电路板与传感器接线）或 `general`。安装与使用类开始前先确认具体型号，再查对应官方资料后生成方案；已提供型号不重复问，已有适用资料可复用。其他场景按具体资料缺口决定是否联网。

## 中英文切换

顶部的 **EN / 中文** 按钮可切换界面语言，选择保存在当前浏览器。首次进入根据浏览器语言选择中文或英文；浏览器不允许本地存储时仍可切换，但刷新后不保留选择。

后续生成的任务方案、AI 回答和语音识别跟随所选语言。已有方案、用户输入、历史回答和记忆事件保留原文，不自动翻译技术参数；切换不会重置任务或停止记录。模型请求或录音进行中暂时禁用切换，避免一次交互混用两种语言。API 客户端可通过 `X-Along-Language: zh` 或 `en` 指定语言，未指定时默认中文。

## 视觉记忆

- 每 1 秒采集 1 帧。
- 每 10 帧生成一个事件：E1 对应 F1–F10，E2 查看 F9–F20、负责 F11–F20，依此类推。前两帧用于保持动作连续。
- 生成事件时可以看到任务目标、步骤计划、上一个事件摘要和当前帧拼图。
- 提问时，主 Agent 读取整个任务的事件目录，默认附带当前原图与最近最多 12 格拼图；明确要求回看或确实需要更早证据时，最多再打开一个事件或时间段。
- 事件摘要只用于索引，不能充当已验证事实。回答可依据图像、用户描述和适用资料，并区分它们的来源。侧边栏会显示模型实际收到的图片。

已确认任务的普通问答默认一次模型调用，不再独立分析画面或常规审查。回看最多增加一次调用；安全关键操作保留审查，但共用 12 秒响应预算。用户未主动要求视觉核对时，回答不会讲后台拍摄过程；状态不明确或等待超时，会转为一个具体状态问题。回答附带 `timings`，区分决策、回看回答和安全审查耗时；计时不包含录音和 ASR。

## 快速开始

需要 Python 3.12 或 3.13，以及 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
cp .env.example .env   # 填写 ALONG_DEEPSEEK_API_KEY 和 ALONG_SILICONFLOW_API_KEY
uv run along serve
```

打开 http://127.0.0.1:8766 。

语音默认使用硅基流动 `FunAudioLLM/SenseVoiceSmall` 云端 API。录音会上传至该服务，识别后自动发送给 Agent；不需要安装本地 Whisper。中英文自动识别，当前接口未提供语言或术语提示参数。型号和引脚可能误识别，应结合任务及画面核对。

可选功能：

```bash
uv sync --extra speech && uv run along prepare-speech         # 本地识别：还需设置 ALONG_SPEECH_PROVIDER=local_whisper
uv sync --extra semantic && uv run along prepare-embeddings   # 本地语义检索，之后设置 ALONG_SEMANTIC_ENABLED=true
uv sync --extra device                                        # 原生相机客户端 along-device
```

## 配置

所有配置都通过 `ALONG_` 前缀的环境变量或 `.env` 设置，完整列表见 `.env.example`。常用项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ALONG_LLM_PROVIDER` | `deepseek` | 模型提供方，可选 `deepseek` 或 `codex_proxy` |
| `ALONG_DEEPSEEK_API_KEY` | 空 | 同时用于模型、搜索和网页抓取 |
| `ALONG_DEEPSEEK_MODEL` | `deepseek-flash` | 必须支持图片输入 |
| `ALONG_DEEPSEEK_REASONING_EFFORT` | `low` | 思考强度，可设为 `high` 或 `max` |
| `ALONG_SPEECH_PROVIDER` | `siliconflow` | 语音提供方，可选 `siliconflow` 或 `local_whisper`，不自动降级 |
| `ALONG_SILICONFLOW_API_KEY` | 空 | 硅基流动语音识别 Key，仅保存在后端 |
| `ALONG_SILICONFLOW_ASR_MODEL` | `FunAudioLLM/SenseVoiceSmall` | 云端 ASR 模型 |
| `ALONG_SPEECH_TIMEOUT` | `60` | 云端识别超时（秒） |
| `ALONG_PORT` | `8766` | 服务端口 |
| `ALONG_FRAME_INTERVAL` | `1` | 采帧间隔（秒） |
| `ALONG_EVENT_FRAMES` | `10` | 每个事件包含的帧数 |
| `ALONG_EVENT_OVERLAP_FRAMES` | `2` | 事件之间的重叠帧数 |
| `ALONG_MEMORY_ROUNDS` | `2` | 未确认任务的旧检索流程预算；已确认任务固定最多回看一次 |
| `ALONG_TASK_ANSWER_TIMEOUT` | `12` | 已确认任务的响应预算（秒，上限 12），超时转为状态询问，不采用未完成判断 |
| `ALONG_RETENTION_DAYS` | `7` | 数据保留天数 |

数据默认保存在 `~/Library/Application Support/ALONG`（macOS）或 `~/.local/share/ALONG`（Linux）。模型或搜索服务失败时会直接报错，不会编造结果，也不会静默降级。

## 项目结构

| 路径 | 内容 |
|---|---|
| `along/api.py` | FastAPI 服务和接口 |
| `along/engine.py` | 任务流程、问答 Agent、联网判断 |
| `along/scenarios.py` | 场景路由和各场景提示 |
| `along/memory.py`、`along/memory_catalog.py`、`along/memory_tools.py` | 事件记忆、目录和检索工具 |
| `along/sheets.py` | 帧拼图 |
| `along/providers/` | 模型 Provider 和 DeepSeek 搜索/抓取 |
| `along/static/` | 网页前端 |
| `scripts/` | 探针、集成测试和检索评估脚本 |
| `tests/` | 单元与流程测试 |

## 开发

```bash
uv run pytest
uv run ruff check .
uv run python scripts/probe.py   # 真实调用模型与搜索服务，需要 API Key
```

## 接线模拟验证

```bash
uv run python scripts/wiring_probe.py --output /tmp/along-wiring-probe
```

脚本生成明确标注的模拟图，使用真实 Provider 在隔离数据目录中准备方案、生成事件并检索回答。图片和轨迹写到指定输出目录，不修改现有任务。它验证的是图上拓扑和流程，不代表实物识别精度、导通、电气安全或设备实际工作。需要 API Key，会产生模型调用费用。
