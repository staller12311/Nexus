# 设计决策（68 条）

> 返回 [README](../README.md)

本任务未规定或存在歧义的地方，一律取最简单可行方案，记录如下。

**1. 所有相对路径以「配置文件所在目录」为基准**

`workspace` / `db_path` / `logging.file` / `system_prompt_file` 这些相对路径
解析基准是**配置文件所在目录**，不是当前工作目录。这样无论从哪个目录启动
（`cd D:\MyAi` 还是 `cd C:\`），指向的都是同一套 workspace / 数据库 / 日志，行为可复现。
`AppConfig.resolve_path()` 是唯一的解析入口。

**2. 配置中的未知字段直接报错**

pydantic 模型使用 `extra="forbid"`。像 `max_tool_round`（少写一个 s）这类拼写错误
会立刻报错并指出字段，而不是被静默忽略、然后在运行时表现为「配置没生效」。

**3. 这些字段的默认值**

出于「缺失即报错，多余即报错」的取向，只有 `default_model`、`models` 及其内部的
`base_url` / `model` / `api_key_env` 是必填；其余都给了与 `config.example.yaml`
一致的默认值，便于最小配置先跑起来。

**4. P0 阶段固定非流式（`stream: false`）**

`agent.stream` 只接受 `false`，配成 `true` 会校验失败。理由：

- 工具调用循环要在「本轮是工具调用还是最终回答」上做判断，流式下需要额外拼接
  `tool_calls` 分片、处理增量 delta 与中断，是最容易出隐蔽 bug 的地方；
- 非流式下一次请求拿到完整 `choices[0].message`，工具调用、`finish_reason`、
  `usage` 全部可直接判定，便于测试与排错；
- `usage`（token 统计）在非流式响应里是完整可信的。

rich 只负责把**已完整拿到的回复分块打印**（按段落/换行输出），不引入 `stream=True`。
后续若要支持流式，应作为独立特性整体引入，而不是让主循环同时兼容两种形态。

> **已过时（见第 48 条）**：这条是 P0 的阶段性取舍。第 48 条按「独立特性整体引入」的方式
> 完成了真流式：新增 `chat_stream()` / `run_stream()`，`run()` 改为收集流式结果，
> 主循环不再同时兼容两种形态。`agent.stream` 字段已随之**删除**（配置里再写 `stream`
> 会按未知字段报错，见第 3 条的 `extra="forbid"`）。

**5. 密钥只以「环境变量名」出现在配置里**

配置里写的是 `api_key_env: DEEPSEEK_API_KEY`，真实值只存在于 `.env` 或系统环境变量。
`ModelConfig` 不保存密钥值；`api_key()` 每次现读，缺失时报错只提变量名、绝不回显内容。
日志层另有脱敏 Formatter（见第 9 条）。

**6. python-dotenv 只在真有 `.env` 时才是硬依赖**

`agent/config.py` 对 `dotenv` 做受保护导入。没有 `.env` 时用真实环境变量正常跑；
**存在 `.env` 但没装 python-dotenv 时直接报错**并提示安装，绝不静默忽略用户写的 `.env`。

**7. openai 3.x 的 HTTP 栈是 httpx2，respx 对它无效**

本环境实测（`openai 3.37.0` / `httpx2 2.16.0`）：

- `openai._base_client` 使用 `httpx2`，`OpenAI(base_url=..., http_client=...)` 的类型就是 `httpx2.URL` / `httpx2.Client`；`pip show openai` → `Requires: anyio, httpx2, jiter, pydantic, sniffio, typing-extensions`。
- `respx 0.23.1` 的 `router` / `models` / `handlers` 源码里只出现 `import httpx`，**没有 httpx2**。
  实测在 `respx.mock()` 下调用 openai：请求真的发出网，抛
  `APIConnectionError`，`__cause__` 为 `ConnectError('[Errno 11001] getaddrinfo failed')`。
- `respx2` 并未安装。

因此：**不用 respx 测试 openai 请求**。阶段 3 的验证策略是

1. 主验证 = 标准库 `http.server` 起真实端点，把 `base_url` 指过去，跑完整对话并解析 `usage`（端到端，见 `tests/test_mock_server.py`）；
2. 重试/错误分类 = 把 HTTP 调用抽象成可替换的 `provider._raw_call`，用纯 mock 测外层退避逻辑。

已移除 respx，测试不依赖任何 HTTP 拦截库。

**8. TLS 走系统信任存储**

`pip show httpx2` → `Requires: ..., truststore, ...`。httpx2 用 Python 标准库 `ssl`
配合 **truststore** 读取操作系统信任存储（Windows 证书管理器 / macOS Keychain /
Linux 系统 CA），因此企业自签根证书、公司代理签发的证书无需额外配置即可被信任，
不依赖 certifi 打包的 CA 列表。

**9. 日志脱敏**

`agent/logging_setup.py` 用自定义 Formatter 对**最终格式化后的整行**做脱敏：
凡是环境变量名匹配 `api_key / token / secret / password / authorization` 的值，
以及形如 `sk-...`、`Bearer ...` 的字面量，都会被替换成 `***`。
在格式化后再脱敏，保证异常堆栈、`%` 参数拼接的内容也一并覆盖。

`force_utf8_stdio()` 把 stdout/stderr 切到 UTF-8：Windows 默认代码页（936）下
中文日志和中文报错会变成乱码，与其让用户手动设 `PYTHONIOENCODING`，不如在进程内解决。

**10. 持久化用标准库 sqlite3，消息按「OpenAI API 形状」存取**

不引入 ORM。`messages` 表按 `(session_id, seq)` 唯一，`tool_calls` 序列化为 JSON 文本，
读回来就是能直接扔给 OpenAI SDK 的 dict，避免在存储层和模型层之间来回翻译。
`get_messages(limit=n)` 返回的是**最近 n 条**且按时间升序——历史裁剪要的正是这个语义。

**11. 会话顺序用逻辑序号（`updated_seq`），不用墙钟**

最初用 `ORDER BY updated_at DESC` 排序，结果偶发失败。实测原因：**Windows 墙钟粒度约 15.6ms**
—— 连续调用 `datetime.now(timezone.utc)` 200 次只得到 **1 个不同取值**，同一批操作的时间戳完全相同，
排序于是退化成按随机十六进制 id 排序（先改成微秒也没用，因为分辨率在源头就不够）。

现在 `sessions` 表另有一列 `updated_seq`（递增逻辑序号），每次建会话 / 追加消息 / 重命名都会取
`MAX(updated_seq)+1`；排序一律 `ORDER BY updated_seq DESC, rowid DESC`。时间戳只用于展示。
旧库打开时自动 `ALTER TABLE` 补列并按 `rowid` 回填。

**12. 重试策略：`_raw_call` 是纯传输层，分类与退避在外层**

`Provider._raw_call()` 只负责发请求：成功原样返回 SDK 响应，失败原样抛 SDK 异常，
**不做任何分类**。分类（`translate_exception`）与退避（`_call_with_retry`）都在外层，
这样测试替换 `_raw_call` 时，喂进去的是真实 SDK 异常，走的是真实分类路径，不会绕过被测逻辑。

- **关闭 SDK 自带重试**（构造客户端时 `max_retries=0`）：重试完全由本模块控制，
  否则「重试了几次、等了多久」无法确定性断言。
- 分类按状态码硬分流：
  - `401/403` → `ProviderAuthError`，**不重试**立即抛（凭据问题重试无意义）；
  - `429` → `ProviderRateLimitError`，指数退避重试，有 `Retry-After` 时优先遵守（受 `backoff_max` 上限约束）；
  - `5xx` → `ProviderServerError`，指数退避重试；
  - `408` 与 `APITimeoutError` → `ProviderTimeoutError`（超时单独分类），退避重试；
  - 其余 `4xx`（400/404/422…）→ `ProviderRequestError`，不重试；
  - 响应结构异常 → `ProviderResponseError`，不重试（重试不会变好）。
- 退避公式 `base * 2^(attempt-1)`，封顶 `backoff_max`（默认 30s），默认叠加 jitter
  （取 `[delay/2, delay]` 区间）；`max_retries` 指**首次之外**的重试次数，
  `max_retries: 3` 即最多 4 次尝试。
- 非 SDK 异常（自己代码的 `TypeError` 等）原样抛出，不会被伪装成 provider 错误。

**13. 408 作为瞬态超时主动纳入重试（规格只列了 429/5xx）**

任务规格写的是「429/5xx 才退避」。实现上**主动把 HTTP 408 也纳入可重试**，并归类为
`ProviderTimeoutError`（与客户端侧 `APITimeoutError` 同类）：408 在语义上就是「请求超时」，
与 `APITimeoutError` 是同一种瞬态故障的不同表达，不重试反而不合理。如实记录这一处对规格的偏离。

408 的退避时长与 5xx **完全一致**：`_delay_for()` 只依赖尝试次数、`Retry-After` 与
`backoff_base/backoff_max`，不看错误类型，因此 408 与 500 在同一尝试次数下等待时间相同
（测试 `test_408_backoff_matches_5xx_backoff` 固定了这一点）。

**14. `run_shell` 默认禁用且默认空白名单；白名单只接受 PATH 中的真实可执行文件**

早期示例里写的 `allowed_commands: [dir, ls, pwd, cat, type]` 是错的：
`dir` / `type` 是 Windows `cmd` 的**内建命令**，`ls` / `cat` 在 Windows 上并不存在；
而我们用 `shell=False` 直接 `exec`，这些名字根本不经过 shell 解析，一律 `FileNotFoundError`。

因此：

- 默认 `allowed_commands: []`（空白名单 = 什么都跑不了），且 `run_shell` 不在
  `tools.enabled` 默认值里 —— 开箱即安全，要用必须先显式配置；
- 白名单只能填**PATH 中真实存在的可执行文件**（`git`、`python`、`rg` …）；
  **cmd 内建命令（`dir` / `type` / `copy` / `echo` …）不受支持**，不要填；
- 只接受裸命令名（不含路径分隔符），避免用 `C:\evil\git.exe` 绕过白名单。

`split_command()` 刻意不用 `shlex`：`posix=True` 会把 `C:\tmp\x.py` 的反斜杠当转义符吃掉，
`posix=False` 又会把引号留在参数里，两者在 Windows 上都有坑。这里只处理引号分组。

**15. workspace 越界校验：先 `resolve()` 再 `is_relative_to()`**

顺序不可颠倒：

```python
resolved = candidate.resolve()            # 展开 ..、跟随符号链接/目录联接
if not resolved.is_relative_to(self.root):  # root 也来自 AppConfig.resolve_path()
    raise WorkspaceViolation(...)
