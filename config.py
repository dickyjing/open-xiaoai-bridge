import asyncio
import socket
import subprocess
import sys
import time
import uuid


# 每次唤醒生成独立 Session，对话互不干扰
# 适合以下场景：
#   - "提问 → 回答"式交互，不需要 Agent 记住上下文
#   - 长期使用同一 Session 导致 Agent 上下文窗口堆积过长，影响响应质量和速度
# def new_session_key():
#     return f"agent:main:session-{uuid.uuid4().hex[:8]}"


_led_anim_task = None  # 当前动画任务


_LED_WAKE_L = 3  # ledd 内建：L=3 静态白色（唤醒待命用）


async def _led_clear(speaker):
    """通过 ubus shut 弹出唤醒态 LED（必须传 L，否则 ledd 不弹栈）。"""
    try:
        await speaker.run_shell(
            f"ubus -t 1 call led shut '{{\"L\":{_LED_WAKE_L}}}' 2>/dev/null; "
            f"echo 255 > /sys/devices/i2c-2/2-0034/led_gcc 2>/dev/null; true",
            timeout=1500,
        )
    except Exception:
        pass


async def _led_breath_loop(speaker):
    """通过 ubus 让 ledd 显示静态白色（L=3，小爱原生唤醒效果）。

    ubus call led show 仅接受 {L, pos}，不支持 rgb 参数，颜色由 ledd 内建 L 表决定。
    任务被 cancel/异常时，finally 会自动 shut 同一 L 让 ledd 回到默认状态。
    """
    import asyncio
    try:
        # 让 ledd 压栈显示 L=3 静态白色
        await speaker.run_shell(
            f"ubus -t 1 call led show '{{\"L\":{_LED_WAKE_L}}}' 2>/dev/null; true",
            timeout=1500,
        )
        # 保持任务存活，直到被 cancel
        while True:
            await asyncio.sleep(3600)
    finally:
        # 无论正常退出、cancel、异常，都让 ledd 退出自定义状态
        try:
            await _led_clear(speaker)
        except Exception:
            pass


async def _led_start_anim(speaker):
    """启动 LED 自定义白色（已有则取消）。"""
    import asyncio
    global _led_anim_task
    if _led_anim_task and not _led_anim_task.done():
        _led_anim_task.cancel()
    _led_anim_task = asyncio.create_task(_led_breath_loop(speaker))


async def _led_stop_anim(speaker):
    """停止 LED 自定义状态（清理由 _led_breath_loop.finally 负责）。"""
    global _led_anim_task
    if _led_anim_task and not _led_anim_task.done():
        _led_anim_task.cancel()
        try:
            await _led_anim_task
        except Exception:
            pass
    _led_anim_task = None


def cancel_led_anim_sync(loop):
    """同步取消 LED 任务（供 on_interrupt 等同步回调使用）。

    清理逻辑由 _led_breath_loop 的 finally 块负责（包括 ubus shut）。
    """
    global _led_anim_task
    if _led_anim_task and not _led_anim_task.done():
        loop.call_soon_threadsafe(_led_anim_task.cancel)


async def _set_led_color(speaker, rgb_hex: str):
    """点亮 OH2P 12 颗 LED 为指定颜色（RGB 24bit）。rgb_hex='000000' 即熄灭。"""
    cmds = " ; ".join(
        f"echo '{i} 0x{rgb_hex}' > /sys/devices/i2c-2/2-0034/led_rgb"
        for i in range(12)
    )
    try:
        from core.utils.logger import logger
        logger.info(f"[LED] _set_led_color called rgb={rgb_hex}", module="LED")
        res = await speaker.run_shell(cmds, timeout=2000)
        logger.info(f"[LED] run_shell result: exit_code={getattr(res, 'exit_code', '?')}, stdout={getattr(res, 'stdout', '')[:100]!r}, stderr={getattr(res, 'stderr', '')[:100]!r}", module="LED")
    except Exception as e:
        from core.utils.logger import logger
        logger.error(f"[LED] error: {e}", module="LED")


