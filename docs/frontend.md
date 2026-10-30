# 前端实现

> 返回 [README](../README.md)

## 历史消息怎么渲染（对齐第 21 条）

多轮工具调用时，落库的 assistant 消息有三种形态，前端按这个规则渲染：

| 落库形态 | 例子 | 渲染 |
|---|---|---|
| 只有文字 | `content="C：最终答案"`，无 `tool_calls` | 普通 AI 气泡（Markdown） |
| 只有工具调用 | `content=None`，有 `tool_calls` | 不渲染空气泡，只渲染工具卡片 |
| 文字 + 工具调用 | `content="A：我先看看时间。"`，有 `tool_calls` | 先渲染文字气泡，再渲染工具卡片 |

`role="tool"` 的消息**不单独气泡显示**：按 `tool_call_id` 配对到对应工具卡片，作为卡片的「返回结果」。

实测一轮 `A→B→C` 的落库结果（`GET /api/sessions/{id}` 共 7 条）：
`system` / `user` / `assistant("A：我先看看时间。" + call_a)` / `tool(call_a)` /
`assistant("B：再看一次时间。" + call_b)` / `tool(call_b)` / `assistant("C：最终答案…")`。

**链接一律新标签页打开**：AI 消息是 Markdown，正文里的 `a` 标签（包括 `web_search` 返回的
搜索结果链接、Markdown 语法链接与 `autolink` 裸链接）统一走自定义 `a` 渲染器，强制
`target="_blank" rel="noopener noreferrer"` —— 否则点一下搜索结果就会把当前对话页面覆盖掉，
而且 `noopener` 顺带挡住新页面对 `window.opener` 的反向操控。
用户消息不经过 Markdown，因此不会把文字里的 URL 变成可点链接（保持原样显示）。
A、B 的 `content` 是**文字**（模型确实返回了文字），不是 `None` 或空串。

## 前端

```bash
cd frontend
npm install          # 首次
npm run dev          # http://localhost:5173
npm run test         # vitest（jsdom），只跑 src/**/*.test.tsx
```

技术栈：React 19 + TypeScript + Vite 8 + **Tailwind 3.4**（`tailwind.config.js` + `postcss.config.js` + `index.css` 三条 `@tailwind` 指令）
+ Zustand（状态）+ react-markdown / remark-gfm（Markdown）+ lucide-react（图标）。

**布局**（侧边栏 + 主区；窄屏 `<768px` 侧边栏隐藏）：

```text
┌──────────────────┬──────────────────────────────────────────┐
│ 侧边栏 268px      │ 主区 flex-1（内容 max-w-3xl 居中）        │
│ · 品牌（PRO 标签）│ · 错误 banner（有错才出现，可关闭）        │
│ · ＋新建会话      │ · 消息流（滚动）：                        │
│ · 历史会话列表    │    用户气泡 右对齐 / AI 气泡 左对齐        │
│   （标题/时间/    │    工具卡片 可折叠（名+参数摘要→展开）      │
│    条数/tokens）  │ · 底部输入卡片 sticky：                   │
│ · 知识库（折叠）  │    输入区 / 📎🛡 工具行 + 模型选择器 + 发送 │
└──────────────────┴─   底部：快捷键 · Token 统计 · 免责声明───┘
```

**状态管理（Zustand）**：`sessionId / model / sessions / messages / streaming / error / draft`。
`appendDelta(delta)` 收到一个增量就追加到当前 assistant 消息并触发重渲染（**不缓存到 message_end**，见第 34 条）。
前端 store 只是**内存缓存**，刷新即丢；重新打开靠 `GET /api/sessions/{id}` 拉回。

**SSE 消费（模块 4）**：`POST /api/chat/stream` 用 **`fetch` + `ReadableStream`** 手写解析
（不用原生 `EventSource` —— 它只支持 GET、不能带 body）。`src/lib/sse.ts` 里：

- 用一个 `buffer` 字符串拼接每次 `reader.read()` 的内容，按 `\n\n` 切帧 —— 一个 JSON 被切在两次
  read 之间也不会解析失败；
- 单帧 JSON 解析失败只丢弃该帧，不中断整条流；
- 事件映射：`message_start`→建 assistant 占位并 `streaming:true`、`tool_call_start`→加 `pending` 卡片、
  `tool_call_end`→按 `tool_call_id` 更新为 `success`/`error`、`text_delta`→`appendDelta` 增量追加、
  `message_end`→补 `usage` 并 `streaming:false`、`error`→ErrorBanner。

**防竞态**：`sendMessage` 用 `try/finally` 兜底 `finishMessage()`，无论成功、后端 500、网络断还是流中途断，
输入框都不会永久禁用；发送期间 `streaming` 为 true 时输入框禁用、按钮变灰。

**集成测试（打真后端）**：`src/__tests__/integration.live.test.tsx` 会真连后端（默认 8000，
可用 `VITE_TEST_BACKEND` 覆盖），驱动真实的 `streamChat` + store + DOM 渲染。

**它们有三重保险，避免误花真实 API 的钱**：后端在线 **且** 假上游在线 **且**
后端 `/api/models` 里的 `base_url` 确实指向假上游（`127.0.0.1:8128`）——三者缺一就整体 skip。

