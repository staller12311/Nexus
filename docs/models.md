# 模型接入

> 返回 [README](../README.md)

## 如何新增一个模型

以 Moonshot（月之暗面）为例，三步。

**第 1 步：在 `config.yaml` 的 `models` 下加一段**（字段名不可改）：

```yaml
default_model: moonshot          # 想让它当默认模型就改这里，否则保持原值

models:
  # ... 已有的模型 ...
  moonshot:
    base_url: https://api.moonshot.cn/v1   # OpenAI 兼容端点，注意结尾 /v1
    model: moonshot-v1-8k                  # 上游真实的模型名
    api_key_env: MOONSHOT_API_KEY          # 只写「环境变量名」，不写密钥本身
    temperature: 0.3
    max_tokens: 4096
    context_window: 8192                  # 按模型官方文档填真实上下文窗口
```

- `base_url` / `model` / `api_key_env` 是必填；`temperature` / `max_tokens` / `context_window` 可省，默认 0.7 / 4096 / 32768。
  自定义模型未显式配置时按 **128K** 保守估计；已知 1M 模型（DeepSeek-V4 系列 / qwen3.8-flash 等）自动按 1M 计算。
- `api_key_env` 只是变量名。**密钥永远不写进 YAML**（第 5 条设计决策）。
- 同一份配置里可以并列多个 provider，用 `--model` 或 `/model` 切换。

**第 2 步：把变量加进 `.env`**（`.env` 已在 `.gitignore` 里，不会被提交）：

```bash
cp .env.example .env                 # Windows: copy .env.example .env
```

```dotenv
MOONSHOT_API_KEY=sk-你的真实密钥
```

也可以不用 `.env`，直接在 shell 里设置（`set` / `export`）同名环境变量。

**第 3 步：验证与切换**

```bash
python -m agent config check                    # 检查默认模型（这里是 moonshot）
python -m agent models list                     # 看所有模型及其密钥状态
python -m agent --model deepseek config check   # 指定其它模型检查
python -m agent --model moonshot run "你好"     # 用它跑一次
```

交互模式下切换（写进当前会话，下一次请求生效）：

```console
你 > /model moonshot
已切换到模型 moonshot（moonshot-v1-8k），下次请求生效
```

真实的 `config check` 输出（密钥填的是占位值；输出里**不会出现密钥**，只会显示变量名与已设置/未设置）：

```console
$ python -m agent --config config.yaml config check
配置文件              D:\MyAi\_acc\config_moonshot.yaml
默认模型              moonshot
当前模型              moonshot (model=moonshot-v1-8k)
base_url              https://api.moonshot.cn/v1
api_key_env           MOONSHOT_API_KEY: 已设置
temperature           0.3
max_tokens            4096
context_window        8192
request_timeout       60s
max_retries           3
max_tool_rounds       8
max_history_messages  40
workspace             D:\MyAi\_acc\workspace
db_path               D:\MyAi\_acc\data\sessions.db
log_file              D:\MyAi\_acc\data\agent.log (INFO)
启用工具              read_file, write_file, list_dir
系统提示词            你是一个严谨、简洁的助手。
配置检查通过
```

切换成另一个模型后的同一段输出：

```console
$ python -m agent --config config.yaml --model deepseek config check
当前模型              deepseek (model=deepseek-chat)
base_url              https://api.deepseek.com/v1
api_key_env           DEEPSEEK_API_KEY: 已设置
配置检查通过
```

若变量没设置，`config check` 会照常打印配置表，但退出码为 1 且错误写到 stderr：

```console
$ python -m agent --config config.yaml --model deepseek config check   # 未设 DEEPSEEK_API_KEY
api_key_env           DEEPSEEK_API_KEY: 未设置
...
[stderr] 配置检查失败：环境变量 DEEPSEEK_API_KEY 未设置或为空（模型 deepseek 需要它）。
exit=1
```