```

若先判断后 resolve，`workspace/link -> C:\Windows` 这类链接就能带着「看起来在内部」的路径过关。
workspace 根目录一律取 `AppConfig.resolve_path(agent.workspace)`（基准是配置文件所在目录），
**不是当前工作目录**。

Windows 上 `is_relative_to()` 做大小写归一，因此 `read_file("C:\WS\a.txt")` 与
`read_file("c:\ws\a.txt")` 视为同一文件且都不算越界（有专门用例固定这一行为）。

`list_dir()` 会对每个子项做同样的检查并标注「符号链接或目录联接指向 workspace 外」——
**文件和目录都要标**：目录联接最容易在列表里被忽略（该遗漏由测试暴露并修复）。

**16. `http_get` 白名单与重定向**

- 默认 `allowed_domains: []` → 拒绝一切请求；且 `http_get` 不在 `tools.enabled` 默认值里；
- 只允许 http/https 的 GET；
- 命中规则：host 完全相同或是白名单域名的子域（`api.example.com` 命中 `example.com`，
  `example.com.evil.com` **不**命中）；
- **重定向单独校验**：urllib 默认会自动跟随 302，等于给「白名单域名跳转到任意地址」开洞，
  因此自定义 `RedirectHandler` 对每个跳转目标重新过白名单；
- 用标准库 `urllib.request` 实现，不引入额外依赖。

**17. 工具 Schema 写死，参数在本地校验**

`registry.py` 为每个工具声明的 `parameters` 都满足：`type: object`、
每个属性都有明确的 `type` 与 `description`、`required` 显式列出（即使是空列表）、
`additionalProperties: false`。模型不需要猜类型，也无法悄悄多传字段。

调用前本地校验：JSON 解析失败 / 缺必填 / 类型不符 / 多传字段，都会返回一段**模型能照着修**
的错误文本（例如「参数不是合法 JSON，请重试。解析失败：… 请只输出一个 JSON 对象，
字符串要用双引号」+ 该工具的 schema）。工具抛 `ToolError`（越界、白名单拒绝…）同样转成
`错误：…` 文本回灌给模型，让模型自己纠正；只有工具内部的意外异常会记日志并提示模型别重复调用。

**18. 符号链接用例的环境差异（本机 2 条 skip 的原因）**

`tests/test_tools_security.py` 里与链接相关的用例：

- 「指向 workspace 外的**目录**链接」「`list_dir` 标注越界链接」「清理只摘链接」在本机 **PASSED** ——
  `os.symlink` 因权限失败后回退到 **Windows 目录联接（junction，`cmd /c mklink /J`）**，
  联接不需要开发者模式或管理员权限，而它同样是被 `Path.resolve()` 跟随的重解析点。
- 「指向 workspace 外的**文件**符号链接」「workspace 内的文件符号链接」在本机 **SKIPPED**，
  原因写在 skip 理由里：`os.symlink` 抛 `OSError: [WinError 1314] 客户端没有所需的特权`
  （Windows 创建符号链接需要开发者模式或管理员权限），且**文件**没有 junction 回退方案。
- **在 Linux / macOS 上这两条应当直接通过**：`os.symlink` 无需特权，`_make_symlink()` 会走
  第一分支创建真符号链接，两条用例即按预期执行（越界者被拒、内部者放行）。
  Windows 用户若开启「开发者模式」以管理员身份运行一次，或在 CI 里用 Linux，也能覆盖到。

清理链接一律用 `os.rmdir()` 只摘链接本身，不用 `rmtree` 递归 —— 见第 19 条。

**19. 链接清理用 `os.rmdir`，不依赖 `rmtree`**

测试里创建的链接全部位于 pytest 的 `tmp_path` 下，并由 autouse fixture 用 `os.rmdir()`
显式清理（`unlink_link()`：真符号链接走 `unlink`，目录联接走 `rmdir`）。

理由是实测发现的一个坑：**Python 3.11 的 `os.path.islink()` 对 junction 返回 `False`**
（`st_reparse_tag = 0xa0000003` 说明它确实是重解析点）。这意味着「靠 `islink` 判断要不要
递归」的逻辑会把 junction 当成普通目录。虽然本机实测 `shutil.rmtree` 恰好没有递归进目标
（目标文件完好），但这属于实现细节，不能依赖：`os.rmdir` 对目录联接的语义是明确的
——只移除重解析点，绝不会进目标。有一条用例专门固定这一点
（`test_link_cleanup_removes_only_the_link`）。

**20. 历史裁剪按「消息组」，不按条数**

OpenAI 要求每条 `role="tool"` 前面必须有对应的 `assistant`（带 `tool_calls`）。
若按条数从后往前保留 N 条，很容易把那个 assistant 切掉、留下孤立 tool 消息，下一轮请求直接 400。

`loop.trim_history()` 因此以**组**为单位取舍：`assistant(tool_calls)` 连同紧随其后的**全部**
兄弟 `tool` 消息算一组，同生共死；其余消息各自成组。规则：

- `system` 消息永远保留，且**不计入** `max_history_messages`；
- 只保留一个**连续尾部**：某组装不下就停止，不会为凑数保留更早的消息（那会在中间留洞，同样非法）；
- 孤立 tool 消息（没有前驱 assistant）永远非法，直接丢弃；
- 退化情形（最后一组本身就超限，例如 `max_history_messages` 配得极小）：**宁可略微超限也保留整组**，
  否则会发出只有 system 的空请求。

**21. assistant 消息原样回填；tool content 必须是字符串**

- 带 `tool_calls` 的 assistant 消息形如
  `{"role":"assistant","content":None,"tool_calls":[{"id":…,"type":"function","function":{"name":…,"arguments":<原始字符串>}}]}`：
  `content` 可以是 `None` 但**字段必须存在**；
- `arguments` 用模型返回的**原始字符串**，绝不解析再序列化 —— 重新序列化会改变空白与转义
  （`json.dumps(json.loads(raw))` 与 `raw` 并不相等，测试里有断言固定这一点），某些模型会因此行为异常；
- 参数解析只发生在**执行工具时**：`loop` 把原始字符串交给 `registry.call()`，由它做 JSON 解析、
  必填/类型/多余字段校验，失败则返回错误文本。`ToolCall.arguments_dict()` 供需要结构化参数的调用方使用；
- `tool` 消息的 `content` 必须是 `str`：`registry` 用 `_as_text()` 统一收口
  （dict/list → `json.dumps`，None → `""`），并有测试锁死。

上面两条约束各自都有**专用用例**，并且做过**变异验证**（把实现改错，确认用例真的会失败）：

| 约束 | 用例 | 变异实验 | 结果 |
|---|---|---|---|
| 多个 `tool_calls` 按 id 顺序回填、不串号 | `test_two_tool_calls_are_not_swapped_by_id`（id=a/b，两文件内容不同）<br>`test_full_loop_two_tool_calls_match_by_id`（真实 HTTP 链路，抓线上请求体） | `_execute_tool_calls` 改成反序遍历 | `assert ['b','a'] == ['a','b']` → FAILED |
| `arguments` 逐字符原样透传 | `test_arguments_are_passed_verbatim_to_next_request`（`'{"x": 1, "y":2}'`）<br>`test_full_loop_arguments_verbatim_on_the_wire` | `build_assistant_message` 改成 `json.dumps(json.loads(...))` | `assert '{"x": 1, "y": 2}' == '{"x": 1, "y":2}'` → FAILED（3 条用例同时捕获） |

**22. `max_tool_rounds` 的语义：拦住第 N+1 轮请求，而不是打断第 N 轮**

判定点在**循环顶部**：已经执行完 N 轮工具调用、正准备发起第 N+1 轮请求时发现超限 →
不再请求，返回 `LoopResult(tool_limit_reached=True)` 并给出「已达工具调用上限（N 轮）」。

于是退出时历史末尾一定是**已经回填好的 tool 消息**，它前面有对应的 `assistant(tool_calls)`，
序列合法。绝不出现「assistant 带了 tool_calls 但 tool 结果没回填」就退出。
这条提示只返回给调用方，不写进历史（否则会插入一条并非模型产出的 assistant 消息）。

**23. usage 逐轮累加并落库**

每轮 `Provider.chat()` 的 `usage` 都用 `Usage.__add__` 累加到本次 `run()` 的返回值，
同时**立即**通过 `Storage.add_usage()` 累加到会话记录（`sessions` 表的
`prompt_tokens / completion_tokens / total_tokens`，旧库自动 `ALTER TABLE` 补列）。
立即写而不是等结束再写，是为了 Ctrl+C 中断时账目不失真。

另一处相关细节：循环构造时插入的 `system` 消息，只在**会话还没有任何消息**时落库。
已有消息的会话不补写，否则 system 会被追加到 `seq` 末尾，破坏「system 在最前」的顺序；
这类会话每次构造循环都会在内存里补上 system，发给模型的内容依然正确。

**`sessions resume` 用哪个 system？—— 写死为「数据库里落库的那个」。**

- 会话创建时若还没有任何消息，会把当时的 system 消息落库；
- 之后 resume 时，历史里带着这条 system，循环**不会**用当前配置的 `system_prompt` /
  `system_prompt_file` 覆盖它；改配置只影响**新会话**。
- 理由：一个会话的历史必须和它自己的 system 保持一致。若 resume 时换成新提示词，
  同一段对话的前后轮次就建立在两套不同的指令上，模型行为会漂移，排查时也无法复现；
  而「换了提示词想生效」的正确做法是 `/new` 开新会话（或 `sessions resume` 前先改配置建新会话）。
- 边界：旧会话若**没有**落库的 system（例如更早版本建的库，或创建时没配 system），
  构造循环时会在内存里补上当前配置的 system —— 这种情况数据库不会被改写。

实测验证（同一份 DB，配置 A 的 system 是「提示词A」，用配置 B（system=「提示词B」）resume 旧会话）：

```console
$ python -m agent --config cfg_tool_a.yaml run 第一句      # 建会话，系统提示词 A
$ python -m agent --config cfg_tool_b.yaml sessions resume ba877c46
...
--- 假端点抓到的请求体 ---
最后一个请求的 system 消息: ['提示词A：我是配置里的系统提示词。']
是「提示词A」（数据库落库的）: True
是「提示词B」（当前配置的）: False
message 角色序列: ['system', 'user', 'assistant', 'tool', 'assistant', 'user']
```

**24. rich 只负责「已经完整拿到的回复」的分段打印**

CLI 打印的是 `run()` 收集到的**完整回复**（第 48 条起主循环内部走 `run_stream`，但 `run()`
只返回最终结果），rich 的职责只是渲染：

- 按空行把回复切成段落逐段打印，不做任何推断或改写；
- **模型产出的文本一律包成 `rich.text.Text`**，不做 markup 解析 —— 否则模型输出里的
  `[red]...[/red]`、`[not-a-tag]` 会被当成富文本标记（轻则变色，重则抛异常）；
  自己的提示文案才用 markup 上色。

**25. Ctrl+C 只中断当前请求，不退出程序（含一个真实进程才暴露的坑）**

- 提示符处中断：捕获后打印「已中断（不会退出）」，回到提示符；
- 请求进行中中断：`run_turn` 捕获后调用 `loop.abort_pending_tool_calls()`，
  把**没来得及执行**的 tool_call 补成显式的「已中断」结果再回到提示符 ——
  消息是边跑边落库的，不补的话历史会停在「assistant 带 tool_calls、tool 结果缺失」，
  下一次请求直接 400。中断时若还没拿到 assistant 消息（纯请求阶段），
  历史里就是两条连续的 user 消息，这是合法序列，不需要特殊处理；
- `/exit` 或 Ctrl+D（EOF）才退出。顶层未捕获的中断退出码 `130`。

**坑（真实子进程 + 控制台事件测出来的）**：Windows 上 Python 只为 SIGINT（Ctrl+C）装了
`default_int_handler`，**SIGBREAK（Ctrl+Break）没有**，而 CRT 对未处理的 SIGBREAK 默认动作是
**直接终止进程**。也就是说用户按 Ctrl+Break 会把整个 agent 打死，绕过全部 `KeyboardInterrupt`
处理。`cli.install_interrupt_handlers()` 显式把 SIGBREAK 接到同一个处理器上。
实测：修复前 `CTRL_BREAK_EVENT` 后子进程退出码非 0、进程已死；修复后进程存活、
打印「已中断」、提示符回来、后续提问正常、`/exit` 退出码 0。

这条行为有一条**真实子进程**用例守着（`test_ctrl_c_in_real_subprocess_keeps_prompt_alive`）：
它 spawn 一个真的 `python -m agent chat`，等假端点收到请求（确保中断落在请求进行中），
再投递真实中断事件。平台差异：Windows 用 `CTRL_BREAK_EVENT`（需要
`CREATE_NEW_PROCESS_GROUP` 与控制台），POSIX 用 `SIGINT` —— 因此**POSIX 上不会跳过**；
只有「Windows 但缺少 `signal.CTRL_BREAK_EVENT`」或「当前环境无法投递控制台事件」时才
`pytest.skip`，skip 理由会写明具体原因。

写这条用例时踩到的坑（值得记下来）：`TextIOWrapper.read(n)` 会**阻塞到读满 n 个字符或 EOF**，
而提示符 `你 > ` 没有换行、总量又小，于是输出要等子进程退出才交付 —— 看起来就像「子进程静默」。
正确做法是用二进制管道 + `read1()`（有多少返回多少）。

**26. 表格列宽是实测出来的，不是拼出来的**

`rich` 在内容总宽超过终端时会把列压扁：最初 `sessions list` 在 80 列终端下
**id 列整列消失**、`models list` 显示成 `mo…`。实测各布局的「表格最小宽度」后发现
`pad_edge=True` 与过宽的列是主因，最终定为：

- 所有表格 `pad_edge=False` + 固定列宽（`width=`）或显式上限（`max_width=` + `overflow="ellipsis"`）；
- `sessions list`：id 8 / 标题 ≤30 / 更新时间 16 / 消息 4 / tokens 7 → **实测最小宽度 79 ≤ 80**；
- `models list`：见第 31 条（`base_url` 移出表格，4 列全完整，实测最长行 78）；
- `updated_at` 只显示到分钟（19→16 字符，省 3 格），列名用 `tokens`（`total_tokens` 要 12 格，放不下）；
- 超长 `api_key_env` 在表格里会被省略号截断，**完整值看 `config check`**（那条是键值两列的布局，不截断）。

有回归用例固定「80 列下每行 ≤ 80、id 列不得消失」。**注意第 31 条的教训**：
列宽必须拿「最长的真实数据」测，短样例测不出 `no_wrap` 列的最小宽度问题。

**27. 退出码约定**

| 退出码 | 含义 |
|---|---|
| 0 | 成功（含 `config check` 通过、`/exit` 正常退出） |
| 1 | 配置错误 / 密钥缺失 / 模型调用失败 / 会话不存在 |
| 2 | argparse 用法错误（argparse 自带） |
| 130 | 被 Ctrl+C 中断（顶层未捕获时） |

`config check` 把「密钥是否已设置」也当作检查项：即使 `api_key_env` 未设置也会先打印完整配置表
（方便排查），随后把错误写到 **stderr** 并返回 1。

**28. 会话短 id：列表显示 8 位，show/resume 支持唯一前缀**

`sessions list` 的 id 列只显示 8 位（12 位放不下 80 列），但真实手测发现用户会直接把它复制给
`sessions show` —— 于是 `Storage.find_session()` 支持「完整 id 或唯一前缀」：精确命中优先，
前缀唯一则命中，多个则报「前缀不唯一」并列出候选，找不到则报「找不到会话」。
`chat --session <短id>` 与 `sessions resume <短id>` 同样适用。

**输入保护**：空串 / 纯空白**直接报错「请提供会话 id（不能为空）」，不进前缀匹配** ——
否则 `LIKE '' || '%'` 会匹配到所有会话，可能悄悄打开一个完全无关的会话。有空串用例固定这一点。

**29. `force_utf8_stdio()` 连 stdin 一起设置**

最初只设了 stdout/stderr。真实手测 `echo 你好 | python -m agent chat` 时中文变乱码：
**管道输入**时 Python 用本地代码页（936）解码，而真实终端走 PEP 528 本来就是 UTF-8。
现在 stdin/stdout/stderr 三者都显式切到 UTF-8。

**30. `--config` / `--model` 放在子命令前后都能用**

最初它们只挂在顶层解析器上，于是 `agent run "你好" --config x.yaml` 直接
`error: unrecognized arguments`（退出码 2）—— 用户按习惯把选项写在后面必然踩坑。
现在用一个公共父解析器（`parents=[common]`）挂到**每一级子命令**上：两个位置都可用，
子解析器默认值用 `SUPPRESS` 以免覆盖父级已解析到的值。有两条用例分别覆盖 `--config`
与 `--model` 的后置写法，并断言后置的配置**真的生效**（指向另一个库时会话列表为空）。

**31. `models list` 不把 `base_url` 放进表格**

第 26 条只测了短名字（`mock`/`alt`/`nokey`），于是漏掉了一个真问题：**`no_wrap` 列的
「最小宽度」等于它最长内容的宽度** —— 一旦有 `https://dashscope.aliyuncs.com/compatible-mode/v1`
这种 48 字符的端点留在表里，整表最小宽度被顶到 **103**，80 列终端下 rich 会一起压缩所有列，
结果名称、model、密钥全变成 `…`（实测输出：`名…` / `* …` / `密钥 …`）。