```bash
# 1) 假上游（simple / missing / multi / boom / prompt 五种模式，POST /control 切换）
python tools/fake_upstream.py 8128

# 2) 用集成配置起后端（已在仓库里，指向假上游，用独立的库）
set AGENT_CONFIG=tools/config.integration.yaml
set FAKE_API_KEY=any
.venv\Scripts\python.exe -m uvicorn backend.main:app --port 8000

# 3) 跑测试
cd frontend && npm run test
```

后端起不来的话也可以起在别的端口（`--port 8010`），测试改成
`set VITE_TEST_BACKEND=http://127.0.0.1:8010`。

**会话管理（模块 5）**：

- 点击历史会话 → `GET /api/sessions/{id}` → 用纯函数 `reconstructMessages()`（`src/lib/reconstruct.ts`）
  把 OpenAI 原生格式（`assistant(tool_calls)` + 其后的 `role="tool"`）合并成「气泡 + 工具卡片」；
  选中项高亮（`aria-current`）。正在生成时点击只提示「请稍候」，不切换。
- 标题：后端 `title` 为空时用该会话第一条 user 消息的前 15 字，连 user 消息都没有则显示「新会话」。
- 时间：`刚刚` / `N 分钟前` / `今天 HH:MM` / `昨天 HH:MM` / `MM-DD HH:MM`。
- 列表静默刷新：新建会话后、以及每轮对话结束（`message_end` 之后）各拉一次 `GET /api/sessions`；
  失败不打扰用户。新建会话会真的 `POST /api/sessions`，清空主区并高亮新会话。
- **系统提示词设置**：侧边栏右上角齿轮 → 弹窗预填当前会话的 system 消息 → 保存走
  `PATCH /api/sessions/{id}/system_prompt` → 绿色提示条「已更新，下一句对话生效」。
  齿轮任何时候都能点：还没有会话时，保存会先自动建一个会话再写提示词。
- **重命名 / 删除**：鼠标悬浮会话项，右侧出现铅笔与垃圾桶。重命名是**就地编辑**
  （Enter 或失焦保存、Esc 取消）；删除**先弹二次确认**（显示标题与消息条数，
  正在生成时会额外提醒），确认后才 `DELETE`。删掉的若是当前会话，主区回到空状态。
- **动态模型配置**：输入框右下角的模型选择器（灰字 + 下拉箭头，向上弹出）旁边是极简「＋」，
  打开「模型管理」——内置模型只读、自定义模型可删
  （显示脱敏 Key 如 `sk-****abcd`）、底部表单可添加任意 OpenAI 兼容端点。
  添加后自动切到新模型；删除当前选中的模型会退回默认模型。
  **正在回答时**模型选择器禁用、面板里保存/删除也禁用（避免把 SSE 流拦腰截断）。
- **自动起标题**：首轮对话结束（`message_end`）后，前端**不 await** 地调一次
  `POST /api/sessions/{id}/generate_title`；后端用一次短超时（15s）、不重试的非流式调用，
  只取前 4 条消息生成「≤10 字」标题，清洗掉前缀/空格/标点后写库。失败则保留原来的
  「首条消息截断」兜底，**不弹错、不阻塞对话**（第 45 条）。

**代码高亮**：Markdown 里的围栏代码块由 `src/components/CodeBlock.tsx` 渲染 ——
深色底 + 行号 + 语法高亮（`react-syntax-highlighter` 的 PrismLight，只注册常用语言，
未注册的降级为纯文本）+ 横向滚动（长行不撑爆气泡）+ 顶部「复制代码」。
行内 `code` 仍是行内样式；没有语言标识但有换行的代码也按代码块渲染。

**本机环境的三个必要绕行**（都不影响别人 clone 后正常用）：

1. `.npmrc` 把 npm 缓存重定向到仓库内（`cache=D:\MyAi\.npm-cache`）：本机沙箱不允许写
   `C:\Users\...\npm-cache`，否则 `npm install` 直接 EPERM。注意 npm **只读当前目录的 `.npmrc`**，
   所以 `frontend/.npmrc` 也有一份。
2. **新增了含新 Tailwind 类的组件后，如果样式没生效，重启 `npm run dev`**：
   Tailwind v3 的 JIT 只在 CSS 被重新处理时才重新扫描 `content` 里的文件；
   某个组件新增了从没用过的类（比如 `bg-rose-600`），CSS 里可能还没有这条规则，
   表现是"元素在 DOM 里但看不见"（白底白字）。实测踩过一次：删除确认框的"删除"按钮不可见。
   判断办法：`curl http://localhost:5173/src/index.css | findstr bg-rose-600`。
3. `frontend/scripts/patch-child-process.mjs`：Vite 启动时会 `exec("net use")` 探测网络驱动器，
   本机沙箱禁止 spawn（EPERM）且 Vite 没有 catch → 直接把 `child_process.exec` 换成「回调一个错误」，
   Vite 会退回保守的 `fs.realpathSync`。npm scripts 里用 `node --import` 挂着它。
4. `vitest.config.ts` 里 `pool: 'threads'`：默认的 `forks` 池要 spawn 子进程，同样 EPERM；
   `threads` 用 worker_threads，不 spawn。

<details>
<summary><b>交付清单</b>（点击展开）</summary>
