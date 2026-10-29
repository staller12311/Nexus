import json, sys, urllib.request

# 取最近一个刚做过 regenerate 的会话
listing = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/sessions?limit=3", timeout=10).read().decode())
sessions = listing["sessions"]
target = None
for item in sessions:
    detail = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8000/api/sessions/{item['id']}", timeout=10).read().decode())
    roles = [m["role"] for m in detail["messages"]]
    if roles.count("user") == 1 and "tool" in roles and roles[-1] == "assistant":
        target = (item["id"], detail, roles)
        break

if not target:
    print("找不到测试会话"); sys.exit(1)

sid, detail, roles = target
print(f"会话 {sid[:8]}：{len(detail['messages'])} 条")
print(f"角色序列: {roles}")

# 不变量 1：只有一条 user 消息（重新生成没有重复插入）
print(f"user 条数 = {roles.count('user')}")
assert roles.count("user") == 1

# 不变量 2：每条 tool 消息都有紧邻的、带 tool_calls 的 assistant 前置
messages = detail["messages"]
problems = []
for index, message in enumerate(messages):
    if message["role"] == "tool":
        owner = None
        for back in range(index - 1, -1, -1):
            if messages[back]["role"] == "assistant":
                owner = messages[back]
                break
            if messages[back]["role"] == "user":
                break
        if owner is None or not owner.get("tool_calls"):
            problems.append(index)
        else:
            ids = [c["id"] for c in owner["tool_calls"]]
            if message["tool_call_id"] not in ids:
                problems.append(index)
print(f"孤立 tool 消息索引: {problems}")
assert not problems, f"出现孤立 tool 消息：{problems}"

# 不变量 3：没有半截 assistant（content 和 tool_calls 都为空）
empty = [i for i, m in enumerate(messages) if m["role"] == "assistant" and not m.get("content") and not m.get("tool_calls")]
print(f"空 assistant 消息: {empty}")
assert not empty

# 不变量 4：最后一个 tool_calls 的 id 都拿到了对应结果
last_call_ids = None
for message in messages:
    if message["role"] == "assistant" and message.get("tool_calls"):
        last_call_ids = [c["id"] for c in message["tool_calls"]]
got = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
print(f"最后一组 tool_calls: {last_call_ids}  已结果: {sorted(got)}")
assert last_call_ids and all(cid in got for cid in last_call_ids)

print("\n✅ 结构不变量全部成立：历史合法、无孤立 tool、无半截 assistant")