现在 `models list` 是 4 列（名称 12 / model ≤30 / api_key_env 18 / 密钥 8），
**base_url 移到表格下方的注释行**（长就自然换行，不影响表格）。用 4 个真实长名字实测：

- `moonshot-v1-8k`、`qwen2.5-72b-instruct`、`deepseek-chat`、`gpt-4o-mini-2024-07-18` 全部完整显示；
- `MOONSHOT_API_KEY` / `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` / `OPENROUTER_API_KEY` 全部完整；
- `已设置` / `未设置` 完整，**整份输出没有任何省略号**，最长行 **78 ≤ 80**。

教训：列宽不能只拿短样例测；要拿「最长的真实数据」测。

**32. 已知瑕疵：工具说明文字有两份**

`tools list` 的「说明」列取自 `agent/cli.py::render_tools_table()` 里的一张手写表，
而工具的**真实描述**在 `agent/registry.py` 的 `ToolSpec.description` 里 —— 新增工具要改两处，
忘了改就会出现「工具已启用、说明列空白」（`get_time` 首次演示时就踩到了）。

之所以没合并：`tools list` 需要显示**未启用**的工具，而未启用的工具不在 registry 实例里，
拿不到 `ToolSpec`。干净的修法是把「工具名 → 描述」抽成 registry 里的单一常量表，
`build_registry()` 与 CLI 都从它取；这属于结构调整，留到下次改动时做。

**33. `get_time` 默认启用是对原始规格默认值的**主动扩展****

原始规格给的 `tools.enabled` 默认值是 `[read_file, write_file, list_dir]`。
本实现把它扩成 `[read_file, write_file, list_dir, get_time]` —— 这是**主动扩展，不是规格要求**，
如实记录以免被误读成规格内容：

- **理由**：`get_time` 只读、无副作用（不碰文件、不联网、不起子进程），不需要任何配置或白名单；
  唯一失败模式是 `zone` 写错，且会走 `ToolError` 返回可读错误文本。默认启用能让
  「工具调用 → 结果回填 → 第二轮回答」这条链路开箱即可演示，也让它成为「新增工具」的活模板。
- **风险面**：模型能知道当前时间。对一个本地 CLI 助手来说没有隐私或安全影响。
- **关闭方式**：从 `config.yaml` 的 `tools.enabled` 里删掉 `get_time` 即可（代码留在
  `agent/tools/time.py` 不会被加载，也不会出现在 `tools list` 的启用项里）。
  若想连**代码默认值**一起改，改 `agent/config.py::ToolsConfig` 的 `default_factory` 那一行。
- **与规格的一致性**：`read_file` / `write_file` / `list_dir` 的默认启用没有变化；
  `run_shell` / `http_get` 依然默认不启用、默认空白名单（第 14、16 条）。

**34. `text_delta` 是「增量」语义，**现在已经是真流式逐 token****

前端**必须**增量消费：收到一个 `text_delta` 就把它 append 到当前 assistant 消息的
`content` 上并立即重渲染，**不要**攒着等 `message_end` 一次性显示。

这条在 P1 阶段是「先定协议、后端用 24 字符切块伪造」；到第 48 条真流式重构时，
后端换成 `provider.chat_stream()`（`stream=True`）逐 token 推送，事件结构与顺序不变，
**前端确实零改动**（只加了一个 `reasoning_delta` 分支）——当初的预判兑现了。

**35. SSE 断开的语义：跑完、落库、只丢事件流**

`POST /api/chat/stream` 里同步的 `service.chat()` 跑在 `asyncio.to_thread` 的工作线程里。
客户端断开（关标签页、刷新、断网）时：**SSE 生成器结束，但线程里的这一轮会继续跑完**，
消息与 usage **照样落库**（`loop` 边跑边 `append_message`，每轮 `add_usage`）。

也就是说「断开」丢的只是**实时事件流**，不是数据：用户重新打开该会话，能看到完整结果。
成本是这一轮的 token 照常花掉。第一版明确接受这个行为（不做取消/中断按钮）。
`anyio`/`asyncio.to_thread` 不提供杀掉线程的手段，这也与「不强杀正在执行工具」的取舍一致。

**36. 实时流只推最终回答（C），中间文本只在重载后可见**

一次多轮工具调用的形态是 `A(带 tool_calls) → 工具 → B(带 tool_calls) → 工具 → C(最终)`。
`LoopResult.content` 只有 C，所以 SSE 也只推 C 的 `text_delta`；A、B 的文字不进实时流，
**过程完全由工具卡片表达**（谁被调用、参数、结果、成功/失败）。

已知不一致（第一版接受）：A、B 仍以独立 assistant 消息**落库**，所以刷新或重新打开该会话时，
`GET /api/sessions/{id}` 返回的历史里能看到 A、B 的文字（各自带工具卡片）。
即「直播只推 C，回看能看到 A/B」。要消除它需要给循环再加一个「assistant 文本」回调，
属于对 `agent/` 的改动，不在本次范围。

**37. `/api/chat/stream` 用「1 个长连接 + 1 个短查询」，不为字面「单连接」牺牲 404 预检**

- **长连接**：整轮对话（含里面多轮工具调用）只有 1 个 `Storage`，在
  `AgentService.chat()` 的 `with self._storage()` 里创建、由同一个 `with` 的 `finally` 关闭；
  SSE 生成器只消费 `asyncio.Queue`，**不会**为每个 `text_delta` 开连接。
- **短查询**：路由在开始推流**之前**调一次 `service.get_session()` 做存在性校验，
  好让「未知会话」返回真正的 `404` 而不是 `200 + error 事件`。这是 1 个 ≤5ms 的独立连接，建完即关。
- 取舍：严格做到「一个请求一个连接」就得把校验挪进推流之后，代价是未知会话拿不到 404。
  这里选择保留 404 —— 5ms 的连接换前端清晰的错误语义，划算。
- 为什么不把路由里那个 `Storage` 传进工作线程复用：`agent/storage.py` 的 sqlite 连接是默认的
  `check_same_thread=True`，跨线程用会抛 `ProgrammingError`；要改就得动 `agent/`，而那是红线。

**38. 前端用 `fetch` + `ReadableStream` 消费 SSE，不用 `EventSource`**

`EventSource` 只支持 GET、不能带请求体，而我们的 `/api/chat/stream` 是 POST + JSON body，
所以手写解析：`response.body.getReader()` + `TextDecoder`，用 **buffer 字符串**拼接后按 `\n\n` 切帧。
必须这样做的原因很具体：一个事件的 JSON 完全可能被切在两次 `reader.read()` 之间
（`sse.test.ts` 里有「逐字节喂入」的极端用例），直接 `JSON.parse` 半截字符串会抛错。
坏帧（解析失败 / 没有 `type`）只丢弃该帧，不中断整条流。

`src/lib/base.ts::apiBase()` 默认返回空串 → 请求 `/api/xxx` 走 Vite proxy（业务代码里没有硬编码后端地址）；
只有集成测试用 `VITE_API_BASE` 指到真实后端。这条是集成测试逼出来的真实修复：
最初 `api/client.ts` 写死相对路径，在 Node 里直接 `Failed to parse URL`。

**39. `streaming` 状态的兜底复位**

`sendMessage` 的 `try/finally` 里无条件 `finishMessage()`：无论正常结束、后端 500、网络异常，
还是流中途断掉（没收到 `message_end`），`streaming` 一定会回到 false —— 否则输入框会永久禁用。
刷新页面后重新拉的会话里，没有拿到结果的工具卡片由 `lib/reconstruct.ts` 标成**失败**
（而不是永远转圈），因为「重新打开时它确实没有结果」。

**40. 历史消息还原：纯函数 `reconstructMessages()`，卡片默认 success**

后端存的是 OpenAI 原生格式：`assistant(tool_calls=[...])` 紧跟若干条 `role="tool"`。
`src/lib/reconstruct.ts::reconstructMessages()` 负责把它们合并成 UI 结构
（`tool` 消息不再单独成气泡，而是并入对应卡片的结果区）。它是**纯函数**，
`reconstruct.test.ts` 覆盖了：单工具、多轮 A→B→C、并行多 tool_calls、孤儿 tool 消息、
有 tool_calls 但无结果、空序列、id 唯一性。

**一处刻意的偏离**：需求写的是「历史里的 ToolCard 状态直接设为 success」，
实现改成「默认 success，**但结果文本以「错误：」开头时标 error**」。
理由：`agent/registry.py` 用统一前缀 `错误：` 表达工具失败，历史里如实存在这种记录；
把它显示成绿色的「成功」会掩盖失败。如果你要求严格全部 success，改
`reconstruct.ts` 里的那三行即可。

**41. 标题兜底的取数放后端，展示决策放前端**

「`title` 为空就用第一条 user 消息」需要那条第消息，而 `GET /api/sessions` 原本只返回会话元数据。
若让浏览器对每个会话再请求一次详情，就是 N+1。所以：后端在列表里补一个
`first_user_message`（只在该会话 `title` 为空时才去查，60 字截断），前端 `displayTitle()`
决定「取 15 字 + 省略号」还是「新会话」。这样**标题口径仍由前端决定**，但不产生额外请求。

注意 `agent/storage.py::get_messages(limit)` 取的是**最近** N 条，拿不到「第一条」，
所以取数用了一条只读 SQL（`backend/service/agent_service.py::_first_user_messages`）：

```sql
SELECT m.session_id, m.content FROM messages m
WHERE m.session_id IN (?, ...) AND m.role = 'user'
  AND m.seq = (SELECT MIN(x.seq) FROM messages x
               WHERE x.session_id = m.session_id AND x.role = 'user')
```

一条查询覆盖整页，命中索引 `idx_messages_session_seq(session_id, seq)`，**不随消息数增长**；
连接是只读的（`file:...?mode=ro`），库不存在或结构异常时返回空字典，不影响会话列表。
之所以不往 `agent/storage.py` 加「取前 N 条」的方法，是因为那是红线（`agent/` 零改动）；
如果将来允许改 `agent/`，这个查询应该下沉成 `Storage.first_user_messages()`。

**42. 系统提示词存在库里的那条 `system` 消息上，PATCH 直接改它**

`AgentLoop.__init__` 的行为是：**历史里已经有 `system` 消息时，就用历史里那条，不再用配置里的默认提示词**。
所以「让新提示词立刻生效」最直接的办法就是改库里那条 `system` 消息，
`PATCH /api/sessions/{id}/system_prompt` 就是这么做的：

1. `UPDATE messages SET content=? WHERE session_id=? AND role='system'`；
2. 一行都没改到（会话还没有 system 消息）且新提示词非空 → `INSERT` 一条，
   `seq = COALESCE(MIN(seq),1) - 1`（正常就是 0，插到最前面）；
3. **空串 + 本来就没有 system 行 → 什么都不做**。这一条很重要：若硬插一条空
   `system`，反而会把配置里的默认提示词「挡住」（循环只在没有 system 时才补默认）。

为什么不让前端"新建会话时带 prompt"：那样改提示词就必须换会话，不符合「当前会话立刻生效」。

**并发**：`agent/storage.py` 建库时本来就 `PRAGMA journal_mode = WAL`，所以读写可以并行；
后端写库时再显式设一次（幂等），用 `BEGIN IMMEDIATE` + 单条 UPDATE 快速提交，
连接 `timeout=10`，撞锁时等而不是立刻失败，不会打断正在跑的 loop。
验证脚本：`tools/verify_system_prompt.py`（UPDATE / INSERT / 空串 / 404 / 空串不动 五条路径 + `journal_mode` 实测）。

**43. 重命名与删除：复用 `Storage` 的方法，删除靠外键级联**

- `PATCH /api/sessions/{id}` 直接调 `Storage.rename_session()`（它顺带更新 `updated_at`/`updated_seq`，
  会话因此会跳到列表最前，符合"刚改过"的直觉）；标题会 `strip()`，**空串表示清空**，
  前端随后退回用首条 user 消息兜底显示。
