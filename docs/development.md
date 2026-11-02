# 开发与测试

> 返回 [README](../README.md)

## 持续集成

`.github/workflows/ci.yml` 在每次 push 到 `main` 和每个 PR 上跑两条 job（都在 `windows-latest`，
因为 `tests/test_tools_security.py` 依赖 Windows 路径与目录联接）：

| Job | 内容 |
|---|---|
| Backend (pytest) | Python 3.11 + `pip install -r requirements.txt` + `python -m pytest -q` |
| Frontend | Node 22 + `npm ci` + `npx tsc --noEmit` + `npm run test` |

两个注意点：

- 前端 `.npmrc` 里写的是本机绝对缓存路径，CI 用 `npm ci --cache .npm-cache` 覆盖
- 本地复现同一条流水线：`python -m pytest -q`，以及 `cd frontend && npx tsc --noEmit && npm run test`

## 测试

```bash
.venv\Scripts\python.exe -m pytest -q
```

> 必须用 `python -m pytest`（而不是裸 `pytest`）：测试通过包名 `agent.*` 导入，
> 需要仓库根目录在 `sys.path` 上，`python -m` 会把当前目录加进去。

当前全量：**397 passed / 2 skipped**（2 条 skip 是本机无法创建**文件**符号链接；
目录场景已能用 junction 回退，见第 18 条）。
后端用例：`tests/test_backend_service.py` 覆盖工作区 / 知识库 / 自定义模型 / 用量统计 / 审批接口；
本轮新增上下文管理 4 条（工具参数压缩、80% 摘要替换、摘要失败回退、1M 窗口识别），
以及此前的非法 JSON 本地拦截 / 审批修正，总数从 321 增至 328。
前端：40 个测试文件 / 219 passed + 6 skipped（含审批卡片、审批+流式稳定性、错误边界、模型菜单、状态栏、图片压缩、Vision UI 与会话专属知识库用例）。

### 检索质量评测（golden set）

功能测试只能证明「检索有结果」，证明不了「结果对」。`tests/test_retrieval_eval.py`
用固定语料（`tests/fixtures/retrieval_corpus/`，6 份 txt）+ 21 条 golden QA
（`tests/fixtures/retrieval_golden.json`）把检索质量钉住，走纯 BM25 路径，
不联网、不调模型，结果可复现。

同时输出四项指标，口径不同：

| 指标 | 含义 | 当前基线 |
|---|---|---|
| Recall@4 | 期望文档是否出现在前 4 条 | **95.2%** |
| P@1 | 排第一的文档是否就是期望的 | **95.2%** |
| MRR | 期望文档首次出现位置的倒数均值 | **0.952** |
| 证据命中率 | 命中文档的片段里是否含关键证据串 | **100%** |

看报告：`python -m pytest tests/test_retrieval_eval.py -q -s`

三条纪律：

- 阈值设为**略低于实测值**（留约 1 条用例余量）。要抬阈值，先把指标做上去，不要为绿而调数字。
- 未命中的用例必须登记进 `tests/retrieval_eval.py` 的 `KNOWN_WEAKNESSES` 并写明原因，
  否则用例会红 —— 不允许「悄悄变差还继续绿」。
- 已知短板若被修好，`test_known_weakness_still_fails` 会反向报警，提示删除登记并上调阈值。

当前已知短板：`vpn-device-lost`（查询说「笔记本丢了」、语料写「设备丢失」，判别性词零重叠，
纯 BM25 无解，需同义词扩展或向量召回）。修法属功能改动，不与评测提交混做。


---

## 桌面打包

前置：venv 里要有 PyInstaller（它不在 `requirements.txt` 里，属于打包专用依赖）：

```powershell
.venv\Scripts\python.exe -m pip install pyinstaller
```

脚本用 `python -m PyInstaller` 调用，所以**不依赖** `Scripts\pyinstaller.exe` 是否存在
（用 `pip --target` 之类的装法会把启动器放到 `site-packages\bin`）。

```powershell
# 默认同时构建 Personal + Distribution
powershell -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1

# 单独构建
powershell -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1 -Variant Personal
powershell -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1 -Variant Distribution

# 只准备后端资源，不跑 NSIS
powershell -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1 -SkipInstaller
```

两个安装包：

- `Nexus 智枢_1.2.2_x64-personal-setup.exe`：包含本机 `.env`、`config.yaml` 和完整 `.venv`，`run_python_code` 使用 venv；只供自己用。
- `Nexus 智枢_1.2.2_x64-distribution-setup.exe`：使用 `config.example.yaml` + 空 `.env` 模板 + 便携 Python，不含 venv，适合分发。

产物：

- 后端 onedir：`packaging/dist/nexus-backend/`
- Tauri 资源：`frontend/src-tauri/resources/backend/`
- NSIS：`frontend/src-tauri/target/release/bundle/nsis/`

