# 对接 Hermes Agent 完整教程

> open-xiaoai-bridge 把小爱音箱的麦克风/扬声器接到 [Hermes Agent](https://hermes-agent.nousresearch.com/)，
> 让小爱用人话回报 Agent 跑出来的结果——天气、控家、提 issue、查日历都可以。
> 本文从最小可跑的 OpenAI 兼容路径开始，逐步走到原生 backend、双向触发和多 Agent 路由。

[Hermes Agent](https://hermes-agent.nousresearch.com/) 是一个本地优先的多模态 Agent，
内置 Skills / Cron / Kanban，并暴露 OpenAI 兼容和原生两套 HTTP API。
本仓库 (`open-xiaoai-bridge`) 把小爱音箱的麦克风/扬声器接到这套 API，
让你直接对小爱说话就能跑 Hermes 的 skill。

---

## 0. 名词速览

| 名字 | 是什么 |
|---|---|
| Bridge | 本仓库 (`open-xiaoai-bridge`)。Python + Rust，跑在 NAS / 树莓派 / x86 小主机上，与小爱音箱通讯。 |
| Hermes API Server | Hermes Agent 起的 HTTP 服务，默认端口 `8642`，支持 `/v1/chat/completions`（OpenAI 兼容）+ `/v1/runs`（原生）。 |
| backend | Bridge 里的一个适配层，决定语音文字最终送到哪：`xiaozhi` / `openclaw` / `openai` / `hermes`。本文重点讲后两个。 |
| Profile | Hermes 一套独立的配置（system prompt、session、model 等）。常见命名：`cto / pm / dev / qa / ops`。 |

---

## A. 最小可跑：OpenAI 兼容路径

> 这条路径**完全不需要本 PR 的代码**——`main` 分支早就有 `core/openai.py`。
> 适合先把链路打通、确认硬件 + 网络都对再升级到原生 backend。

### A.1 起 Hermes API Server

```bash
# 在装了 hermes-agent 的机器上
hermes config set api_server.enabled true
hermes config set api_server.host 0.0.0.0   # 让 bridge 能跨机连
hermes config set api_server.port 8642
hermes gateway start                         # 或 hermes serve
```

确认：

```bash
curl -s http://<hermes-host>:8642/v1/health
# {"status":"ok",...}

curl -s http://<hermes-host>:8642/v1/models | jq .
# 应该列出当前 active profile 暴露的 model
```

如果 Hermes 配了 `api_server.auth_token`，把 token 记下来留给 bridge 当 `api_key`。

### A.2 改 bridge 的 `config.py`

```python
"openai": {
    "base_url": "http://<hermes-host>:8642/v1",
    "api_key": "<上一步那个 token；没设就空字符串>",
    "model":   "<可选：填了就强制走某个 model；空字符串 → 由 Hermes 决定>",
    "session_key": "default",
    # 其余字段保持默认即可
},
```

把示例里的唤醒词 `小黑` 留下来——`config.py` 顶部 `before_wakeup` 钩子已经把
`"小黑"` 路由到 `"openai"`。

### A.3 启动 bridge

```bash
OPENAI_ENABLE=1 \
XIAOZHI_ENABLE=1 \
python3 main.py
# 日志看到 "[OpenAI] Enabled, base_url=..." 就说明初始化成功
```

### A.4 实际对话

| 唤醒方式 | 行为 |
|---|---|
| 喊 `你好小黑` / `小黑小黑` | 进入连续对话，每句话经 Hermes 回话 |
| 对小爱说 `让小黑 今天天气怎么样` | 一次性发送，TTS 念回 |
| 对小爱说 `召唤小黑` | 同 1，进连续对话模式 |

### A.5 常见坑

| 现象 | 原因 / 解决 |
|---|---|
| `[OpenAI] HTTP 401: ...` | `api_key` 错了，或 Hermes 没启用 auth 但 bridge 填了 token；不要乱填，对齐 Hermes 端 `api_server.auth_token` |
| `[OpenAI] HTTP 404: ...` | `base_url` 拼错——必须以 `/v1` 结尾，不要写到 `/v1/chat/completions` |
| `Connection refused` | Hermes 监听 `127.0.0.1` 但 bridge 在另一台机器；改成 `0.0.0.0` 或同机部署 |
| `[OpenAI] Timeout` | Hermes 那边 model 加载慢；把 `response_timeout` 调到 180+ |

---

## B. 升级到 native Hermes backend

> **本 PR 引入。** 走 `/v1/runs` 而不是 `/v1/chat/completions`。

### B.1 为什么要升级

| 维度 | OpenAI 兼容 (`backend: openai`) | Native Hermes (`backend: hermes`) |
|---|---|---|
| 多轮上下文 | 必须由 bridge 每轮上传 `messages` | 服务端按 `session_id` 维护 |
| 长期记忆 scope | 没有 | `X-Hermes-Session-Key` 头 |
| 运行结构 | 一次 chat completion | `run_id` + queued/running/completed 状态机 |
| Approval / Tool 进度事件 | 拿不到 | 服务端原生支持（本 PR 还没消费，但接口预留好了） |
| Model 路由 | 由 `model` 字段决定 | 默认走服务端 active profile |
| 协议稳定性 | 跟 OpenAI 走 | 跟 Hermes 走，跨大版本可能更稳 |

如果你只想"语音版 ChatGPT"，A 完全够用。
如果你要小爱触发 skill / cron / kanban，或者要给不同唤醒词分配不同 profile，用 B。

### B.2 配置

`config.py` 加 / 改 `hermes` 块（默认值如下，按需覆盖）：

```python
"hermes": {
    "base_url":   "http://<hermes-host>:8642/v1",
    "api_key":    "<auth token, 没设就空字符串>",
    "model":      "",                          # 留空 = 服务端默认
    "input_mode": "local_asr",                 # 或 "xiaoai_asr"
    "session_key": "open-xiaoai-bridge",        # 同时是 session_id + memory scope
    "server_session_id":   "",                  # 留空 → 沿用 session_key
    "memory_session_key":  "",                  # 留空 → 沿用 session_key
    "system_prompt": "",                        # 映射到 /v1/runs 的 instructions
    "send_local_history": False,                # 服务端管历史时保持 False，更便宜
    "response_timeout": 120,
    "poll_interval":    0.5,
    "tts_speaker": "xiaoai",
    # ...其他默认值见 config.py
},
```

### B.3 启动

```bash
HERMES_ENABLE=1 \
XIAOZHI_ENABLE=1 \
python3 main.py
# 日志看 "[Hermes] Enabled, base_url=..., active_profile=<default>" 就成
```

### B.4 唤醒词

| 唤醒方式 | 行为 |
|---|---|
| 喊 `你好小赫` / `小赫小赫` | 进入 Hermes 连续对话 |
| 对小爱说 `让小赫 ...` | 一次性发送，TTS 念回 |
| 对小爱说 `告诉小赫 ...` | Fire-and-forget，不等回复 |
| 对小爱说 `召唤小赫` | 进连续对话模式 |

### B.5 协议细节（写代码时备查）

`HermesManager._request_chat_completion(text)` 实际做的事：

```text
POST /v1/runs
Authorization: Bearer <api_key>
X-Hermes-Session-Key: <memory_session_key 或 session_key>
Content-Type: application/json

{
  "input":      "<用户文字>",            # send_local_history=True 时是 messages 数组
  "session_id": "<server_session_id 或 session_key>",
  "instructions": "<system_prompt>",   # 可选
  "model":      "<model 或省略>",
  "temperature": 0.7,                   # 可选
  "max_tokens":  512                    # 可选
}
→ 202 { "run_id": "run_xxx", "status": "started" }

# 然后每 poll_interval 秒一次：
GET /v1/runs/{run_id}
→ { "status": "queued"|"running"|"completed"|"failed"|..., "output": "..." }
```

`output` 命中以下任一格式都能解析：
1. 字符串：`{"output": "你好"}`
2. 消息数组：`{"output": [{"role": "assistant", "content": [{"type": "text", "text": "..."}]}]}`
3. 兜底：`response` / `final_response` / `text` 顶层字段

---

## C. 双向集成（语音 → Hermes 触发动作）

> Issue #3：希望反过来——小爱听一句话就能让 Hermes 干活。

思路：在 `before_wakeup` 钩子里把语音文字识别成"触发器 + 自然语言指令"，
然后用 `app.send_to_hermes_and_play_reply()` 把指令塞给 Hermes，
让 **Hermes 自己决定调哪个 skill**。Bridge 不做硬编码 intent 解析。

### C.1 在 `config.py` 解开示例

`config.py` 已经留好三段注释，按需放开：

```python
# 1) Home Assistant 控制
if "调到" in text or "打开" in text or "关掉" in text:
    await speaker.abort_xiaoai()
    await app.send_to_hermes_and_play_reply(
        f"使用 homeassistant-control skill 执行：{text}"
    )
    return None

# 2) 给 GitHub 提 issue
if text.startswith("提个 issue"):
    await speaker.abort_xiaoai()
    await app.send_to_hermes_and_play_reply(
        f"使用 github-issues skill 在 dickyjing/open-xiaoai-bridge 仓库新建 issue。"
        f"标题和内容由你判断：{text}"
    )
    return None

# 3) 查今天的会议
if "今晚有什么会" in text or "今天的会议" in text:
    await speaker.abort_xiaoai()
    await app.send_to_hermes_and_play_reply(
        f"使用 google-workspace skill 查我今天剩余的日历安排，按时间顺序口语化播报：{text}"
    )
    return None
```

### C.2 Hermes 那端要装的 skill

| 场景 | Hermes skill | 备注 |
|---|---|---|
| HA 控制 | `homeassistant-control`（自带）或自己的 `mcp_homeassistant_*` 工具 | 端到端走 MCP/REST |
| GitHub | `github-issues` | 用 fine-grained PAT，给 `Issues: write` 权限 |
| 日历 | `google-workspace` | OAuth 装好 `gws` CLI |

### C.3 验收（issue #3 验收标准）

| 场景 | 测试句子 | 预期 |
|---|---|---|
| HA | "让小赫 把客厅空调调到 24 度" | 5 秒内调到 24°，TTS 念回"客厅空调已调到 24 度" |
| GitHub | "让小赫 提个 issue：bridge 启动慢" | 10 秒内 issue 创建好，TTS 念回 issue 编号 |
| 日历 | "让小赫 今晚有什么会议" | 5 秒内 TTS 念出今晚日程 |

如果延迟超过 5 秒：
- 检查 Hermes 端是否在加载 model（首次 cold start 会慢）
- 把 `tts.doubao.stream` 设为 `True`，TTS 边合成边播

---

## D. 多 Agent 路由（唤醒词 → Hermes profile）

> Issue #4。让 5 个唤醒词路由到 5 个不同 system prompt / session 的"角色"。

### D.1 在 `config.py` 配 profiles

`hermes.profiles` 默认是空 dict。打开注释、按需改：

```python
"profiles": {
    "cto": {"system_prompt": "你是 CTO，技术决策果断、回话简短。",
            "session_key":   "open-xiaoai-bridge:cto"},
    "pm":  {"system_prompt": "你是 PM，关注需求拆解和优先级。",
            "session_key":   "open-xiaoai-bridge:pm"},
    "dev": {"system_prompt": "你是 Dev，写代码细节、给出可运行片段。",
            "session_key":   "open-xiaoai-bridge:dev"},
    "qa":  {"system_prompt": "你是 QA，关注边界条件、回归测试。",
            "session_key":   "open-xiaoai-bridge:qa"},
    "ops": {"system_prompt": "你是 Ops，运维和故障排查思路。",
            "session_key":   "open-xiaoai-bridge:ops"},
},
```

每个 profile 可覆盖：`base_url` / `api_key` / `model` /
`session_key` / `server_session_id` / `memory_session_key` /
`system_prompt` / `temperature` / `max_tokens` / `tts_speaker`。
**没填的字段自动 fallback 到 `hermes` 默认配置**。

### D.2 在 `before_wakeup` 钩子里路由

`config.py` 已经留好示例（解开注释）：

```python
HERMES_PROFILE_KEYWORDS = {
    "老板": "cto",
    "产品": "pm",
    "开发": "dev",
    "测试": "qa",
    "运维": "ops",
}
for keyword, profile in HERMES_PROFILE_KEYWORDS.items():
    if keyword in text:
        if app.set_hermes_profile(profile):
            await speaker.play(text=f"{keyword}来了")
            return "hermes"
```

### D.3 切换怎么做到 < 200ms

`set_hermes_profile()` 是**纯进程内 class 属性 swap**——不发起任何 HTTP 调用，
也不重连 session，实测 <10ms。下一次 `POST /v1/runs` 才会带上新的
`session_id` + `X-Hermes-Session-Key`，让 Hermes 服务端按对应 profile 跑。

### D.4 验收（issue #4 验收标准）

| 测试 | 预期 |
|---|---|
| 喊 `老板` | profile 切到 `cto`；问"你是谁" → "我是 CTO ..." |
| 喊 `运维` | profile 切到 `ops`；问"你是谁" → "我是 Ops ..." |
| 切换响应时延 | profile swap < 10ms；端到端 TTS < 200ms（不算 Hermes 出 token 的时间） |

### D.5 与"动态 session_key"的区别

| 方式 | 适用 |
|---|---|
| `app.set_hermes_session_key("xxx")` | 同一个 profile，但要拆"上下文线" |
| `app.set_hermes_profile("cto")` | 切到完全不同的 system prompt / model / 会话 scope |
| 二者叠加 | profile 切完后再调一次 `set_hermes_session_key()`，比如每个用户独立线 |

---

## E. 故障排查

### E.1 日志在哪

| 日志 | 路径 | 看什么 |
|---|---|---|
| Bridge | 控制台 / `journalctl -u open-xiaoai-bridge` | `[OpenAI]` / `[Hermes]` / `[OpenAI Conv]` / `[Hermes Conv]` 前缀 |
| Hermes | `~/.hermes/logs/agent.log`、`gateway.log` | `hermes logs --follow --level WARNING` 一键流式看 |

### E.2 网络连通性自查脚本

```bash
HERMES=http://10.1.1.117:8642
TOKEN="<auth token 或留空>"

# 1) 健康
curl -fsS "$HERMES/v1/health" | jq .

# 2) 模型列表
curl -fsS -H "Authorization: Bearer $TOKEN" "$HERMES/v1/models" | jq '.data[].id'

# 3) Native /v1/runs 端到端冒烟
curl -fsS -X POST "$HERMES/v1/runs" \
     -H "Authorization: Bearer $TOKEN" \
     -H "X-Hermes-Session-Key: smoke-test" \
     -H 'Content-Type: application/json' \
     -d '{"input":"hi"}' | jq .

# 4) /v1/chat/completions 兼容
curl -fsS -X POST "$HERMES/v1/chat/completions" \
     -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"model":"","messages":[{"role":"user","content":"hi"}]}' | jq .
```

### E.3 常见 4xx / 5xx 与对策

| 状态 | 在哪看到 | 通常原因 | 对策 |
|---|---|---|---|
| 400 | `[Hermes] Run failed: HTTP 400: Missing 'input' field` | `_send_local_history=True` 但 history 为空 | 把第一句直接发出去；或保持 `False` |
| 401 | `HTTP 401: Invalid token` | `api_key` 错了 | 对齐 Hermes 端 `api_server.auth_token` |
| 404 | `HTTP 404 polling run xxx` | Run TTL 过了 / Hermes 重启 | 把 `poll_interval` 调小到 0.2，及时拿结果 |
| 429 | `Too many concurrent runs` | 同时 ≥ 8 路并发 | 不要在 `for` 循环里 `send_to_hermes`，用 `wait_response=True` 串行 |
| 500 | Bridge 重连日志一堆 | Hermes 端 model crash | 看 Hermes `errors.log`，常见是 OOM / API key 配额 |
| Timeout | `Hermes run xxx did not finish within 120s` | 服务端 model cold start 或 skill 跑太久 | `response_timeout` 调到 180+；冷启动加 `hermes warmup` |

### E.4 验证某个 profile 是否真的切过去了

启用 debug 日志后，每次 `set_hermes_profile()` 都会打：

```
[Hermes] Profile switched to 'cto': base_url=..., model=..., session_key=open-xiaoai-bridge:cto
```

下一条 `[Hermes(open-xiaoai-bridge:cto)] User speech: ...` 应该带上对应 session_key。
如果没切过去，多半是 profile 名拼错——`config.py` 里和 `set_hermes_profile()` 调用要一致。

---

## F. 进一步定制

| 想做什么 | 改哪 |
|---|---|
| 加新唤醒词 | `config.py` → `wakeup.keywords` 加一行；在 `before_wakeup` 处理路由 |
| 不同人不同 session | `app.set_hermes_session_key("user:dickyjing")` |
| 强制走某个 model | `hermes.model` 或 `profiles.<name>.model` |
| 流式 TTS | `tts.doubao.stream = True` + `audio_format = "pcm"` |
| 用小爱原生 ASR 不依赖本地 SherpaASR | `hermes.input_mode = "xiaoai_asr"`（实验性） |
| 把 audio 也通过 Hermes 跑（不仅仅是文字） | 需要扩展 Hermes API Server 的 `/v1/audio/*`——本仓库目前不支持 |