- `DELETE /api/sessions/{id}` 调 `Storage.delete_session()`。消息不手工删：
  `messages.session_id` 上有 `REFERENCES sessions(id) ON DELETE CASCADE`，
  而 `Storage.__init__` 已经 `PRAGMA foreign_keys = ON`，所以级联生效
  （实测删除后该会话消息数从 3 变 0）。
- 前端删除**一定**经过二次确认弹窗；`streaming` 时额外提示"AI 正在回答，删除可能丢失这一轮结果"。
  删除的是当前会话时，同时清空 `messages` / `systemPrompt` / `sessionId`，避免主区显示已删会话的残影。

**44. 自定义模型：backend 自己一张表 + 继承 `ModelConfig` 组装 Provider**

**为什么不改 `agent/`**：`Provider.__init__(model_config, ...)` 只依赖 `ModelConfig` 的
`base_url / model / api_key() / temperature / max_tokens`。所以在 `backend/service/model_runtime.py`
里继承 `ModelConfig`、覆写 `api_key()` 让它读内存字段，就能构造出和「从 YAML 读出来的」等价的 Provider ——
`Provider.from_config()` 只是「读 YAML + 组装」，不用它即可。

**表**：`custom_models(name PK, base_url, model, api_key, created_at, updated_at)`，
建在同一个 `data/sessions.db` 里（不另建库），由 backend 用 `CREATE TABLE IF NOT EXISTS` 自己建。
`name` 不允许与 config.yaml 的内置模型重名（否则"同名到底走哪边"会产生歧义）；`base_url`
`rstrip("/")`（用户常多敲斜杠，SDK 拼路径会出现 `//`），并复用 `ModelConfig` 的 http(s) 校验。

**安全**：
- 明文存在本地 sqlite（与 `.env` 同一信任边界），**不做假加密**；
- 所有响应只给 `mask_key()` 的结果（`sk-****abcd`，长度 ≤8 全遮）；
- 组 Provider 时调 `register_secret(key)` 登记到日志脱敏器，配合 `RedactingFormatter` 双保险；
- 异常信息里不会带 Key（实测：假地址连接失败时 SSE 只回 `连接失败（将退避重试）：Connection error.`）；
- `Field(repr=False, exclude=True)` 防止 `DbModelConfig` 被打印/序列化时带出 Key。

**其它取舍**：自定义模型不填 `temperature/max_tokens/context_window`，用一组保守默认值
（0.7 / 2048 / 32768）；删除模型时若仍有会话指向它，返回 `referenced_sessions` 让前端提示。
前端会对 `api_key/base_url/name/model` 做 `trim()`（粘贴常带空格），
并在 `streaming` 时禁用模型下拉框与删除，避免 SSE 流被中途切断。

**45. 自动起标题：独立接口 + 短超时 + 静默降级，绝不阻塞主对话**

**为什么不放在 `chat/stream` 里做完**：主对话的 SSE 必须尽快收尾。标题是"锦上添花"的，
所以做成的是一次**独立请求**：前端在 `message_end` 之后 `void generateTitle(sessionId)`
（不 await），主流程早就 return 了。实测：主对话 359ms 结束，标题请求 41ms 之后单独完成。

**请求本身**：`POST /api/sessions/{id}/generate_title`
- 取该会话前 4 条 user/assistant 消息（每条截 200 字），配一句固定提示词
  「请用不超过 10 个字总结这段对话的主题作为标题，只输出标题，不要任何标点符号和解释。」
- **短超时 + 不重试**（`request_timeout=15, max_retries=0`）：失败就降级，不能让前端等；
- `clean_title()` 负责把模型的"不听话"清理掉：取第一行 → 去掉 `标题：`/`title:` 前缀 →
  去掉所有空白与标点 → 截断 20 字。实测 `"标题：Python 快排。「实现」"` → `Python快排实现`；
- **已有标题默认不覆盖**（`force=false`）：用户手动改过名、或之前已生成过的，直接返回
  `generated=false`，避免每次提问都改名；
- 消息不足一轮 → 400；模型调用失败 → **503**（前端静默吞掉，保留截断标题兜底）。

**前端降级**：`generateTitle` 里 `try/catch` 全吞，失败不碰 `error` 状态、不弹 ErrorBanner；
成功则就地更新列表标题并静默刷新。只在**首轮**（发送前 `messages.length === 0`）触发。

**46. 拉取模型列表：一次只读请求，Key 不落库；UI 成功才变下拉框**

`POST /api/models/fetch` 由**用户点击**触发，后端代发一次 `GET {base_url}/models`（`httpx`，10s 超时）：

- **地址拼接**（`backend/service/model_fetch.py::models_endpoint`）：`rstrip("/")` 后补 `/models`；
  输入已以 `/models` 结尾就原样用，避免拼成 `/models/models`。实测三种写法都落到同一个地址：
  `…/v1`、`…/v1/`、`…/v1/models` → `https://dashscope.aliyuncs.com/compatible-mode/v1/models`。
- **响应解析**兼容 `{"data":[{"id":...}]}`、`{"models":[...]}`、裸数组；按 `id/name/model` 取值并去重。
- **错误映射**：401/403 → 「密钥无效或无权限」、404 → 「该服务商没有 /models 接口，请手动填写」、
  其它 4xx/5xx → 带状态码；地址非法/为空 → **400**，上游失败 → **502**。
- **安全**：Key 只在这一个请求的内存里用一次，**不落库、不进日志**（日志只记 host 与数量）；
  实测 `data/agent.log` 里搜不到完整 Key，`custom_models` 行数也没有增加。
- **前端**：Model ID 旁的「获取模型列表」→ 成功则换成**可搜索的模型选择器**（见下），
  并提供「手动输入 Model ID」切回；失败只显示一条琥珀色提示，**输入框原样保留**，
  绝不卡死。改了 Base URL 会让已拉到的列表失效（避免选到另一个端点的模型 id）。

**47. 模型选择器：自己写可搜索下拉 + 过滤非纯文本模型 + 主流优先排序**

DashScope 的 `/models` 返回 **182 个**模型，其中 40 个是 audio / image / tts / vision /
realtime / embedding / speech / ocr，根本不能用于纯文本对话；剩下 142 个用原生 `<select>`
滚动找也很痛苦。所以：

- **不引 react-select**：自己写了 `components/ModelPicker.tsx`（输入框 + `role=listbox` 列表，
  ↑/↓ 移动、Enter 选中、Esc 收起、点外部收起），样式与现有 Tailwind 风格一致，不新增依赖。
- **过滤规则**（`lib/modelFilter.ts`，纯函数、有单测）：命中 `audio/image/tts/vision/realtime/
  embedding/speech/ocr` 任一关键字的 id 直接隐藏，并在列表顶部告知「已隐藏 N 个…」，不偷偷丢数据。
- **排序规则**：`qwen-max > qwen-plus > qwen-turbo > deepseek > gpt > kimi` 命中者排前；
  同一组内**精确名优先**（`qwen-max` 在 `qwen-max-1201` 前）；其余保持上游原顺序（sort 稳定）。
- **搜索**：输入即过滤（子串、大小写不敏感），与过滤规则叠加——搜索 `audio` 也不会把音频模型捞回来。
- 输入框**只显示搜索词**，不显示已选模型（选中态放在 placeholder「已选：xxx」与下方小字里）；
  清空搜索词立即恢复全部过滤后的模型。拉取成功后**不自动选中任何模型**，交给用户自己搜。
  （最初版本把选中值塞进输入框，看起来像"搜索框被自动填了个 cmaas-glm-5.3"，
  而展开时列表按空 query 过滤——输入框和列表不一致，已修正。）

用真实 182 个模型实测：**182 → 142 个可对话、隐藏 40 个**；
前 8 项为 `qwen-max | qwen-max-longcontext | qwen-max-1201 | qwen-max-0107 | qwen-plus |
qwen-plus-2025-12-01 | qwen-plus-2025-11-05 | qwen-plus-2025-01-25`；搜 `flash` 得到 28 个纯文本 flash 模型。

**48. 真流式 + 思维链（reasoning）：单一流式核心，`run()` 只是收集器**

**provider 层**（`agent/provider.py`）：
- `chat_stream()` 发 `stream=True`（并带 `stream_options.include_usage` 以拿到 usage），
  逐 chunk 解析 `choices[0].delta`：
  `reasoning_content` → `{"type":"reasoning"}`、`content` → `{"type":"text"}`，最后 `{"type":"done", content, reasoning, tool_calls, usage}`。
- **工具调用分片拼接**（最容易错的地方）：流式 delta 里每个 tool_call 只有 `index` 和
  碎片化的 `function.arguments`。必须按 `index` 建槽位，把 `id`（首个分片才有）、
  `name`、`arguments` **字符串拼接**起来，流结束后统一 yield 成完整 `ToolCall`。
  **绝不能对每个分片 json.loads**。实测两条并行调用、参数被切成 4 段且交错到达，
  仍能拼出 `{"path": "readme.txt"}` 与 `{"zone": "UTC"}`。
- **重试只在建连阶段**（`_open_stream_with_retry`）：流一旦开始吐数据就不再重试，
  否则会重复输出。
- 旧的 `chat()`（非流式）保留，供起标题这类"只要完整结果"的场景使用。

**loop 层**：`run_stream()` 是唯一核心，产出 `reasoning/text/notice/done` 事件；
`run()` 变成「消费 run_stream、返回 done 里的 LoopResult」，所以 CLI 自动走流式而**接口不变**。

**backend 层**：工作线程消费 `service.chat_stream()`，每个事件经 `EventBridge` 投递到
asyncio 队列 —— 原来"攒完整段再切 24 字符"的伪造逻辑（`chunk_text`）已整体删除。
新增 SSE `reasoning_delta`。

**前端**：`reasoning_delta` 追加到当前 assistant 消息的 `reasoning` 字段；
`MessageBubble` 顶部渲染灰色折叠面板：
- 思考中 → 自动展开，标题「深度思考中…」+ 跳动圆点，内容随增量和自动滚到底；
- 思考结束（`message_end` 把 `reasoningStreaming` 置 false）→ 自动收起为
  「已深度思考（N 字）」，点一下可回看；用户手动展开后不会被强行收起。

**取舍**：reasoning **不落库**（`agent/storage` 的 messages 表没有该列，加列会牵动
``normalize_message`` 与历史回放），所以刷新页面后思考过程消失，正文与工具卡片照旧。
想持久化的话下次给 messages 加一列即可。

真实厂商实测：`deepseek-reasoner` 69 个 reasoning 帧 / 217 字、47 个正文帧，
首字 1.2s、总耗时 2.0s；`qwen3.8-flash`（第三方兼容网关）同样返回 32 个 reasoning 帧 +
12 个正文帧。DashScope 的 `qwen-max` 不返回 `reasoning_content`，此时面板不出现，正文照常逐 token。

**49. 思维链持久化：单独一列、单独参数，绝不混进给模型的消息**

`messages` 表新增 `reasoning_content TEXT`（可空），旧库在 `Storage.__init__` 的 `_migrate()`
里用 `PRAGMA table_info` 检测后 `ALTER TABLE` 补列 —— 与 `sessions.updated_seq` 同一套做法，
**懒迁移**（第一次真正用到库时才执行）。实测 87 条历史消息在迁移后完好，重开库幂等。

三个关键取舍：

- **写入用独立参数**：`append_message(session_id, message, *, reasoning=None)`。
  reasoning 不塞进 message dict，因为 message 会进 `self._messages` → `request_messages()`
  发给模型，而 OpenAI 协议没有 `reasoning_content` 字段，严格校验的上游会 400。
- **读取用独立开关**：`get_messages(..., with_reasoning=False)`。agent 回放走默认（干净），
  只有 `GET /api/sessions/{id}` 传 `with_reasoning=True` 给前端回显。
  实测确认第二轮请求的 body 里搜不到 `reasoning_content`。
- **空值存 NULL**：空白 reasoning 不写空字符串，旧消息自然是 NULL，前端 reconstruct 时
  `.trim()` 为空就不渲染面板。

前端 `reconstructMessages()` 把 `reasoning_content` 映射成 `reasoning` + `reasoningStreaming: false`，
所以点历史会话时面板直接是「已深度思考（N 字）」的收起态，可点开回看。

**50. 消息操作：hover 出现，复制只复制正文，编辑回填输入框**

- 悬停（或键盘 focus）消息时出现操作区：`opacity-0 group-hover:opacity-100 focus-within:opacity-100`，
  默认不可见也不占视觉，键盘用户通过 focus 也能摸到。
- **AI 消息只复制 `content`**，明确不含思考过程（思考是内部推理，粘到别处通常是噪声）。
- 复制成功：图标变绿色对勾 + 显示「已复制」1.5 秒（够"一瞬间反馈"又不至于闪得看不见）；
  剪贴板不可用（`navigator.clipboard` 缺失或抛错）时**不显示假成功**，按钮保持原样。
- **编辑**只对用户消息开放：`store.editMessage()` 把内容写回 `draft` 并递增 `draftFocusSignal`；
  `Composer` 监听该信号 → `focus()` 并把光标移到末尾。刻意**不**做"发送即替换原消息"，
  因为那会牵扯历史重写与服务端截断，先按「快速引用上一次提问」来用。