注意：

- **版本号只有一个来源**：`frontend/src-tauri/tauri.conf.json`。`scripts/build-desktop.ps1`
  会读它拼安装包文件名，`frontend/package.json`、`Cargo.toml`、`agent/__init__.py`、
  `backend/main.py` 里的版本要一起改（升级版本时容易漏，见「已知待办」）。
- `packaging/python-embed/` 是便携 Python，首次构建会自动下载；不要提交进 Git。
- Personal 包含本机 venv 与真实密钥，**绝不能外发**；需要分享时构建 Distribution。
- Personal 的 venv 路径绑定本机 `D:\python3.11`；换机器请用 Distribution，或重新创建 venv 后打包。
- NSIS 需要 WebView2 Runtime；当前配置为 `webviewInstallMode: skip`，Windows 11 自带。
- 安装包未签名，SmartScreen 提示属于预期行为。
- 首次启动可能被 Defender 扫描拖慢，端口等待上限为 45 秒；启动失败日志在 `%LOCALAPPDATA%\Nexus\logs\desktop.log`。
- 不要把 `cargo build --release` 直接产出的 exe 复制到安装目录：Tauri CLI 的 `tauri build` 才会嵌入前端资源；直接 cargo 构建会走 `devUrl`，表现为 "无法访问网页"。

### 构建链路已知坑

这条链路（前端构建 → PyInstaller → Tauri → NSIS）环节多、每节都可能单独失败，先看这里再排查：

1. **`找不到 PyInstaller：.venv\Scripts\pyinstaller.exe`**
   PyInstaller 不是运行时依赖，`requirements.txt` 里没有。先
   `.venv\Scripts\python.exe -m pip install pyinstaller`。
   （脚本已改用 `python -m PyInstaller` 调用，不依赖那个启动器 exe 是否存在。）

2. **改了后端依赖后桌面版才报"不支持的文件类型"**
   PyInstaller 靠 `packaging/nexus-backend.spec` 的 `hiddenimports` 静态分析，
   新增解析器/可选依赖必须同步加进去（`docx` / `pypdf` / `lxml` 就是这么加上的），
   否则源码跑得好、装完就缺包。

3. **安装时报 `Error opening file for writing: ...\nexus-desktop.exe`**
   NSIS 用独占方式打开目标 exe，**任何瞬时占用都会失败**（应用没真正退出最常见，
   关窗口 ≠ 退出，`nexus-backend.exe` 常常还在跑）；杀软拦截 setup 覆盖 exe 也会如此。
   排查：`tasklist` 看两个进程是否退出 → 试着重命名该 exe（能改名说明没有锁）。
   反复失败就别原地重装，改为**手工替换**：旧文件改名留档 → 从
   `frontend/src-tauri/target/release/nexus-desktop.exe` 与
   `frontend/src-tauri/resources/backend/` 复制进安装目录。数据在
   `%APPDATA%\com.stellar.nexus`，与安装目录分离，不会受影响。
   注意：只删安装目录而**不清卸载注册表项**，新安装包仍会弹「已安装」，
   且它要调用的 `uninstall.exe` 已不存在 → 死循环。

4. **想跳过 `npm run build` 里的 tsc**
   `tauri build --no-bundle --config <覆盖文件>`，覆盖文件写
   `{"build":{"beforeBuildCommand":""}}`，前端 dist 用 vite 单独产出。

5. **怎么确认装进去的是不是新前端**
   Tauri 会把 `dist/assets/index-<hash>.js` 的文件名写进 exe，直接对该 exe 扫字节：
   `data.count(b'index-B2FKVPPZ')` 命中即说明内嵌的是这一版。
   对裸 exe 有效；NSIS 安装包是 LZMA 压缩，扫不到属正常。

## 交付清单

验收标准第 7 条要求 README 覆盖安装、配置、用法与扩展方式，逐项对照：

- [x] **安装步骤**：`## 安装`
- [x] **配置说明**：`## 配置`（字段语义、加载优先级、多 provider 示例、加载规则）
- [x] **三种用法示例**：`run "问题"` / `chat` / `sessions resume <id>`，均在 `## 运行`，附真实终端输出
- [x] **如何新增一个模型**：`### 如何新增一个模型`（Moonshot 示例 + `.env` 步骤 + 真实 `config check` 输出）
- [x] **如何新增一个工具**：`## 如何新增一个工具`（`get_time` 示例 + 5 步改动 + 真实 `tools list` / `run` 输出）
- [x] **「设计决策」章节**：`## 设计决策`（72 条，含 Web 阶段的 34–72）

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
- [x] **会话专属知识库**：面板上传落全局、输入框 📎 上传绑定当前会话；检索只覆盖
  「本会话 + 全局」；system prompt 分段列出两类文档，模糊提问不再反问（第 72 条）

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
