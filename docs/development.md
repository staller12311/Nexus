# 开发与测试

> 返回 [README](../README.md)

## 测试

```bash
.venv\Scripts\python.exe -m pytest -q
```

> 必须用 `python -m pytest`（而不是裸 `pytest`）：测试通过包名 `agent.*` 导入，
> 需要仓库根目录在 `sys.path` 上，`python -m` 会把当前目录加进去。

当前全量：**339 passed / 2 skipped**（2 条 skip 是 Windows 符号链接权限，见第 18 条）。
后端用例：`tests/test_backend_service.py` 覆盖工作区 / 知识库 / 自定义模型 / 用量统计 / 审批接口；
本轮新增上下文管理 4 条（工具参数压缩、80% 摘要替换、摘要失败回退、1M 窗口识别），
以及此前的非法 JSON 本地拦截 / 审批修正，总数从 321 增至 328。
前端：39 个测试文件 / 208 passed + 6 skipped（含审批卡片、审批+流式稳定性、错误边界、模型菜单、状态栏、图片压缩与 Vision UI 用例）。

---

## 交付清单

验收标准第 7 条要求 README 覆盖安装、配置、用法与扩展方式，逐项对照：

- [x] **安装步骤**：`## 安装`
- [x] **配置说明**：`## 配置`（字段语义、加载优先级、多 provider 示例、加载规则）
- [x] **三种用法示例**：`run "问题"` / `chat` / `sessions resume <id>`，均在 `## 运行`，附真实终端输出
- [x] **如何新增一个模型**：`### 如何新增一个模型`（Moonshot 示例 + `.env` 步骤 + 真实 `config check` 输出）
- [x] **如何新增一个工具**：`## 如何新增一个工具`（`get_time` 示例 + 5 步改动 + 真实 `tools list` / `run` 输出）
- [x] **「设计决策」章节**：`## 设计决策`（68 条，含 Web 阶段的 34–68）

Web 界面（第一阶段 CLI 之外的增量交付）：

- [x] **后端 `backend/`**：FastAPI + SSE，只调用 `agent/`（`agent/` 全程零改动），
  端点见「### REST 端点」
- [x] **前端 `frontend/`**：React 19 + TypeScript + Vite 8 + Tailwind 3.4 + Zustand，
  布局/交互见上文「### 前端」
- [x] **代码高亮**：Markdown 围栏代码块 → 深色底 + 行号 + 语法高亮 + 横向滚动 + 复制按钮（第 34–35 条之外的新增组件）
- [x] **系统提示词设置**：`PATCH /api/sessions/{id}/system_prompt`，改完下一句立即生效（第 42 条）
- [x] **会话重命名 / 删除**：`PATCH` / `DELETE` + 悬浮按钮 + 二次确认 + 就地编辑（第 43 条）
- [x] **动态模型配置**：`custom_models` 表 + 掩码脱敏 + 每个请求临时组装 Provider（第 44 条）
- [x] **本地知识库 RAG**：`/api/kb/*` 上传 → 后台解析切片 → BM25 检索，`search_knowledge_base`
  工具动态注入（第 51–52 条）
- [x] **联网搜索**：`web_search` 工具（Bing 主 / 百度兜底）与「中断 / 重新生成」（第 53 条）
- [x] **思维链与真流式**：`reasoning_delta` / `text_delta` 逐 token 推送，思维链单独持久化（第 48–49、54 条）
- [x] **工作区切换 / 目录选择器**：`/api/config/workspace`（GET/POST/DELETE）+ `/api/fs/dirs`、
  `/api/fs/mkdir`，点着选不用手打绝对路径（第 58、60 条）
- [x] **多根工作区**：`@name` 命名挂载（`agent.extra_workspaces`），只读默认、越界红线不变（第 59 条）
- [x] **文件 CRUD 工具**：`create_dir` / `delete_path` / `move_path`，权限报错改成可操作的人话（第 61–62 条）
- [x] **运行时诊断**：`GET /api/health` 返回 `python` / `integrity` / `write_warning`（第 62 条②）
- [x] **极客状态栏**：单轮 usage（含缓存命中率）+ 会话级 `GET /api/stats` + 上下文占用 + 省钱估算（第 63 条）
- [x] **极简侧边栏 + 输入框内模型选择**：黑灰白配色、向上弹出的模型菜单（第 64 条）
- [x] **Human-in-the-loop 工具审批**：危险工具挂起 → SSE `approval_required` → 黄色审批卡
  （write_file 带只读代码预览）→ `POST /api/chat/approve`；超时/断开自动拒绝（第 65 条）
- [x] **run_python_code 代码执行器**：工作区临时脚本 + 子进程 + 超时/输出截断，
  默认关闭、默认纳入审批集合（第 66 条）
- [x] **非法 JSON arguments 本地拦截**：构造历史前 `json.loads` 校验，非法参数替换为 `{}`
  并回灌错误，彻底避免上游 400 与无脑重试（第 67 条）
- [x] **上下文记忆管理**：大代码 tool_call 历史压缩 + 1M 窗口识别 + 超 80% 用后台摘要
  替换最旧 10 条消息（第 68 条）

---

## 已知待办

- `agent/cli.py::render_tools_table()` 的工具说明表与 `registry.py` 的 `ToolSpec.description` 是两份
  （README 第 32 条），新增工具要改两处。

---

## 已清偿的技术债

- **密钥环境变量的测试隔离**：`tests/conftest.py` 的 autouse fixture `isolate_secret_env`
  在每个用例前统一删掉 `*_API_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` / `*_ACCESS_KEY`
  以及几个显式列出的名字；`test_config.py` 里那处单独的 `monkeypatch.delenv` 已删除。
  验证方式：`pytest tests/test_cli.py tests/test_config.py -q`（先跑会载入 `.env` 的 CLI 用例）
  仍然 64 passed。
- **`GET /api/sessions` 不再全量扫消息**：改用一条只读 SQL 取每个会话第一条 user 消息，
  命中现有索引 `idx_messages_session_seq`（第 41 条）。15k 消息的库上实测
  117.9ms → 24.3ms（服务层），HTTP 往返 59ms。