**51. 本地知识库（RAG v1）：BM25 起步，向量接口预埋，不碰 agent/**

**依赖**：解析 PDF/Word 才需要 `pypdf` / `python-docx`（**用户自行安装**：
`pip install pypdf python-docx`）。缺库时 `GET /api/kb/status` 的 `supported` 里就不会出现
`.pdf/.docx`，前端拖拽区也只显示可用格式——不假装支持。`.txt/.md/.csv/.json/.py/.log`
开箱即用。

**流水线**：解析（PDF 逐页 / Word 按段落+表格）→ 切片（500 字符 + 100 重叠，优先在
换行/句号断开）→ 入库（`kb_docs` / `kb_chunks`，与 `custom_models` 同一 SQLite 文件，
backend 自己建表）→ 后台线程执行，`status` 走 `parsing → ready/failed`，前端轮询。

**检索**：v1 用 **BM25**（纯 Python，中文按双字 bigram、英文按整词）。
刻意**不保留中文单字 token**：实测「量子色动力学的渐近自由」会因「的/学」这类高频单字
微弱命中「报销制度」（单文档时 IDF 不够大），噪声淹没区分度；只留 bigram 后无关问题
正确返回空。`search()` 已同时实现余弦分支与 `0.7*向量 + 0.3*BM25` 混合公式，
**配好 embedding 就能切过去**（`KnowledgeStore(embedding_provider=...)`，
需一个带 `embed(texts)` 的 Provider 实例）。

**为什么 v1 不接向量**：实测你现有端点都不支持 `/embeddings`——DashScope 那个 Key 是 401，
`maas.qianwenaiapi.com` 网关对 `/embeddings` 直接 400「url error」（只代理 chat）。
所以先把 BM25 做成能用的闭环，等有有效 Key 再开向量，代码不用改结构。

**工具注入**：`search_knowledge_base(query, top_k=4)` 在 `AgentService.__init__` 里
`registry.register(...)` 注入，**`agent/` 完全不动**、`KNOWN_TOOLS` 也不改；
每次调用现开一个 SQLite 连接（不跨线程共享）。工具输出带来源文件名与页码、
总长上限 2000 字；无命中返回「知识库中没有找到相关内容」而不是抛错（模型不会卡住）。

**已验证的闭环**：上传 `报销制度.txt` → 提问「一线城市出差住宿每晚能报多少钱？」
→ 模型调 `search_knowledge_base({"query":"一线城市住宿标准每晚多少钱"})`
→ 工具返回含「600 元」与来源 `报销制度.txt（第 1 页）` → 回答里带上 600。
`GET /api/kb/status` 报 `mode=keyword`、`pipeline="BM25 关键词检索（中文双字 + 英文整词）"`。

**52. 让模型「知道」该调知识库：三层提示 + 输入框直接上传**

实测踩过：上传了 README.md 后问「解析一下说的是什么」，模型却调 `list_dir` / `read_file`
去 workspace 里翻，最后说没找到。根因是**模型不知道有知识库，也不知道上传的文件不在 workspace**。

三层修正（缺一层都会漏）：

1. **默认系统提示词**（`config.yaml` 的 `agent.system_prompt`）：明确「你拥有一个本地知识库，
   解析/总结/查找任何上传的文档时优先用 `search_knowledge_base`，不要凭空猜测；
   上传的文档在知识库里、不在 workspace 里，不要用 list_dir / read_file 猜路径」。
2. **工具 description 写清楚触发场景**：列了「解析/总结/查找/回答上传文档」「文档、资料、附件、
   报告、手册、说明书」这些词，并再次强调不要用文件工具猜路径 —— description 是模型唯一的
   选择依据，泛泛一句「检索知识库」不够。
3. **动态文档清单**（`AgentService._loop_config()`）：知识库非空时，把「当前已有文档：README.md、
   报销制度.pdf」拼到系统提示词末尾再构造 loop。会话自己设置过提示词的不覆盖（历史里已有
   system 消息时 `AgentLoop` 优先用历史的）。

**输入框上传**：`Composer` 左侧回形针（`accept` 来自 `/api/kb/status.supported`，装没装 pypdf
一样能正确显示），多选后每文件一张卡片渲染在聊天流里：上传中显示百分比进度条 → 「正在解析」
→ 「文档已加入知识库」，失败显示后端 detail；完成后可手动关闭。轮询沿用面板那套逻辑，
另加「连续 10 次查不到该文档就判定失败」，避免卡片永远转圈。

**真实模型实测**（deepseek，未做任何手工引导）：
```
上传 README.md（排班系统说明）→ status=ready chunks=1
新会话提问「这个文件里写了啥？简单说说它是干什么的。」
模型调用: search_knowledge_base 参数={"query": "README 文件内容 说明 用途", "top_k": 8}
回答: 「知识库里只有一份 README.md，内容是一份排班系统的项目说明（来源：README.md 第 1 页）：
      是什么 / 技术栈 Python 3.11 + FastAPI + SQLite、React 18 + TypeScript / 核心功能 / 部署…」
命中关键信息: 排班、FastAPI、React、SQLite、Docker   耗时 2670 ms
```
顺手修了 `ChatArea` 的 `scrollIntoView` —— jsdom 没实现它，之前没有测试真正挂载 ChatArea
所以一直没暴露；改成可选调用后，新测试能正常渲染聊天流。

**53. 联网搜索（Bing 主 / 百度兜底）与「中断 / 重新生成」**

**web_search 为什么不用 duckduckgo_search / Jina**：实测这台机器上 DuckDuckGo（含 `ddgs`
库的数据源）、Jina Reader、SearXNG、Wikipedia **全部超时**，只有 `cn.bing.com` 与
`www.baidu.com` 可达。所以「免费 + 免 Key」的现实解就是抓这两个的 HTML：
Bing 解析 `li.b_algo` 块（标题 / 真实直链 / 摘要），解析到 0 条自动换百度（`div.result`，
链接是 `baidu.com/link` 跳转链）。**零新增依赖**，用已装的 httpx。

配套加固：
- **域名多样性**：同域名最多 2 条，避免前几条全被官网/百科占满（实测 `上海 天气` 现在给出
  气象网 / 天气网 / 中央气象台 / 市气象局 四个不同来源）；
- 5 分钟结果缓存、12s 超时、`mkt=zh-CN`、单次最多 8 条、总注入 ≤2000 字；
- 失败时工具返回「错误：联网搜索失败…可以改用本地知识库或说明无法联网」，不抛异常；
- 系统提示词里划清边界：实时信息走 `web_search`，上传文档走 `search_knowledge_base`；
- description 教模型写 query：「用具体关键词，查官方文档可加 `site:`，通常一次调用就够」。

**已知局限（如实记录）**：Bing CN 对「某软件最新版本是多少」这类实体查询，排序偏向官网
首页与百科，摘要里常常没有确切数字（模型只能靠 docs.python.org 的默认版本号间接推断，
并在回答里明说「需要以官方页面为准」）。加 `site:python.org` 后能直接命中发布页。
想要稳定质量就上 Azure Bing Web Search API（有免费额度，但需要 Key）——接口留好了，
换 `_http_get` 的目标与解析即可。

**中断生成**：store 持有 `AbortController`（`sse.ts` 早就留了 `signal` 参数），发消息期间
「发送」按钮换成红色「停止」；`abort()` 后**保留已收到的内容**、消息标 `interrupted`、
操作区显示琥珀色「已中断」，且不写 `error`（中断不是错误）。诚实标注：前端 abort 只断开
SSE，**后端线程仍会跑完并落库**（第 35 条的既定语义），刷新会话会看到完整回答。

**重新生成**：`POST /api/sessions/{id}/regenerate`（SSE）—— `storage.delete_from_last_user()`
删掉「最后一条 user 及其之后」的全部消息（含 tool 消息），再把同一句话重新入历史跑一轮，
所以历史**不会重复**也不会留下孤立 tool 消息。前端「刷新」图标只出现在 AI 消息上，点击后
删掉该条 AI 及之后的 UI 消息再走同一条 SSE 渲染管线；流式期间按钮禁用，避免并发两条流。

**删除必须在一个事务里**（`agent/storage.py`）：显式 `BEGIN IMMEDIATE` 把「查最后一条 user」
与「整段删除」放进同一个写事务，异常整体 `ROLLBACK`。理由很实际 —— 如果删到一半崩溃，
历史里会留下「tool 结果找不到对应的 assistant tool_calls」这种半截状态，下一次请求上游直接
400，会话就废了。实测用 SQLite 触发器让 DELETE 必定失败：回滚后 7 条消息一条不少、
`in_transaction` 复位为 False、`PRAGMA integrity_check` 仍是 ok。

实测：`site:python.org 3.15` → 命中 `Python Release Python 3.15.2`；
真实模型问「Python 最新稳定版本」→ 主动调用 `web_search`（带 `site:` 限定）→ 回答附来源链接
并明确标注不确定性；`regenerate` 后消息数不变、user 消息仍只有 1 条、角色序列
`[system,user,assistant,tool,assistant]` 保持不变，且还能继续正常追问。

**54. 流式渲染性能：流式期间**关闭语法高亮** + memo + 50ms 合帧**

**症状**：输出到 100 行左右代码时浏览器「页面没有响应」。根因是每来一个 token，
`react-markdown` 都会重新解析整段（越来越长的）markdown，里面的
`react-syntax-highlighter` 再把整段代码重新分词、生成成百上千个 `<span>`。

**量化**（jsdom 里渲染同一段 200 条函数体的 Python，预热后取 3 次均值）：

| 场景 | 单次渲染耗时 |
| --- | --- |
| 流式中渲染纯 `<pre>` | **1.7 ms** |
| 完成后挂 Prism 高亮 | **1065 ms** |

差了 **623 倍**。而真实一轮长代码回复有 **2751 个 `text_delta` 帧**（实测 323 行 / 16.5s）——
如果每帧都高亮，就是 2751 次 × 秒级任务，必然假死。

**三管齐下**：

1. **流式期间关高亮**（`CodeBlock` 的 `streaming` 属性）：只渲染纯 `<pre><code>`，
   不挂 `SyntaxHighlighter`、不生成行号、不显示复制按钮；`message_end` 后
   `streaming` 变 false，那一帧才一次性挂上高亮（此时只算一次，1065ms 的成对成本可接受）。
2. **memo + components 缓存**：`MessageBubble`/`CodeBlock` 都用 `React.memo` 包裹；
   markdown 的 components 映射由 `useMemo` 按 `streaming` 缓存，避免每次 delta 都重建
   导致所有代码块被重新挂载。store 的不可变更新保证历史消息**对象引用不变** →
   浅比较直接跳过（有测试断言这一点）。
3. **50ms 合帧**（`chatStore.appendDelta`）：token 先进入缓冲，每 50ms（或积压到 2048 字）
   才 `set` 一次；`finishMessage` / `markInterrupted` / `reset` 都会先 `flushDeltas()`
   **强制冲刷**，所以不会丢尾字、也不会「等 message_end 才显示」。
   实测 2751 帧 → 约 330 次状态更新（8 倍减少），单次渲染从 1065ms 降到 1.7ms。

**品牌**：侧边栏顶部改为 `Nexus 智枢`（`Boxes` 图标 + 渐变蓝圆角底：
`bg-gradient-to-br from-sky-500 via-blue-600 to-indigo-600`），副标题
「你的私有化 AI 调度中枢」；`index.html` 的 `<title>` 改为 `Nexus 智枢 - 私有化 AI 中枢`。

**55. 「思考烧光预算、正文为空」不再 500：合成提示 + 暴露 finish_reason**

**线上事故**：用户问「写一个 200 行的 python 代码」，deepseek 把 4096 的 `max_tokens`
全用在思维链上（13383 字思考），正文一个 token 都没产出。于是这一轮构造出
`{"role": "assistant", "content": None}` 且无 `tool_calls`，被 `normalize_message`
拦下抛 `StorageError`，整轮变成一条红色 ErrorBanner：
「出错了：assistant 消息必须至少有 content 或 tool_calls」。

**根因有两层**：① 校验本身是对的（OpenAI 协议要求 assistant 至少有一个），错在**上层
构造了非法消息**；② `max_tokens` 对推理模型来说同时装着思考和正文，4096 根本不够。

**三层修复**：

1. **provider 暴露 `finish_reason`**：`chat_stream()` 在 `done` 事件里带上
   `finish_reason`（`stop` / `length` / `tool_calls`），上层才能区分「正常结束」与「被截断」。
2. **loop 兜底合成提示**（`_empty_answer_notice`）：当本轮既无正文又无 `tool_calls` 时
   - `finish_reason == "length"` → 「模型在思考阶段就用完了 max_tokens 配额（本轮思考了 N 字），
     没有产出正文。请调大该模型的 max_tokens…后重试。」
   - 其它 → 「模型这一轮没有返回正文。可以重试一次，或者换一个模型试试。」
   这段提示会**作为 `text_delta` 推给前端**（用户看到的是正文，不是报错），
   同时**作为 assistant.content 落库** —— 历史保持合法，下一轮还能继续聊。
   `storage` 的校验**没有放宽**：手写空 assistant 依旧被拒（有验证脚本断言）。
3. **抬高预算**：`config.yaml` 里 deepseek 的 `max_tokens` 4096 → **8192** 并加注释说明原因。

**真实复现验证**（同一句话、真实 deepseek）：

| | 修复前 | 修复后 |
| --- | --- | --- |
| error 事件 | 1（StorageError） | **0** |
| 正文 | 空 | **7985 字 / 264 行** |
| 思考 | 13383 字（烧光预算） | 5079 字 |
| 落库 | 只有 system + user | system + user + assistant（含 reasoning） |

连跑多次也稳定；如果某天 8192 仍被烧光，用户会看到那条人话提示而不是报错。

**56. 滚动跟随：用户在往上看时，流式内容绝不抢滚动条**

**症状**：AI 流式输出时，只要新字一蹦出来，页面就把用户强行拽回底部 —— 想往上翻着读
前面内容根本做不到。

**做法**（对齐 ChatGPT 的行为）：

- `ChatArea` 给消息容器加 `onScroll`，实时算
  `distanceToBottom = scrollHeight - scrollTop - clientHeight`；
  大于 **100px** 判定为「用户正在往上看」（`isUserScrolledUp = true`），
  滚回底部附近（≤100px）自动恢复 `false`。用 `ref` 镜像状态，避免 effect 拿到旧闭包。
- 流式增量（`messages` 每 50ms 更新一次）触发的自动滚动，**只有 `isUserScrolledUp === false`
  才执行**；否则什么都不做，页面停在用户看的位置。
- **例外**：用户自己发新消息时（判断依据是**用户消息条数变多** —— `sendMessage` 会同时插入
  user 消息与 assistant 占位，所以不能用「最后一条是 user」判断）无条件滚到底部并恢复跟随。
- 加分项：往上看时右下角浮出「👇 回到底部」按钮，点击 `scrollTo({ behavior: 'smooth' })`
  平滑归位并重置跟随状态。
- jsdom 没有 `scrollTo`，沿用可空调用兜底（和 `scrollIntoView` 一样的处理）。

**测试**（`ScrollFollow.test.tsx`，用 `Object.defineProperty` 伪造
`scrollHeight/scrollTop/clientHeight` 几何 + 记录 `scrollTo` 调用）：

| 场景 | 断言 |
| --- | --- |
| 贴底时流式更新 | 触发滚动，滚到 `scrollHeight` |
| 上滑 900px 后流式更新 | **一次滚动都不发生**，且「回到底部」按钮出现 |
| 距底 50px（贴底） | 继续自动跟随，不出按钮 |
| 点「回到底部」 | 调用 `scrollTo` 且 `behavior: 'smooth'`，按钮消失 |
| 用户发新消息（即使在上方看） | 强制滚到底部、按钮消失 |

**57. 沙箱徽章（纯前端）与「越界拦截」的真实性验证**

**UI**：输入框回形针右侧加了一枚「🛡 安全沙箱保护中」徽章（`ShieldCheck` + 淡绿胶囊），
纯前端展示、不改任何后端逻辑。`title` 与自定义 Tooltip 都写明：
> 为保护您的数据安全，AI 文件读写工具已被严格限制在当前的 workspace 目录内，无法越权访问系统文件。

**验证脚本**（`tools/verify_sandbox.py`，走的是模型真实调用路径 `registry.call()`）——
8 类越界手法全部被拦，且返回里**没有泄漏任何系统文件内容**：

| 攻击姿势 | 结果 |
| --- | --- |
| `read_file(C:\Windows\System32\drivers\etc\hosts)` | ✅ 拒绝（路径越出 workspace） |
| `read_file(../../../../Windows/win.ini)` | ✅ 拒绝 |
| `read_file(C:/Windows/win.ini)`（正斜杠混写） | ✅ 拒绝 |
| `read_file(\\?\C:\Windows\win.ini)`（扩展长度前缀） | ✅ 拒绝 |
| `read_file(\\localhost\C$\Windows\win.ini)`（UNC） | ✅ 拒绝 |
| `read_file(%WINDIR%/win.ini)`（不展开，按相对路径） | ✅ 拒绝（文件不存在） |
| `write_file(C:\Windows\evil.txt)` | ✅ 拒绝 |
| `list_dir(C:\Windows\System32\drivers\etc)` | ✅ 拒绝 |

对照组证明「不是无脑拒绝」：workspace 内 `read_file` / `write_file` / `list_dir` 全部正常；
`subdir/../readme.txt` 这类「绕出去又绕回来」的路径能正确归一化后放行 —— 说明
**先 `resolve()` 再 `is_relative_to()`** 的顺序是对的。

如实说明一处环境限制：脚本本想再验「workspace 内建 junction 指向 `C:\Windows`」这种最阴的
绕过方式，但本环境的沙箱禁止 `mklink /J`（Access is denied），所以该用例被跳过并打印原因；
代码侧对此的防护依据是 `Workspace.resolve()` 的注释与实现（`Path.resolve()` 会跟随链接，
之后才判边界）。

**58. 工作区切换（Workspace Switcher）：可配置的沙箱根目录**

沙箱原本固定在 `config.yaml` 的 `agent.workspace`。但「想让它帮我重构 D:\\MyProject」是真实需求，
所以把沙箱**根目录本身**做成可切换的 —— 注意切换的是边界位置，边界规则一点没松。

**后端**（`backend/service/workspace.py` + `AgentService`）：
- `POST /api/config/workspace {path}` / `GET` / `DELETE`（恢复默认）；
- 校验顺序：非空 → 拒绝 UNC → 必须绝对路径 → `resolve(strict=True)` 必须存在 → 必须是目录 →
  拒绝盘符根目录 → 拒绝用户主目录整体 → 拒绝系统核心目录整棵树 → 拒绝凭据/应用数据目录；
- 「系统核心目录」用环境变量动态取（`SYSTEMROOT`/`ProgramFiles`/`ProgramFiles(x86)`/`PROGRAMDATA`）
  + 兜底黑名单，判定用 `resolve()` 后的 `is_relative_to`，所以 `C:\Windows\System32\..\..\Windows`
  这种穿越写法同样被拦；
- 用户主目录策略：**整体拒绝**（一眼能看到所有个人文件），但允许它的**普通子目录**
  （如 `C:\Users\me\Documents\proj`）；`AppData` 与 `.ssh/.aws/.config/.gnupg/.kube/.docker`
  等一律拒绝（凭据）；
- 切换后重建工具注册表（`Workspace(root=...)` 绑在工具闭包里），所以**下一句话立即生效**，
  不需要重启；
- 选择持久化到 `app_settings` 表；重启后读回；若该目录后来被删除/改名，**自动回退默认**；
- AgentService 是单例，切换是进程级全局状态（对本地单人使用足够，README 里如实标注）。

**前端**：输入框旁的「安全沙箱保护中」徽章改成**按钮**，点开极简弹窗：
预填当前工作区、提示「当前 AI 将获得该目录的读写权限」、显示当前生效路径、
「应用 / 恢复默认 / 取消」，后端 400 的错误原样显示在弹窗里（不关窗）。
徽章 tooltip 里也带上当前工作区路径。

**验证**（`tools/verify_workspace.py` + `tools/verify_workspace_api.py` + `tools/verify_workspace_real.py`）：

| 用例 | 结果 |
| --- | --- |
| `C:\Windows` / `System32` / `SysWOW64` / `Program Files` / `ProgramData` | ✅ 拒绝（系统核心目录） |
| 盘符根目录 `D:\`、用户主目录整体、`AppData`、`.ssh` | ✅ 拒绝 |
| 相对路径、不存在的目录、指向文件、UNC、`..` 穿越 | ✅ 拒绝 |
| 切到 `D:\...\_ws_probe\MyProject` 后 read/write/list | ✅ 全部成功 |
| 切换后越界读 `C:\Windows\...\hosts` | ✅ 仍被拦（新边界之外） |
| 新进程（模拟重启）读回工作区 | ✅ 持久化生效 |
| 目录被删后重启 | ✅ 自动回退默认 |
| 真实模型：切到演示项目后问「读一下 hello_demo.py」 | ✅ 先 `list_dir` 再 `read_file`，回答正确 |
| 真实模型：再问「读 C:\Windows\...\hosts」 | ✅ 模型自己就拒绝了调用；直接调用工具仍返回「路径越出 workspace」 |

**59. 多根工作区：`@name` 命名挂载（跨目录协同，红线不动）**

**需求**：主工作区在 `D:\ProjectA`，但要读 `D:\SharedLibs` 里的依赖库 —— 单根做不到。

**设计**（方案 C，命名挂载）：主根保持原有相对路径语义；附加目录挂成 `@libs`，
模型用 `read_file("@libs/http/client.py")` 访问。比裸多根白名单好在：
① 路径可迁移（配置改了模型提示词不用变）；② 能表达**只读**；③ 不会和主根里的真实目录名混淆。

**红线（`is_relative_to`）为什么没破**：`Workspace` 从「一个 root」变成「主根 + 挂载列表」，
但闸门仍是同一个函数、同一条纪律 —— **先 `resolve()`（展开 `..` 与链接），再按命中的那个根做
`is_relative_to`**。三个工具函数 `read_file` / `write_file` / `list_dir` 的结构完全没动。

解析规则（`Workspace.resolve_with_mount`）：

| 输入 | 解析基准 | 边界判定 |
| --- | --- | --- |
| `@libs/x` | `D:\SharedLibs` | `is_relative_to(D:\SharedLibs)` |
| `x`（普通相对路径） | 主根（语义与单根版本一致） | `is_relative_to(主根)` |
| `D:\...`（绝对路径） | 原样 | 落在**某个**授权根内 |

因此：`@libs/../../Windows` → 先解析成 `D:\Windows` → 不属于 `@libs` → 拒绝；
相对路径 `../SharedLibs/x` 仍然越界（附加目录**只能**用 `@name` 进入，不能靠相对路径偷渡）。

**只读挂载**：`Mount(name, path, writable=False)`，默认只读；`write_file` 命中只读挂载直接报错。
`list_dir` 主根时把挂载作为虚拟条目列出来（`@libs/ -> D:\SharedLibs（只读）`），
`relative()` 输出带 `@name` 前缀，工具结果能看出文件来自哪个根。

**配置**：`config.yaml` 的 `agent.extra_workspaces: [{name, path, writable}]`；
运行时 `POST /api/config/workspace {path, extras}` 覆盖并持久化到 `app_settings`（JSON，
兼容旧的纯路径格式）。切换后重建注册表，下一句话生效。

**系统提示词动态注入（关键）**：模型原本不知道 `@libs` 的存在，所以
`AgentService._loop_config()` 在系统提示词尾部追加：

```
【附加工作区】除了主工作区，你还可以访问这些挂载目录（用 `@名字/路径` 的形式，
例如 `read_file("@libs/http/client.py")`）：
- @libs/ -> D:\SharedLibs（只读）
注意：相对路径只相对于主工作区解析；访问附加目录必须带 @名字 前缀，否则会被沙箱拒绝。
只读挂载不能写入。
```

同时三个工具的 description 里也写明 `@名字/路径` 用法（双保险：提示词 + 工具说明）。

**攻防验证**（`tools/verify_multiroot.py`，12 组）：

| 攻击/场景 | 结果 |
| --- | --- |
| `@libs/../../Windows/win.ini` | ✅ 拒绝「路径越出挂载 @libs」 |
| `@libs/../ProjectA/main.py`（从挂载逃回主根） | ✅ 拒绝 |
| `@libs/http/../../..`（爬到盘符根） | ✅ 拒绝 |
| 主根 `../SharedLibs/libs_only.txt` | ✅ 拒绝（附加目录不能靠相对路径进入） |
| 相对路径 `libs_only.txt`（想偷渡附加目录） | ✅ 拒绝（文件不存在：只在主根找） |
| `D:\...\SharedLibs_evil\payload.txt`（前缀混淆） | ✅ 拒绝 |
| `@libsevil/...`、`@libs2/...`（未注册挂载） | ✅ 拒绝并列出可用挂载 |
| `C:\Windows\...\hosts`（跨盘符） | ✅ 拒绝 |
| 写 `@libs/...`（只读挂载） | ✅ 拒绝「只读挂载，不允许写入」 |
| 写 `@scratch/...`（writable 挂载） | ✅ 成功 |
| 非法挂载名（`Libs`/`@Libs`/`1 bad`/超长/空/含斜杠）、重名 | ✅ 构造期拒绝 |
| 非法配置（`name: "../evil"`） | ✅ 加载期报错 |
| 不配 extras 时 | ✅ 行为与单根版本完全一致（`../` 仍拒绝） |

**真实模型闭环**（`tools/verify_multiroot_real.py`）：

```
提示词注入片段: 【附加工作区】… - @libs/ -> …（只读）
模型调用: read_file {"path": "@libs/http/client.py"}   ← 用了 @ 前缀
          list_dir {"path": "."}
          write_file {"path": "usage.py", ...}          ← 写在主根
写 @libs 的要求: 模型自己拒绝（只读挂载），且 @libs 文件内容未被改动（PORT 仍 8080）
```

`pytest` 仍 328 passed / 2 skipped：默认 `extras=()` 时旧路径分支完全不变。

**60. 目录选择器：点着选，不用手打绝对路径**

**问题**：工作区弹窗只能手输 `D:\ProjectA`；打错一个字母就报「路径不存在」，而且用户不知道
`D:\SharedLibs` 到底存不存在、要不要先自己新建。

**浏览器限制**：网页拿不到用户选择的绝对路径 —— `<input webkitdirectory>` 只给相对路径，
`showDirectoryPicker()` 只给目录名。所以选择器必须由**后端列目录**实现。

**后端**（`backend/service/fs_browser.py`）：
- `GET /api/fs/dirs?path=` → `{path, parent, entries:[{name,path}], drives, truncated}`；
  `path` 为空表示「此电脑」，只返回盘符；
- **只列目录、绝不返回文件内容**（`scandir` + `is_dir`，文件直接跳过）；
- 受保护目录不列出：系统根、`Program Files`、`ProgramData`、用户主目录整体、`AppData`/`.ssh` 等；
  指向受保护目录的符号链接/联接也不展示（点进去才报错更糟）；
- `POST /api/fs/mkdir {parent, name}` → 在可列出的父目录下新建文件夹（名字必须是单段，
  不含 `/ \ :`，不能是 `.`/`..`，不能重名），返回新目录绝对路径；
- 单次最多 500 项（`truncated` 标记），避免超大目录卡住前端。

**前端**（`FolderPickerDialog.tsx` + `WorkspaceDialog`）：
- 主目录和每个附加目录旁边都有 📁 按钮，点开选择器；
- 选择器里：盘符列表 / 面包屑 / 上一级 / 单击进入 / **双击直接选中** / 底部「新建文件夹」/
  「选择此文件夹」；
- 选中的路径回填到对应输入框（主目录或第 N 行附加目录），仍可手动编辑；
- 后端 400 的错误在选择器内显示（例如尝试进入 `C:\Windows`）。

**验证**（`tools/verify_fs_browser.py`，12 个拒绝用例 + 4 个正例）：

| 用例 | 结果 |
| --- | --- |
| 根列表只返回盘符 | ✅ |
| 列 `D:\` 只返回目录（文件被过滤） | ✅ |
| 进入探针目录：看到 `subfolder`，看不到 `file.txt` | ✅ |
| 新建文件夹并返回绝对路径 | ✅ |
| 列 `C:\Windows` / `System32` / 用户主目录 / `AppData` | ✅ 400 |
| 列不存在目录 / 文件 / UNC / 相对路径 | ✅ 400 |
| 在 `C:\Windows` 新建、名字含 `a/b`、名字 `..`、重名 | ✅ 400 |
| 新建的目录立刻可设为工作区 | ✅ 200 |

前端测试 `FolderPicker.test.tsx` 6 条：打开选择器见盘符、逐级进入并回填、附加目录回填、
选择器内新建并回填、系统目录错误提示、面包屑回「此电脑」。

**部署提示：新建文件夹可能被外部沙箱/只读盘拒绝**。`POST /api/fs/mkdir` 走的是后端进程的
文件权限：如果后端被上层沙箱（容器、DSH 之类）限制为「只能写某个目录」，或目标盘是只读卷，
`mkdir` 会返回 `WinError 5`。这时前端会给出可操作的提示（换一个有写权限的位置、或先在资源
管理器里手动建好再来选择），而不是干巴巴地抛 `[WinError 5] 拒绝访问`。
注意：**读取**不受这类沙箱影响不受影响（只读挂载 `@libs` 照常可用），只有新建/写入受限。

**61. `create_dir` 工具、可操作的权限报错，以及一次「假 React 崩溃」的诊断**

**① 新增 `create_dir` 工具**（`agent/tools/files.py` + `registry.py` + `KNOWN_TOOLS` + `config.yaml`）：
模型原来没有建目录的能力，只能靠「写一个占位文件」来顺带创建父目录，遇到权限问题就卡死。
现在有 `create_dir(path)`：多级创建（`parents=True`）、幂等（已存在直接返回提示）、
撞到同名文件报错；同样走 `resolve_with_mount`，所以 `../` 越界与只读挂载都被拦住
（只读挂载返回「@libs 是只读挂载，不允许创建目录」）。

**② 权限错误改成可操作的人话**：`read_file` / `write_file` / `list_dir` / `create_dir`
统一捕获 `PermissionError`：

```
错误：当前目录无写入权限，无法写入 main.py（拒绝访问）。请检查文件夹是否被设为只读，
     或后端进程是否具有该目录的写权限（也可以用 create_dir 建目录、或让用户换一个有写权限的工作区）。
```

而不是把 `[WinError 5] 拒绝访问` 原样丢给模型（模型会反复重试同样的操作）。

**③ 一次「假 React 崩溃」的诊断**：用户切到 `D:\ProjectA` 后写文件报
`[WinError 5]`，界面同时弹出
`Maximum update depth exceeded`。逐层排查：

| 怀疑对象 | 结论 |
| --- | --- |
| React 无限更新（`setState` in effect） | 用 `StrictMode + 流式 100 帧 + 工具调用 + 弹窗` 复现测试，**无法复现** |
| Zustand 选择器返回新对象 | 全项目 50 个选择器逐个检查，都是原始值/稳定引用 |
| 滚动反馈环（scrollTo → scroll 事件 → setState） | jsdom 无布局无法复现，但仍属真实风险，**已加固** |

`[WinError 5]` 的真正原因是**进程完整性级别**：`.venv\Scripts\python.exe` 被打上了
`Mandatory Label\Low`（Low 完整性），而 `D:\MyAi` 有 Low 可写 ACE、`D:\ProjectA` 没有 →
Low 进程写 `D:\ProjectA` 一定失败。系统 python（`D:\python3.11\python.exe`）是 Medium，可写。
所以本轮验证后端临时改用系统 python（`PYTHONPATH` 指向 venv 的 site-packages）。

**部署提示**：如果 venv 里的 python 被安全策略降权，`icacls /setintegritylevel` 需要管理员，
最省事的办法是用系统 python 启动后端，或重建 venv。读写表现会是「工作区内正常、工作区外只读」。

**④ 滚动/弹窗加固**（降低这类反馈环的真实风险）：
- `ChatArea`：加 `programmaticScrollRef`，自己发起的 `scrollTo` 产生的 scroll 事件不再翻转
  「用户在看上面」状态；但离底部仍很远（>100px）时说明是真人操作，必须尊重。
- `WorkspaceDialog`：初始化 effect 的依赖从数组 `extras` 换成稳定字符串 `extrasKey`，
  避免 store 每次刷新（`extras` 是新数组）反复点燃 effect；未加载时才补拉工作区。
- `FolderPickerDialog`：同一路径不重复请求（`lastLoadedRef`），防 prop 抖动连环加载。
- `ReasoningPanel`：只在展开时收起，去掉无意义的 setState。

新增回归测试：`NoUpdateLoop.test.tsx`（100 帧流式 + 同步 scroll 事件）、
`StrictLoop.test.tsx`（StrictMode 下流式 / 工作区弹窗 / 目录选择器三条路径），
任何一条出现 `Maximum update depth` 都会让测试失败。

**同类报错再次出现（2026-10-29）**：用户在一次商城页面生成中再次看到同一句 React 报错。
本轮新增 `ErrorBoundary`（页面级兜底 + 组件栈展示）与 React 19 root 的
`onUncaughtError` / `onCaughtError` 日志；多次复现尝试（真实模型 6KB 代码审批卡、
审批卡 + 100 帧流式、加载 29 条历史会话、长思考模型）均未能重现，因此没有改动渲染逻辑。
以后再出现时，Console 里 `[MyAi] 页面渲染异常` 会直接给出出错组件，可据此定点修复。

**62. 文件 CRUD 补齐（create_dir / delete_path / move_path）+ 低完整性进程诊断**

**① 新工具**（`agent/tools/files.py`、`registry.py`、`KNOWN_TOOLS`、`config.yaml`）：

| 工具 | 行为 | 安全边界 |
| --- | --- | --- |
| `create_dir(path)` | 多级创建，已存在直接返回提示（幂等） | 越界拒绝；只读挂载拒绝；撞到同名文件报错 |
| `delete_path(path)` | 删**文件**或**空目录** | 越界拒绝；只读挂载拒绝；**不能删工作区/挂载根**；非空目录拒绝（要求先删内容，避免误删整棵树） |
| `move_path(src, dest)` | 移动/重命名文件或目录（跨卷自动退回 `shutil.move`） | 越界拒绝；只读挂载不能当源或目标；**目标已存在拒绝覆盖**；源/目标是根目录拒绝 |

都走 `Workspace.resolve_with_mount()`，所以 `../` 越界、`@libs` 只读、`C:\Windows` 这类
一律沿用同一套红线判定。

**② 「WinError 5」的根因与诊断**（上一轮已经查明，这轮做成可检测）：

某些安全策略会把 venv 里的 `python.exe` 打上 `Mandatory Label\Low`（低完整性）。
低完整性进程**能读**大部分文件，但**写**只允许落在同完整性或更低的对象上：
`D:\MyAi` 有 Low 可写 ACE 所以能写，`D:\ProjectA` 没有所以写不了。

- 新增 `backend/service/runtime_diag.py`：用 Win32 `TokenIntegrityLevel` 检测进程完整性
  （ctypes 失败时回退 `whoami /groups`），结果缓存。
- `GET /api/health` 现在返回 `python`、`integrity`、`write_warning`；
  启动时若检测到 `low` 会打一条 WARNING 日志。
- 所有写类工具（`write_file` / `create_dir` / `delete_path` / `move_path`）的
  `PermissionError` 提示里直接写清两条解法：
  ```
  请检查：① 文件夹是否被设为只读（可在资源管理器取消，或执行 attrib -R "路径"）；
         ② 后端是否用系统全局 Python 启动 —— 某些安全策略会把 venv 里的 python.exe
            降为 Low 完整性级别，导致它只能写工作区目录、写外部工作区一律 WinError 5。
  ```
- 本项目自身的运行方式：`D:\python3.11\python.exe`（Medium）+ `PYTHONPATH` 指向 venv 的
  site-packages。**这是环境限制，不是代码能绕过的**（改标签需要管理员权限）。

**③ 实测结果**（`tools/verify_crud.py`）：

| 用例 | 结果 |
| --- | --- |
| `/api/health` → `integrity=medium`、无警告 | ✅ |
| `enabled` 含 read/write/create_dir/delete_path/move_path/list_dir | ✅ |
| `delete_path` 文件 / 空目录 | ✅ 删除成功 |
| `delete_path` 非空目录 | ✅ 拒绝「不是空目录…先删内容」 |
| `delete_path` 根目录 / `../` 越界 | ✅ 拒绝 |
| `move_path` 重命名、自动建父目录 | ✅ |
| `move_path` 目标已存在 | ✅ 拒绝覆盖 |
| 用 `delete_path` 逐层清除旧 `workspace/ProjectA` | ✅ 无需 shell |
| 在 `D:\ProjectA` 重做 `create_dir('src/components')` + `write_file` | ✅ |
| `python main.py` | ✅ 输出 `hello ProjectA` |

最终 `D:\ProjectA` 结构：

```
main.py
src/components/__init__.py
```

**63. Token 统计与「极客状态栏」：会话级缓存命中、上下文占用、省钱估算**

**需求**：底部状态栏静默展示当前会话的 token 消耗与缓存节省；每条 AI 消息底部显示本轮
`prompt 1200 (命中 90%) / completion 50 / total 1250`。

**后端**（三层，全部向后兼容）：
- `agent/provider.py`：`Usage` 新增 `cache_hit_tokens`；`from_raw()` 同时兼容 DeepSeek 顶层
  `prompt_cache_hit_tokens` 与 OpenAI 嵌套 `prompt_tokens_details.cached_tokens`，取不到为 0；
  `cache_hit_rate` 在 prompt=0 时返回 0（不做除零）。
- `agent/storage.py`：`sessions.cache_hit_tokens` 与 `messages.usage_json` 两列，`_migrate()`
  自动给旧库补列；`add_usage(..., cache_hit_tokens=)` 累加；`get_messages(with_usage=True)` 才回放
  `usage`（默认不带，绝不污染发回模型的请求）；新增 `usage_totals()` 全库聚合。
- `backend/`：`message_end.usage` 透传 `cache_hit_tokens` / `cache_hit_rate`；
  `GET /api/stats?session_id=` 默认返回**当前会话**的累计值（不传为全库诊断口径）；
  `/api/models` 补 `context_window` 供前端算上下文占用。

**口径（修过一次的真实 bug）**：
- 命中率 = `cache_hit_tokens / prompt_tokens`，**completion 永不进分母**（专项用例：
  prompt=1000 / completion=500 / cache=940 → 0.94，而不是 0.63）。
- 第一版按「全库」累计：旧会话没有缓存字段（按 0 计）会把当前会话 90%+ 的命中率稀释成 6–7%，
  与单条消息严重矛盾。现在默认按**当前会话**聚合，单条消息与累计值统计范围一致；
  切会话后旧响应按请求序号丢弃，不会串数据。

**前端**：
- 单条消息：usage 从 hover 操作区移到**常驻**的 10px 灰字行
  （`prompt X (命中 Y%) / completion Z / total T`），历史消息由 `usage_json` 回放。
- 状态栏（Composer 卡片底部）：`Database 累计 {tok} | Zap 节省 {rate}（约 ¥{money}）| Brain 当前上下文 {pct}`；
  全部使用 lucide 线性图标 + `gray-400/600` 灰阶，不用 Emoji、不用彩色，
  容器 `overflow-hidden` 保证不溢出卡片；无数据时显示「暂无数据」。
- 省钱估算：`src/lib/pricing.ts` 里 `0.001 元/千 token` 的默认输入单价 × 命中 tokens，
  NaN/负数/Infinity 一律夹到 0（有纯函数测试）；上下文占用 ≈ 最近一轮 `prompt_tokens / context_window`。

**实测**：同一会话里单条 `prompt 3125 (命中 94%)`、会话累计 **84.4%**、全库 6.7% —— 状态栏现在展示
84.4%（会话口径），不再被历史旧会话稀释；`/api/stats?session_id=...` 只返回该会话的数值。

**64. 侧边栏极简化 + 模型选择器迁入输入框（对齐 Cursor 的层级与配色）**

**动机**：侧边栏同时承担品牌、模型、会话、知识库，视觉密度高且蓝色块面积大；模型选择是高频操作，应贴着输入框。

**侧边栏**（`Sidebar.tsx` / `SessionList.tsx` / `KnowledgePanel.tsx`）：
- 只保留「品牌 + 新建会话 + 历史会话（+ 折叠知识库）」；模型下拉框与 ⊕ 按钮整体移除。
- 品牌区改极简线条：白底细边框 + `Box` 深灰图标，`Nexus 智枢` 用 `font-bold text-gray-900`，
  右侧 `PRO` 黑底白字小标签；去掉渐变蓝与蓝色文字。
- 新建会话：白底 + 细边框 + 灰色文字 + 细线 `+`（不再是蓝色实心）。
- 历史列表：选中态 `bg-gray-100` + 左侧 2px 深黑指示条（`inset box-shadow`，不产生布局抖动）；
  标题 `gray-800`、副文本 `gray-400`；重命名/删除悬浮按钮同色阶。
- 配色纪律：侧边栏组件内不再出现 sky/blue/emerald/indigo（`Select-String` 自查为空），
  仅错误/破坏性操作保留 rose；蓝色只留给右下角发送按钮。

**输入框右下角**（`ModelMenu.tsx`）：
- 收起态是灰字 + 下拉箭头的小按钮（当前模型名），旁边是极简 `＋`（打开模型管理弹窗）。
- 展开态 `absolute bottom-full right-0 z-50` **向上弹出**，`max-h-64` 内部滚动；
  不会压到下方状态栏、也不会被卡片/屏幕边缘裁切；点外部或 Esc 收起。
- 菜单项显示「默认 / 自定义 / 未配置密钥」标记，底部固定「添加 / 管理模型」入口；
  生成期间模型按钮禁用、`＋` 仍可打开管理弹窗（弹窗内保存/删除禁用）。
- 状态栏（Token 统计 + 快捷键提示）保持在卡片最底部那一行，不受菜单影响。

**测试**：新增 `ModelMenu.test.tsx` 5 条（侧边栏无模型控件、菜单向上弹出且 `z-50`、选择写回 store、
底部入口打开弹窗、streaming 时禁用/仍可管理）；`ModelManager` / `ModelPicker` / `FetchModels` / `App`
等用例改为从 Composer 进入。前端 37 文件 / 199 passed + 6 skipped（含后续审批卡 / 错误边界用例）；`tsc --noEmit` 通过。

**65. Human-in-the-loop 工具审批：线程阻塞网关 + SSE 审批卡片**

**目标**：AI 要做**代码执行 / 删除 / shell** 这类高危操作时，先挂起，让人看到「即将发生什么」再决定；
写入/建目录在 workspace 沙箱边界内静默执行，避免生成页面时每个文件弹一次、打断心流。

**线程模型（关键）**：Agent 循环跑在 `asyncio.to_thread` 的工作线程里，所以「挂起」不用协程，
而是 `backend/service/approvals.py` 里的同步原语：
- `ApprovalGate.wait()` 在线程里阻塞（默认 300s），`POST /api/chat/approve` 从 HTTP 线程
  `resolve()` 唤醒；一次性、超时按拒绝。
- `AgentService.approvals` 是进程级登记表；`approval_id` 一次性，404（不存在/过期）与 409（重复决策）分开。
- 客户端断开：SSE 生成器 `finally` 里 `gate.cancel()`，工作线程按「拒绝」继续并把
  `用户已拒绝此操作，工具未执行。` 回填成 tool 消息；再加上 `AgentLoop.abort_pending_tool_calls()`
  收尾，历史永远合法（绝不悬空 `tool_call`）。
- 配置：`tools.require_approval` / `approval_required` / `approval_timeout`；
  默认审批集合 = `run_python_code` / `delete_path` / `run_shell`；
  `write_file`、`create_dir` 不在集合内（沙箱内静默执行，覆盖已有文件时结果会注明
  「文件已存在，已执行覆盖」）。CLI 用终端 y/N，非 TTY 自动拒绝。

**信任边界（重要）**：在**完全信任的本地沙箱环境**（自己的开发机、workspace 也是自己的代码）里，
可以把 `tools.require_approval` 设为 `false`，获得最高效率的编码体验——所有工具（含
`run_python_code` / `delete_path` / `run_shell`）静默执行、不再弹审批框；如果 workspace 可能包含
他人代码、下载来的脚本或任何不可信输入，请保持 `require_approval: true`，否则等于把执行权限直接交给模型。

**前端**：`approval_required` 事件把 `approval` 状态挂到对应工具卡片上，`ApprovalCard` 渲染黄色卡片：
`write_file` 解析 `path`/`content`，用只读 `CodeBlock`（行号 + 高亮）展示即将写入的内容
（超 200 行折叠并提示「同意后完整写入」；默认审批集合已不含 write_file，但把它加回配置时
仍会使用这套富文本预览）；`delete_path`/`move_path` 展示路径与不可逆提醒；
按钮 `同意执行` / `拒绝`；决定后卡片降级为普通工具卡 + 「已同意/已拒绝/审批过期」徽标，
`tool_call_end` 的结果照常展示（执行结果可见）。

**实测**：真实模型触发 `run_python_code` → SSE `approval_required` → 前端黄色卡片（代码预览）→
点同意 → 子进程执行 → `tool_call_end` 展示 stdout → 模型给出最终回答。

**66. run_python_code：受限的 Code Interpreter（默认关闭 + 默认审批）**

**动机**：让模型「现场生成、现场执行」代码，而不是只能读文件。

**实现**（`agent/tools/python_exec.py`）：
- 代码非空且 ≤20KB；写到**主工作区** `.agent_tmp/run_<uuid6>.py`（走 `Workspace` 校验，
  只读挂载/越界拒绝），执行后必删，空目录也清理；
- `sys.executable -I -B -X utf8` + 最小环境（SYSTEMROOT/PATH/TEMP/TMP）+ `cwd=workspace.root`
  + `capture_output` + `timeout`；`-X utf8` 是因为 `-I` 会忽略 `PYTHONIOENCODING`，
  不加在 GBK 机器上中文输出会乱码；
- 返回「退出码 / 用时 / --- stdout --- / --- stderr ---」，各自按 `max_output_chars` 截断并注明省略量；
  超时抛 `执行超时（Ns），已终止`；
- 配置：`tools.python_exec.timeout`（默认 10s）/ `max_output_chars`（默认 8000）；工具可通过
  `timeout` 参数覆盖单次上限。

**安全边界（诚实版）**：这不是 OS 级隔离。代码以当前进程权限运行，能读写该权限触达的文件、
访问网络；`-I` 只隔离 Python 环境，不构成安全边界。因此：`config.example.yaml` 默认**关闭**；
一旦启用，默认纳入 `approval_required`（每次执行都要人工确认）；真实边界依赖 OS 账户/沙箱，
本期不承诺。

**依赖自检**：`backend/main.py` 启动时尝试 import `docx`/`pypdf`，缺失则控制台黄色打印
「⚠️ 缺少依赖 python-docx 或 pypdf，文档解析功能将不可用。请运行 pip install -r requirements.txt」，
只警告、不崩溃。

**67. 非法 JSON arguments 本地拦截 + 审批粒度调优（长任务不再 400 与弹窗轰炸）**

**现象**：生成带大量引号/特殊字符的 HTML/JS 时，模型偶尔把 `function.arguments` 的 JSON
转义写坏。旧实现把原始字符串原样回填进 assistant 历史，下一轮请求被上游以
`400 The "function.arguments" parameter must be in JSON format` 拒绝；模型判定失败后反复重试，
几秒内打满 `max_tool_rounds`，整轮报废。

**本地拦截（`agent/loop.py`）**：在构造 assistant 消息回填历史**之前**，
对每个 `tool_call.arguments` 做 `json.loads` 且要求解析结果是 JSON 对象：
- 合法 → 原样透传（保留模型的空白/转义风格，不改写）；
- 非法 → 绝不发给上游：该调用 arguments 替换为合法占位 `{}`，工具**不执行**，
  tool 结果回灌「参数不是合法 JSON，请重新生成（本次工具未执行）。」；
- 模型在同一轮内看到这条 tool 错误后自行纠正，不再产生上游 400；
- 同时保留 `ToolArgumentsError`（ToolError 子类）与 warning 日志，便于排查；
- 多 tool_calls 混合场景：合法调用照常执行，只有坏的那条被拦截。

**审批粒度调优**：`write_file` / `create_dir` 移出 `approval_required`（workspace 沙箱内
写入属于预期行为），审批只保留 `run_python_code` / `delete_path` / `run_shell`。
`write_file` 覆盖已有文件时返回「文件已存在，已执行覆盖」，让模型明确知道这次是覆盖而不是新建。
`max_tool_rounds` 默认 8 → 15，给复杂页面生成留出重试空间；`write_file` / `run_python_code`
的 description 增加「生成含大量引号/特殊字符的代码时请确保 JSON 转义正确」提示。

**验证**：`tests/test_loop.py` 覆盖单条与多 tool_call 的非法 arguments 拦截；
`tests/test_mock_server.py` 用真实 HTTP 假端点抓取每个请求体，断言所有
`tool_calls[].function.arguments` 都能被 `json.loads` 解析，并验证非法调用未真正写文件。

**68. 上下文记忆：大参数压缩 + 1M 窗口 + 自动摘要（不再粗暴裁剪）**

**问题**：写长代码时上下文瞬间 100%。旧实现有三个叠加原因：
① 写文件的 `tool_call.arguments` 里带着完整代码，历史每轮都重发；
② 模型上下文窗口按 32K 估，实际 DeepSeek-V4 / Qwen3.8-Flash 都是 **1M**；
③ 超限后 `trim_history` 直接从最旧消息砍起，模型「失忆」。

**① 大参数历史压缩**（`agent/loop.py`）：`write_file` / `run_python_code` 的 `arguments`
超过 2000 字符时，发给模型的历史里替换成合法占位 JSON：
`{"path":"snake.js","content":"（21540 字符的代码已写入文件，历史中省略）"}`。
磁盘文件与前端展示不受影响；工具返回值本身也一直是「已写入 xxx，X 字节」，从不回显代码。

**② 真实上下文窗口**：按官方文档把内置 `deepseek` 调到 **1,000,000**
（DeepSeek-V4 上下文 1M，输出最大 384K）；Qwen3.8-Flash 官方文档同样为
**上下文 1,000,000 / 最大输入 991,808**。自定义模型无法从配置读取时：
已知模型（deepseek-v4* / qwen3.8* / qwen3.7* / qwen-turbo 等）按 1M，其余按 128K 保守估计。
`/api/models` 返回该值，前端状态栏占用率的分母随之同步；≥80% 变琥珀色、≥95% 变玫红。

**③ 自动摘要压缩**（替代直接裁剪）：每轮请求前检查估算 tokens（优先用上一轮真实
`prompt_tokens`，否则按 2.5 字符/token 估算）。达到 `context_window × context_summary_trigger`
（默认 80%）时：
- 取最旧的 `context_summary_messages`（默认 10）条消息（按「assistant(tool_calls)+tool」整组取，绝不切断工具组）；
- 调用**后台静默模型**（同模型、`temperature=0.2`、`max_tokens=512`）压成 ≤200 字摘要，
  形如「用户之前要求写贪吃蛇，模型遇到 JSON 报错，最后修正了文件」；
- 摘要追加进 system 提示词的「【历史记忆摘要】」块，原消息从上下文移除；DB 完整历史不动
  （前端回放仍是全文），失败则静默退回旧的按条数裁剪。
配置：`agent.context_summary_trigger` / `agent.context_summary_messages`；
摘要模式下不再按 `max_history_messages` 砍历史（该配置只作为未启用摘要时的兜底）。

**验证**：`tests/test_loop.py` 覆盖参数压缩、摘要替换与最近两轮保留、摘要失败回退；
`tests/test_backend_service.py` 覆盖 qwen3.8-flash 自定义模型返回 1M 窗口。

## 多模态补充（第一批）

- 图片文件落 `data/uploads/`，消息表只新增 nullable `attachments_json`；不把 base64 写进 SQLite，旧会话零影响。
- `AgentLoop` 只加 `run_stream(..., attachments=...)` 与 `request_transform` 两个可选扩展点；Vision 组装全部在 backend。
- 视觉能力由 `ModelConfig.supports_vision` 声明，默认 false；前端禁用 + 后端 422 双重拦截，防止绕过 UI。
- 历史图片最多重放 3 轮（当前 + 最近 2 轮），更早的用 `[图片]` 占位，控制 data URL 请求体大小。
