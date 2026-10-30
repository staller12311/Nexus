# CLI 用法

所有命令都支持 `--config <路径>`（优先级最高）与 `--model <名称>`（覆盖 `default_model`）。

```bash
agent --help
```

### 用法一：`run "问题"` —— 单次问答后退出

```console
$ python -m agent run "你好"
🔧 调用 read_file({ "path" : "readme.txt" })
   ↳ # readme.txt（32 字节）
我读到了：# readme.txt（32 字节） / 你好，我是假端点。 /
   tokens: prompt=30 completion=10 total=40  用时 0.1s
```

要点：`🔧` 行报告本轮的工具调用（参数摘要过长会截断）；`↳` 行是工具结果首行；
`tokens` 行是**本次运行**累计用量，同时会累加写入会话记录。

退出码：成功 `0`；配置错误 / 密钥缺失 / 模型调用失败 `1`；被 Ctrl+C 中断 `130`；用法错误 `2`。

### 用法二：`chat` —— 交互式多轮

```console
$ python -m agent chat
会话 0d2089b9 · 模型 mock (manual-model) · 工具 read_file, write_file, list_dir
输入 /help 查看命令，/exit 或 Ctrl+D 退出；Ctrl+C 只中断当前请求。
你 > 你好
🔧 调用 read_file({ "path" : "readme.txt" })
   ↳ # readme.txt（32 字节）
我读到了：# readme.txt（32 字节） / 你好，我是假端点。 /
   tokens: prompt=110 completion=10 total=120  用时 0.1s
你 > /history
会话 0d2089b972f3（5 条消息，来自数据库）
[1] system
    你是一个严谨、简洁的助手。
[2] user
    你好
[3] assistant
    → read_file({ "path" : "readme.txt" })  id=call_read
[4] tool  tool_call_id=call_read  name=read_file
    # readme.txt（32 字节）
    你好，我是假端点。
[5] assistant
    我读到了：# readme.txt（32 字节） / 你好，我是假端点。 /
你 > /exit
已退出。
```

斜杠命令：

| 命令 | 作用 |
|---|---|
| `/exit` | 退出（等同 Ctrl+D） |
| `/new` | 新建会话；旧会话已保存，可用 `sessions resume <id>` 回来 |
| `/model <name>` | 切换模型，写进当前会话，下次请求生效 |
| `/history` | 从数据库读当前会话的完整消息并渲染 |
| `/tools` | 查看工具启用状态 |
| `/help` | 命令帮助 |

中断行为：**Ctrl+C / Ctrl+Break 只中断当前这一次请求**，打印「已中断」并回到提示符；
`/exit` 或 Ctrl+D 才退出。空回车会被忽略（不会发给模型）。

### 用法三：会话恢复

```bash
python -m agent sessions list            # 最近会话（id 显示 8 位）
python -m agent sessions show 766b2612   # 支持唯一短前缀
python -m agent sessions resume 766b2612 # 接着聊
python -m agent chat --session 766b2612  # 等价写法
```

恢复时的真实输出（旧会话先跑过一轮工具调用，共 5 条消息）：

```console
$ python -m agent sessions list
 id         标题       更新时间           消息    tokens
 ───────────────────────────────────────────────────────
 de5de4d8   (未命名)   2026-10-27 02:50      5        30

$ python -m agent sessions resume de5de4d8
会话 de5de4d8 · 模型 mock (manual-model) · 工具 read_file, write_file, list_dir
输入 /help 查看命令，/exit 或 Ctrl+D 退出；Ctrl+C 只中断当前请求。
你 > 恢复之后的新问题
我读到了：# readme.txt（32 字节） / 你好，我是假端点。 /
   tokens: prompt=10 completion=5 total=15  用时 0.1s
你 > /history
会话 de5de4d8be42（7 条消息，来自数据库）
[1] system
    你是一个严谨、简洁的助手。
[2] user
    历史里第一句
[3] assistant
    → read_file({"path": "readme.txt"})  id=call_read
[4] tool  tool_call_id=call_read  name=read_file
    # readme.txt（32 字节）
    你好，我是假端点。
[5] assistant
    我读到了：# readme.txt（32 字节） / 你好，我是假端点。 /
[6] user
    恢复之后的新问题
[7] assistant
    我读到了：# readme.txt（32 字节） / 你好，我是假端点。 /
你 > /exit
已退出。
```

**模型实际收到的历史**（用假端点把请求体记下来核对的）：恢复后这一次请求发了 6 条消息
`system → user(旧) → assistant(tool_calls) → tool → assistant(旧回答) → user(新问题)`，
旧历史在、序列合法（每个 `tool` 都有前驱 `assistant`）。`/history` 与
`sessions show de5de4d8` 的输出逐条一致，`tokens` 也会继续累加（30 → 45）。

### 其他子命令

```bash
python -m agent models list     # 已配置的模型（只显示 api_key_env 名与已设置/未设置）
python -m agent tools list      # 全部工具及启用状态（含默认禁用的两项）
python -m agent config check    # 打印将要使用的配置；密钥缺失时退非 0 且错误走 stderr
```

> 返回 [README](../README.md)