async def before_wakeup(speaker, text, source, app):
    """
    处理收到的用户消息，并决定是否唤醒 AI。

    参数：
        speaker : SpeakerManager，可调用 play/abort_xiaoai/wake_up 等方法
        text    : 识别到的文字内容
        source  : 唤醒来源
                    'kws'    — 本地关键词唤醒（用户说了唤醒词）
                    'xiaoai' — 小爱同学收到用户语音指令
        app     : MainApp 实例，可调用 send_to_openclaw / send_to_openai 等方法

    返回值：
        "openclaw" — 进入 OpenClaw 连续对话流程
        "openai"   — 进入 OpenAI 兼容服务连续对话流程（例如 Hermes Agent API Server）
        "hermes"   — 进入 Hermes Agent 原生 API 连续对话流程（/v1/runs）
        "xiaozhi"  — 进入小智 AI 流程
        None       — 不做额外处理（可在此自行调用 app.send_to_openclaw 等）

    ---
    动态切换 session_key：
        每次进入此函数前，框架会自动将 session_key 重置为配置文件中的默认值。
        如需路由到其他 Agent，在 return "openclaw" 之前调用：
            app.set_openclaw_session_key("agent:main:xxx")
        不调用则自动使用 openclaw.session_key 的默认值，无需手动重置。
    """
    if source == "kws":
        # --- 示例一：按唤醒词路由到不同 Agent ---
        # AGENT_SESSIONS = {
        #     "龙虾": "agent:assistant:open-xiaoai-bridge",  # 说"你好龙虾" → 路由到 assistant Agent
        #     "小美": "agent:xiaomei:open-xiaoai-bridge",    # 说"你好小美" → 路由到 xiaomei Agent
        #     "管家": "agent:butler:open-xiaoai-bridge",     # 说"你好管家" → 路由到 butler Agent
        # }
        # for keyword, session_key in AGENT_SESSIONS.items():
        #     if keyword in text:
        #         app.set_openclaw_session_key(session_key)
        #         await speaker.play(text=f"{keyword}来了")
        #         return "openclaw"

        # --- 示例二：每次唤醒生成独立 Session ---
        # if "龙虾" in text:
        #     app.set_openclaw_session_key(new_session_key())
        #     await speaker.play(text="龙虾来了")
        #     return "openclaw"

        # --- 示例三：进入 OpenClaw 前播放服务端本地开场白 ---
        # if "龙虾" in text:
        #     await speaker.play(server_file="/path/to/openclaw_intro.wav")
        #     return "openclaw"

        # Route to OpenClaw Agent by wake word
        if "龙虾" in text:
            await speaker.play(text="龙虾来了")
            return "openclaw"

        if "小黑" in text:
            await speaker.play(text="小黑来了")
            return "openai"

        if "召唤小赫" in text:
            await _led_start_anim(speaker)  # 青色循环动画
            await speaker.play(text="小赫来了")
            return "hermes"

        # --- 多 Agent 路由示例（issue #4）：唤醒词 → Hermes profile ---
        # 启用前先在 hermes.profiles 中配置对应 profile，并把 enable_hermes 打开。
        # HERMES_PROFILE_KEYWORDS = {
        #     "老板": "cto",
        #     "产品": "pm",
        #     "开发": "dev",
        #     "测试": "qa",
        #     "运维": "ops",
        # }
        # for keyword, profile in HERMES_PROFILE_KEYWORDS.items():
        #     if keyword in text:
        #         if app.set_hermes_profile(profile):
        #             await speaker.play(text=f"{keyword}来了")
        #             return "hermes"

        if "小智" in text:
            await speaker.play(text="小智来了")
            return "xiaozhi"

        return None

    if source == "xiaoai":
        # --- 示例四：小爱指令按用户名路由到不同 Session ---
        # if text == "召唤小美":
        #     app.set_openclaw_session_key("agent:xiaomei:open-xiaoai-bridge")
        #     await speaker.abort_xiaoai()
        #     return "openclaw"

        if text == "召唤龙虾":
            await speaker.abort_xiaoai()
            return "openclaw"  # OpenClaw continuous conversation

        if text == "召唤小黑":
            await speaker.abort_xiaoai()
            return "openai"  # OpenAI-compatible service continuous conversation

        if "召唤小赫" in text or "小赫" in text:
            await speaker.abort_xiaoai()
            await _set_led_color(speaker, "FF0000")  # 蓝色：监听中（bits16-23=BLUE）
            return "hermes"  # Hermes Agent native /v1/runs continuous conversation

        if text == "召唤小智":
            await speaker.abort_xiaoai()
            return "xiaozhi"  # XiaoZhi AI

        if "让龙虾" in text:
            await speaker.abort_xiaoai()
            # One-shot: send to OpenClaw and play the reply via TTS
            await app.send_to_openclaw_and_play_reply(text.replace("让龙虾", ""))
            return None  # No further handling by the framework

        if "告诉龙虾" in text:
            await speaker.abort_xiaoai()
            # Fire-and-forget: let the Agent decide when/how to reply
            await app.send_to_openclaw(text.replace("告诉龙虾", ""))
            return None

        if "让小黑" in text:
            await speaker.abort_xiaoai()
            await app.send_to_openai_and_play_reply(text.replace("让小黑", ""))
            return None

        if "让小赫" in text:
            await speaker.abort_xiaoai()
            await app.send_to_hermes_and_play_reply(text.replace("让小赫", ""))
            return None

        if "告诉小赫" in text:
            await speaker.abort_xiaoai()
            await app.send_to_hermes(text.replace("告诉小赫", ""))
            return None

        # --- 双向集成示例（issue #3）：小爱语音 → Hermes 触发具体动作 ---
        # 这里的提示词只是触发器，真正的实现交给 Hermes Agent 的 skill / tool
        # 来调度（例如 homeassistant-control / github-issues / google-workspace）。
        # 默认注释掉，避免和上面的 Demo 唤醒词冲突；真实部署时按需放开。
        #
        # # 1) Home Assistant 控制
        # if "调到" in text or "打开" in text or "关掉" in text:
        #     await speaker.abort_xiaoai()
        #     await app.send_to_hermes_and_play_reply(
        #         f"使用 homeassistant-control skill 执行：{text}"
        #     )
        #     return None
        #
        # # 2) 给 GitHub 提 issue
        # if text.startswith("提个 issue"):
        #     await speaker.abort_xiaoai()
        #     await app.send_to_hermes_and_play_reply(
        #         f"使用 github-issues skill 在 dickyjing/open-xiaoai-bridge 仓库新建 issue。"
        #         f"标题和内容由你判断：{text}"
        #     )
        #     return None
        #
        # # 3) 查今天的会议
        # if "今晚有什么会" in text or "今天的会议" in text:
        #     await speaker.abort_xiaoai()
        #     await app.send_to_hermes_and_play_reply(
        #         f"使用 google-workspace skill 查我今天剩余的日历安排，"
        #         f"按时间顺序口语化播报：{text}"
        #     )
        #     return None


