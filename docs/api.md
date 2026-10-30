# Web API

> 返回 [README](../README.md)

## REST 端点

| 方法 | 路径 | 请求体 | 响应 |
|---|---|---|---|
| GET | `/api/models` | — | `[{name, model, base_url, api_key_env, key_set, is_default, is_custom, has_key, api_key_masked}]`（内置 + 自定义合并） |
| GET | `/api/models/custom` | — | `{models:[{name, base_url, model, has_key, api_key_masked, created_at, updated_at}]}` |
| POST | `/api/models/custom` | `{name, base_url, model, api_key}` | `201 {model}`；重名 409、非法 400 |
| DELETE | `/api/models/custom/{name}` | — | `{deleted, name, referenced_sessions}` |
| POST | `/api/models/fetch` | `{base_url, api_key}` | `{base_url, models, count}`（一次性只读，Key 不落库） |
| POST | `/api/sessions/{id}/regenerate?model=` | — | SSE（先删旧回答再重跑，历史不重复） |
| GET | `/api/kb/status` | — | `{docs, chunks, mode, supported[], max_file_mb, pipeline}` |
| GET | `/api/kb/docs` | — | `{docs:[{id,name,size,pages,chunk_count,status,mode,error}]}` |
| POST | `/api/kb/docs` | `multipart: file` | `{doc...}`（201；后台解析，status=parsing） |
| DELETE | `/api/kb/docs/{id}` | — | 被删的文档对象（切片级联删除） |
| GET | `/api/tools` | — | `{tools:[{name, enabled, default_disabled, description}]}` |
| GET | `/api/sessions?limit=20` | — | `{sessions:[{id,title,model,updated_at,message_count,total_tokens}]}` |
| GET | `/api/sessions/{id}` | — | `{session, messages:[...], system_prompt}`（消息含 `reasoning_content`，旧消息为 null；id 支持唯一短前缀） |
| POST | `/api/sessions` | `{model?, title?}` | `201 {session}` |
| PATCH | `/api/sessions/{id}` | `{title}` | `{session}`（空串=清空标题） |
| DELETE | `/api/sessions/{id}` | — | `{deleted, session}`（消息级联删除） |
| POST | `/api/sessions/{id}/generate_title?force=false` | — | `{session_id, title, generated, model}`（短超时、非流式，失败 503） |
| PATCH | `/api/sessions/{id}/system_prompt` | `{system_prompt}` | `{session_id, system_prompt, created, seq}` |
| POST | `/api/chat/stream` | `{session_id, message, model?}` | `text/event-stream` |
| POST | `/api/chat/approve` | `{approval_id, approved, reason?}` | 200 `{ok, approval_id, approved}`；404 不存在/已过期、409 已处理 |
| GET | `/api/health` | — | `{status:"ok", python, integrity, write_warning}`（含运行时诊断） |
| GET | `/api/stats?session_id=` | — | `{sessions, prompt_tokens, completion_tokens, total_tokens, cache_hit_tokens, cache_hit_rate, session_id}`（默认按当前会话；不传 `session_id` 为全库诊断口径） |
| GET | `/api/config/workspace` | — | `{workspace, default_workspace, is_default, extras:[{name, path, writable}]}` |
| POST | `/api/config/workspace` | `{path, extras?:[{name, path, writable}]}` | 同上结构；合法 200、非法 400（切换后下一句话立即生效） |
| DELETE | `/api/config/workspace` | — | 同上结构（恢复默认工作区并清空附加挂载） |
| GET | `/api/fs/dirs?path=` | — | `{path, parent, entries:[{name, path}], drives, truncated}`（空 `path` 返回盘符列表；只列目录） |
| POST | `/api/fs/mkdir` | `{parent, name}` | `{name, path}`（在可列出的父目录下新建文件夹；非法 400） |

**自定义模型端点语义**：`api_key` 明文只存 SQLite，绝不出现在响应与日志里；对外只有脱敏的
`api_key_masked`（如 `sk-****3456`）。模型名重名 409、字段非法 400、删除不存在的模型 404。

**多根挂载**：配置字段 `agent.extra_workspaces: [{name, path, writable}]`（`path` 与
`agent.workspace` 一样以**配置文件所在目录**为相对路径基准；`writable` 默认 `false`，即只读），
运行时也可用 `POST /api/config/workspace` 的 `extras` 覆盖。模型用 `@name/路径` 访问挂载，
普通相对路径只相对主工作区解析；只读挂载不能写入（第 59 条）。


## 图片上传

| 方法 | 路径 | 请求体 | 响应 |
|---|---|---|---|
| POST | `/api/uploads` | `multipart: file` | `201 {id,url,mime,name,size}`；仅 JPG/PNG/WebP，单张 ≤5MB |
| GET | `/api/uploads/{id}` | — | 原图（Content-Type 按文件头） |

`POST /api/chat/stream` 的 `ChatRequest` 增加 `attachments: [{id, name?}]`；带附件时 `message` 可为空，
backend 会存 `[图片]` 作为文本占位并转换为 OpenAI Vision content 数组。模型 `supports_vision=false` 时返回 422。

## SSE 事件协议

每帧都是 `data: <JSON>\n\n`：

```text
{"type":"message_start","session_id":"..."}
{"type":"reasoning_delta","text":"先把两个数…"}          ← 思维链增量（模型支持时才有）
{"type":"tool_call_start","tool_call_id":"...","name":"read_file","arguments":"{\"path\": \"readme.txt\"}"}
{"type":"tool_call_end","tool_call_id":"...","name":"read_file","ok":true,"content":"..."}
{"type":"approval_required","approval_id":"ap_1a2b3c4d","session_id":"...","tool_call_id":"...","name":"write_file","arguments":"{...原始 JSON...}","expires_in":300}
{"type":"text_delta","delta":"..."}                    ← 真·逐 token
{"type":"message_end","content":"...","reasoning":"...","usage":{"prompt_tokens":24,"completion_tokens":12,"total_tokens":36,"cache_hit_tokens":18,"cache_hit_rate":0.75},"rounds":1,"tool_limit_reached":false}
{"type":"error","message":"...","code":"ProviderAuthError"}
```

`arguments` 是模型返回的**原始 JSON 字符串**（后端不解析再序列化，与第 21 条一致）；
前端自己格式化显示。
