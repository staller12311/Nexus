<div align="center">

<h1>Nexus 智枢</h1>

<h3>把大模型变成真正的生产力工具</h3>

<p><b>支持 1M 上下文、本地知识库与代码沙箱的私有化 Agent 中台</b></p>

[![License](https://img.shields.io/badge/License-Apache_2.0-3b82f6?style=flat-square&logo=apache&logoColor=white)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-19-61DAFB?style=flat-square&logo=react&logoColor=black)](https://react.dev/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=flat-square&logo=docker&logoColor=white)](https://docs.docker.com/compose/)

</div>

[核心特性](#核心特性) · [系统架构](#系统架构) · [界面预览](#界面预览) · [快速开始](#快速开始) · [配置](#配置)

## 为什么做这个？

当前市面上的 AI Agent（如 Coze、Dify）多为云端 SaaS，企业核心数据面临泄露风险；而基于 LangChain 的开源框架往往抽象过重，遇到复杂 Bug 难以调试。

**Nexus 智枢** 是一个**极致轻量、本地优先**的 Agent 中台。我们纯手写了底层 Agent Loop，让你在自己的电脑或服务器上安全地读写文件、执行 Python、检索私有知识库。接本地 Ollama / vLLM 时可完全离线运行；接云模型时仅推理请求出网。

## 它能帮你做什么？

- **本地代码重构**：通过多根挂载（`@libs`），让 AI 安全地读取依赖库，并帮你重构整个项目。
- **私有知识库问答**：上传 PDF/Word 财报，知识库与检索留在本地，AI 精准检索并给出结论（支持 1M 超长上下文）。
- **安全执行沙箱**：让 AI 现场写 Python 代码并运行（如处理 Excel 数据），全程在 workspace 沙箱内执行，高危操作需人工点击确认。

## 竞品对比（为什么选我们？）

直接看差异：

| 维度 | 云端 SaaS (如 Coze/Dify) | 纯框架 (如 LangChain) | Nexus 智枢 |
|---|---|---|---|
| 数据隐私 | 数据需上云，有泄露风险 | 需自行搭建，缺乏安全沙箱 | 本地部署；接本地模型可全离线 |
| 执行能力 | 难以直接操作本地文件 | 需大量开发适配器 | 原生支持文件 CRUD、Python 执行 |
| 安全控制 | 依赖平台规则 | 几乎没有防护 | 路径越界拦截、HITL 审批 |
| 底层可控性 | 黑盒调包 | 抽象过重，难以 Debug | 纯手写 Agent Loop，无黑盒 |

## 系统架构

```mermaid
graph TD
    User((用户)) -->|消息 / 文档| FE[React 19 前端]
    FE -->|SSE 流式| API[FastAPI 网关]

    subgraph AgentCore [Agent Core 自研内核]
        API -->|任务| Loop[Agent Loop]
        Loop -->|1 流式请求| Provider[Provider 适配层]
        Provider -->|2 推理 / 正文| Loop
        Loop -->|3 拼装 Tool Calls| Tools[工具执行器]
        Tools -->|4 读写| FS[(workspace)]
        Tools -->|5 执行 Python| Code[run_python_code]
        Tools -->|6 结果回灌| Loop
    end

    subgraph ModelLayer [Model Layer]
        Provider -.->|直连| DS[DeepSeek-V4 1M]
        Provider -.->|热插拔| Other[千问 / 本地 Ollama]
    end

    Loop -->|7 事件 + 审批| API
    API -->|SSE 打字机 / 审批卡| FE

    classDef fe fill:#e0f2fe,stroke:#0284c7,stroke-width:1px,color:#0f172a;
    classDef gw fill:#f1f5f9,stroke:#64748b,stroke-width:1px,color:#0f172a;
    classDef core fill:#ecfeff,stroke:#0891b2,stroke-width:1px,color:#0f172a;
    classDef model fill:#f8fafc,stroke:#94a3b8,stroke-width:1px,color:#0f172a;
    class FE fe;
    class API gw;
    class Loop,Provider,Tools,FS,Code core;
    class DS,Other model;
```

**工具调用**

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant FE as 前端
    participant BE as 后端
    participant Agent as Agent
    participant LLM as 大模型

    U->>FE: 提交任务
    FE->>BE: POST /api/chat/stream
    BE->>Agent: run_stream()
    Agent->>LLM: stream=true
    LLM-->>Agent: reasoning 片段
    Agent-->>BE: reasoning_delta
    BE-->>FE: 思考中
    LLM-->>Agent: tool_calls 片段
    Note over Agent: 按 index 拼装参数
    Agent->>Agent: 执行工具
    Agent-->>BE: tool_call_end
    BE-->>FE: 工具卡成功
    LLM-->>Agent: 最终回答
    Agent-->>BE: text_delta
    BE-->>FE: 打字机输出
    Agent-->>BE: message_end
```

**人工审批**

```mermaid
sequenceDiagram
    participant U as 用户
    participant FE as 前端
    participant BE as 后端
    participant Agent as Agent

    Agent->>Agent: 识别 write_file
    Agent-->>BE: approval_required
    BE-->>FE: 黄色审批卡
    U->>FE: 点击同意
    FE->>BE: POST /api/chat/approve
    BE->>Agent: 唤醒 ApprovalGate
    Agent->>Agent: 执行 write_file
    Agent-->>BE: tool_call_end
    BE-->>FE: 工具卡变绿
```

## 核心特性

- **自研 Agent Loop**：推理/正文双通道流式解析，`tool_calls` 碎片按 index 拼装，不依赖任何 Agent 框架
- **多模型热插拔**：任意 OpenAI 兼容端点（DeepSeek / 千问 / Moonshot / 智谱 / Ollama / vLLM），一个 YAML 搞定
- **真·流式 Web 体验**：SSE 打字机、思考链折叠、工具卡实时状态、React 19 + Zustand
- **企业级安全沙箱**：workspace realpath 越界校验、`@mount` 只读挂载、命令/域名白名单、危险工具审批闸门
- **长上下文记忆**：1M 窗口校准 + 大参数历史压缩 + 80% 自动摘要（不再粗暴砍历史）
- **协议级健壮性**：非法 `function.arguments` 本地拦截、工具异常回灌、上游 400 防御
- **可观测与成本**：Token/缓存命中率/节省估算、结构化日志与密钥脱敏、会话级统计
- **一键启动 / 一键部署**：Windows `start.bat`；Docker Compose 一条命令跑通

## 界面预览

<p align="center"><a href="./assets/ui-chat.png"><img src="./assets/ui-chat.png" alt="Nexus 智枢 对话界面：流式回答、思考链与工具调用卡" width="100%" /></a></p>

<details>
<summary><b>审批卡片与代码沙箱（点击展开）</b></summary>

<p align="center"><a href="./assets/ui-approval.png"><img src="./assets/ui-approval.png" alt="Nexus 智枢 审批卡片：write_file 只读代码预览、同意 / 拒绝" width="100%" /></a></p>

</details>

## 项目结构

```text
nexus-zhishu/
├── agent/  # 自研 Agent 内核（零 Agent 框架依赖）
│   ├── loop.py  # 主循环：流式解析、工具执行、上下文压缩/摘要
│   ├── provider.py  # OpenAI 兼容 Provider、usage、重试与错误分类
│   ├── registry.py  # 工具注册表与 JSON Schema
│   ├── storage.py  # SQLite 会话 / 消息持久化
│   ├── config.py  # pydantic 严格配置校验
│   └── tools/  # 文件、shell、HTTP、时间、run_python_code
├── backend/  # FastAPI + SSE 网关
│   ├── api/  # 路由与请求/响应 Schema
│   └── service/  # AgentService、审批闸门、知识库、工作区
├── frontend/  # React 19 + Vite + Tailwind
│   └── src/  # components / store / lib / api / 测试
├── tests/  # pytest：主循环、工具安全、审批、Web API
├── docker/  # Nginx 配置与后端容器入口
├── assets/  # README 截图（ui-chat / ui-approval）
├── Dockerfile  # 多阶段：Node 构建 → Python 运行时 → Nginx
├── docker-compose.yml  # backend + web，挂载 data / workspace
├── config.example.yaml  # 配置模板（模型、沙箱、审批、上下文）
├── start.bat  # Windows 一键启动（前后端 + 自动开浏览器）
├── docs/  # 深度文档
│   ├── whitepaper.md  # 技术白皮书
│   ├── design-decisions.md  # 68 条设计决策
│   ├── api.md  # REST / SSE 协议
│   ├── models.md  # 模型接入
│   ├── cli.md  # 命令行用法
│   ├── frontend.md  # 前端实现
│   ├── tool-development.md  # 工具扩展
│   └── development.md  # 测试 / 交付 / 历史
├── LICENSE  # Apache-2.0 开源协议
└── README.md
```

## 快速开始

**方式 A：Windows 本地一键启动**

```bat
start.bat
```

自动启动后端（8000）与前端（5173）并打开浏览器；关闭窗口或 Ctrl+C 同时停止两者，不留僵尸端口。

**方式 B：Docker 一键部署（推荐给服务器 / 给别人试用）**

```bash
git clone https://github.com/staller12311/Nexus.git && cd Nexus
cp .env.example .env          # 填入 DEEPSEEK_API_KEY（或其它模型密钥）
docker compose up -d          # 首次构建约 3–5 分钟
```

打开 <http://localhost:8080>。`data/`（SQLite 会话、日志）与 `workspace/`（Agent 文件沙箱）
通过 volume 挂载到宿主机；Nginx 负责静态站点与 `/api` 反代，SSE 已关闭缓冲。

> 安全提示：本项目默认面向**完全信任的本地开发环境**（`config.yaml` 中 `require_approval: false`）。
> 如果 workspace 会处理他人代码或不可信输入，请把 `tools.require_approval` 改为 `true`，
> 危险工具（`run_python_code` / `delete_path` / `run_shell`）将重新走人工审批。

## 手动安装（开发模式）

适合想改代码 / 跑测试的开发者。

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt     # Windows
# python -m pip install -r requirements.txt                     # macOS / Linux
```

要求 Python 3.11+。

## 配置

```bash
cp config.example.yaml config.yaml      # Windows: copy config.example.yaml config.yaml
cp .env.example .env                    # 填入真实密钥
```

配置加载优先级：命令行 `--config` > 环境变量 `AGENT_CONFIG` > `./config.yaml`。
加载后用 pydantic 校验，缺失必填项会直接报出字段路径，例如：

```
配置校验失败，缺失必填字段：models.deepseek.base_url
  - models.deepseek.base_url: 缺少必填项
```

### 模型接入示例

```yaml
models:
  moonshot:
    base_url: https://api.moonshot.cn/v1
    model: moonshot-v1-8k
    api_key_env: MOONSHOT_API_KEY
  qwen:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    model: qwen-plus
    api_key_env: DASHSCOPE_API_KEY
  zhipu:
    base_url: https://open.bigmodel.cn/api/paas/v4
    model: glm-4-plus
    api_key_env: ZHIPUAI_API_KEY
  openrouter:
    base_url: https://openrouter.ai/api/v1
    model: openai/gpt-4o-mini
    api_key_env: OPENROUTER_API_KEY
```

### 新增模型

在 `config.yaml` 的 `models` 下添加任意 OpenAI 兼容端点，或在 Web 界面「模型管理」中直接添加。字段说明、Moonshot 示例与 `config check` 输出见 [docs/models.md](docs/models.md)。

## CLI 用法

除 Web 界面外，也可以直接用命令行运行：

```bash
python -m agent run "你的问题"    # 单次问答
python -m agent chat             # 交互式多轮
python -m agent sessions resume <id>   # 恢复会话
```

完整命令、交互模式、会话恢复、退出码与真实输出见 [docs/cli.md](docs/cli.md)。

## Web 界面（FastAPI + React）

### 一键启动（Windows，推荐日常使用）

```bat
start.bat
```

自动启动后端（8000）与前端（5173）并打开浏览器；关闭窗口或 Ctrl+C 同时停止两者。

- REST 端点与 SSE 事件协议（含审批流）：[docs/api.md](docs/api.md)
- 前端组件、状态管理与历史消息渲染：[docs/frontend.md](docs/frontend.md)

## 开发与测试

```bash
.venv\Scripts\python.exe -m pytest -q
```

当前 **328 passed / 2 skipped**；前端 **37 个测试文件 / 199 passed + 6 skipped**。

完整测试范围、交付清单、已知待办与已清偿技术债见 [docs/development.md](docs/development.md)；新增工具（5 步 + 可运行模板）见 [docs/tool-development.md](docs/tool-development.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [技术白皮书](docs/whitepaper.md) | 项目概述、架构选型、核心突破、安全边界 |
| [设计决策](docs/design-decisions.md) | 68 条关键取舍与原因 |
| [Web API](docs/api.md) | REST 端点、SSE 事件协议、审批流 |
| [模型接入](docs/models.md) | 多 provider 配置与 `config check` |
| [CLI 用法](docs/cli.md) | 命令行、交互模式、会话恢复 |
| [前端实现](docs/frontend.md) | 组件、状态管理、历史消息渲染 |
| [工具开发](docs/tool-development.md) | 新增工具的 5 步模板 |
| [开发与测试](docs/development.md) | 测试、交付清单、待办、技术债 |

## 设计决策

68 条设计决策（配置校验、路径语义、工具边界、流式协议、审批线程模型、上下文摘要、前端交互）见 [docs/design-decisions.md](docs/design-decisions.md)。