async def after_wakeup(speaker, source=None, session_key=None):
    """
    退出唤醒状态

    - source: 退出来源
        - 'xiaozhi': 小智对话超时退出
        - 'openclaw': OpenClaw 连续对话退出
        - 'openai': OpenAI 兼容服务连续对话退出
    - session_key: 当前 OpenClaw/OpenAI 后端 session_key
        可据此区分是哪个 Agent 退出，例如播放不同的退出提示语
    """
    if source == "openclaw":
        # 示例：退出 OpenClaw 时播放服务端本地结束语
        # await speaker.play(server_file="/path/to/openclaw_bye.wav")

        # 示例：按 agentId 区分退出提示语
        # session_key 格式：agent:<agentId>:<rest>，第二段即 agentId
        # agent_id = session_key.split(":")[1] if session_key else None
        # if agent_id == "assistant":
        #     await speaker.play(text="助手，再见")
        # elif agent_id == "xiaomei":
        #     await speaker.play(text="小美，再见")
        # else:
        #     await speaker.play(text="再见")
        await speaker.play(text="龙虾，再见")
    if source == "openai":
        await speaker.play(text="小黑，再见")
    if source == "hermes":
        await _led_stop_anim(speaker)  # 停止动画并熄灭
        await speaker.play(text="小赫，再见")
    if source == "xiaozhi":
        await speaker.play(text="小智，再见")

APP_CONFIG = {
    "wakeup": {
        # 自定义唤醒词列表（英文字母要全小写）
        "keywords": [
            "你好小智",
            "小智小智",
            "hi open claw",
            "你好龙虾",
            "龙虾你好",
            "你好小黑",
            "小黑你好",
            "召唤小赫",
            "你好小赫",
            "小赫你好",
        ],
        # 静音多久后自动退出唤醒（秒）
        "timeout": 20,
        # 语音识别结果回调
        "before_wakeup": before_wakeup,
        # 退出唤醒时的提示语（设置为空可关闭）
        "after_wakeup": after_wakeup,
    },
    "kws": {
        # 唤醒词置信度加成（越高越难误触发，越低越灵敏）
        "keywords_score": 2.0,
        # 唤醒词检测阈值（越低越灵敏，越高越难触发）
        "keywords_threshold": 0.2,
        # 唤醒词检测时的最小静默时长（ms），静默超过该时长则判定为说完
        "min_silence_duration": 480,
    },
    "vad": {
        # 语音检测阈值（0-1，越小越灵敏）
        "threshold": 0.10,
        # 最小语音时长（ms）
        "min_speech_duration": 250,
        # 最小静默时长（ms）
        "min_silence_duration": 500,
    },
    "audio_input": {
        # Input gain multiplier before VAD/KWS/ASR. Use 1.0 to disable.
        "gain": 1.0,
    },
    "asr": {
        # 支持 "sense_voice"（默认）、"paraformer"、"fire_red_asr" 或 "doubao"
        "model": "sense_voice",
        # 是否优先使用 INT8 量化模型（仅本地模型生效）
        "int8": True,
        # 可选：显式指定 core/models/ 下的模型目录名（仅本地模型生效）
        # "model_dir": "",
        "doubao": {
            # "standard": 录音文件识别标准版，调用 /submit + /query
            # "flash": 录音文件极速版，调用 /recognize/flash
            "mode": "standard",
            "app_key": "你的 App Key",
            "access_key": "你的 Access Key",
            # 火山 X-Api-Resource-Id：
            # standard 可选：
            #   "volc.bigasr.auc"  - 豆包录音文件识别模型 1.0
            #   "volc.seedasr.auc" - 豆包录音文件识别模型 2.0
            # flash 可选：
            #   "volc.bigasr.auc_turbo" - 录音文件极速版
            "resource_id": "volc.seedasr.auc",
            "language": "",
            "submit_timeout": 10,
            "query_timeout": 10,
            "poll_interval": 0.5,
            "max_wait_seconds": 20,
        },
    },
    "xiaozhi": {
        "OTA_URL": "http://127.0.0.1:8003/xiaozhi/ota/",
        "WEBSOCKET_URL": "ws://127.0.0.1:8000/xiaozhi/v1/",
        "WEBSOCKET_ACCESS_TOKEN": "", #（可选）一般用不到这个值
        "DEVICE_ID": "0a:b9:bc:1e:83:18", #（可选）默认自动生成
        "VERIFICATION_CODE": "", # 首次登陆时，验证码会在这里更新
    },
    "xiaoai": {
        "continuous_conversation_mode": True,
        "exit_command_keywords": ["停止", "退下", "退出", "下去吧"],
        "max_listening_retries": 2,  # 最多连续重新唤醒次数
        "exit_prompt": "再见，主人",
        "continuous_conversation_keywords": ["开启连续对话", "启动连续对话", "我想跟你聊天"]
    },
    # TTS (Text-to-Speech) Configuration
    "tts": {
        "doubao": {
            # 豆包语音合成 API 配置
            # 文档地址: https://www.volcengine.com/docs/6561/1598757?lang=zh
            # 产品地址: https://www.volcengine.com/docs/6561/1871062
            "app_id": "xxxx",         # 你的 App ID
            "access_key": "xxxxxx",       # 你的 Access Key
            "default_speaker": "zh_female_vv_uranus_bigtts",  # 音色 https://www.volcengine.com/docs/6561/1257544?lang=zh
            "audio_format": "pcm",  # 推荐默认值：局域网稳定环境下首音更快、播放更顺
            "stream": True,  # 推荐默认值：边合成边播放，首音延迟更低
        }
    },
    # OpenClaw Configuration
    "openclaw": {
        "url": "ws://127.0.0.1:18789",  # OpenClaw WebSocket 地址
        "token": "your_openclaw_token",  # OpenClaw 认证令牌
        # 输入模式：
        #   - "local_asr": 现有链路，使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 实验链路，唤醒小爱后接管原生 ASR 结果给 OpenClaw
        "input_mode": "local_asr",
        # session_key 格式：agent:<agentId>:<rest>
        #   agentId: OpenClaw 中配置的 Agent ID（默认为 main）
        #   rest:    会话标识，可自由命名，用于区分不同来源/场景 （默认为 open-xiaoai-bridge)
        # 也可在运行时动态切换（下一条消息即刻生效，无需重连）：
        #   app.set_openclaw_session_key("agent:assistant:open-xiaoai-bridge")
        "session_key": "agent:main:open-xiaoai-bridge",
        "identity_path": "/app/openclaw/identity/device.json",  # 设备身份文件路径；容器部署时建议挂载持久化目录
        "tts_speed": 1.0,  # TTS 语速 (0.5-2.0)，仅豆包 TTS 生效，小爱原生 TTS 不支持调速
        "tts_speaker": "xiaoai",  # "xiaoai" = 小爱原生 TTS；填豆包音色 ID 则用豆包 TTS；不设置则使用 tts.doubao.default_speaker
        # 可按 agentId 单独覆盖音色，优先级高于 tts_speaker
        # agentId 来自 session_key，格式为：agent:<agentId>:<rest>
        # 示例：
        # "agent_tts_speakers": {
        #     "assistant": "zh_female_vv_uranus_bigtts",
        #     "xiaomei": "zh_female_shuangkuaisisi_moon_bigtts",
        #     "butler": "xiaoai",
        # },
        "agent_tts_speakers": {},
        "response_timeout": 120,  # 等待 OpenClaw agent 响应的超时时间（秒）
        "exit_keywords": ["退出", "停止", "再见"],  # 退出连续对话的关键词
        # rule_prompt: 用于「自动播放」和「连续对话」场景
        #   - send_to_openclaw_and_play_reply() 会自动追加
        #   - OpenClawConversationController 会自动追加
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        # rule_prompt_for_skill: 用于「Agent 自主播报」场景（方式三）
        #   - send_to_openclaw() 会自动追加
        #   - 告诉 Agent 需要调用 xiaoai-tts skill 来播报，因为服务端不会自动播放
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送的，他看不到你回复的文字，调用 `xiaoai-tts` skill 播报出来。字数控制在300字以内"
    },
    # OpenAI-compatible Service Configuration
    # 可接入 Hermes Agent API Server、OpenAI、Ollama、LM Studio 等兼容 /v1/chat/completions 的服务
    "openai": {
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "xiaoai-bridge-key",
        "model": "gpt-4o-mini",
        # 输入模式：
        #   - "local_asr": 使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 接管小爱原生 ASR 结果
        "input_mode": "local_asr",
        "session_key": "default",
        "system_prompt": "",
        "temperature": 0.7,
        "max_tokens": 512,
        "history_max_messages": 20,
        "response_timeout": 120,
        "tts_speed": 1.0,
        "tts_speaker": "xiaoai",
        "session_tts_speakers": {},
        "exit_keywords": ["退出", "停止", "再见"],
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送的，他看不到你回复的文字。字数控制在300字以内",
        "extra_body": {},
    },
    # Hermes Agent native backend configuration
    # 走 Hermes Agent API Server 的原生 /v1/runs 接口（不是 OpenAI 兼容路径）
    # 优势：服务端管理 session（不必每轮上传历史）+ 长期记忆 scope（X-Hermes-Session-Key）
    # 文档：https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server
    "hermes": {
        # Hermes API Server 的 /v1 根 URL
        "base_url": "http://127.0.0.1:8642/v1",
        # 鉴权 token（如果 API Server 启用了 auth）
        "api_key": "xiaoai-bridge-key",
        # 留空 → 使用服务端配置的默认 model
        "model": "",
        # 输入模式：
        #   - "local_asr": 使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 接管小爱原生 ASR 结果
        "input_mode": "local_asr",
        # session_key 同时作为：
        #   1) Hermes 的 session_id（决定服务端会话线程，影响多轮上下文）
        #   2) X-Hermes-Session-Key 的默认值（决定长期记忆 scope）
        "session_key": "open-xiaoai-bridge",
        # 留空表示沿用 session_key；填值则覆盖
        "server_session_id": "",
        "memory_session_key": "",
        # 系统 prompt（映射到 Hermes 的 instructions 字段）
        "system_prompt": "",
        "temperature": 0.7,
        "max_tokens": 512,
        # 服务端管理历史时本地不必再发；置 True 则改为每轮带上 conversation_history（无状态模式）
        "send_local_history": False,
        # 当 send_local_history=True 时，最多保留多少条历史
        "history_max_messages": 20,
        # 总等待 run 完成的上限（秒）
        "response_timeout": 120,
        # 轮询 GET /v1/runs/{run_id} 的间隔（秒）
        "poll_interval": 0.5,
        "tts_speed": 1.0,
        "tts_speaker": "xiaoai",
        "session_tts_speakers": {},
        "exit_keywords": ["退出", "停止", "再见", "退下", "下去吧", "你退下", "小赫退下", "好了你退下", "没事了"],
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送的，他看不到你回复的文字。字数控制在300字以内",
        "extra_body": {},
        # 多 Agent 路由（issue #4）：唤醒词 → Hermes profile
        # 每个 profile 可覆盖默认配置的 base_url / api_key / model /
        # session_key / server_session_id / memory_session_key /
        # system_prompt / temperature / max_tokens / tts_speaker。
        # 切换由 before_wakeup 钩子调用 app.set_hermes_profile(name) 触发，
        # 默认走 in-process 状态切换，无需额外网络握手，<10ms。
        # 示例（启用前请把 system_prompt / session_key 调成你的实际值）：
        # "profiles": {
        #     "cto":  {"system_prompt": "你是 CTO Agent，技术决策果断、回话简短。",
        #              "session_key":   "open-xiaoai-bridge:cto"},
        #     "pm":   {"system_prompt": "你是 PM Agent，关注需求拆解和优先级。",
        #              "session_key":   "open-xiaoai-bridge:pm"},
        #     "dev":  {"system_prompt": "你是 Dev Agent，写代码细节、给出可运行片段。",
        #              "session_key":   "open-xiaoai-bridge:dev"},
        #     "qa":   {"system_prompt": "你是 QA Agent，关注边界条件、回归测试。",
        #              "session_key":   "open-xiaoai-bridge:qa"},
        #     "ops":  {"system_prompt": "你是 Ops Agent，运维和故障排查思路。",
        #              "session_key":   "open-xiaoai-bridge:ops"},
        # },
        "profiles": {},
    },
}
