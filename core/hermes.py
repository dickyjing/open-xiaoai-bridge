"""Hermes Agent native backend manager.

Talks to a Hermes Agent API Server using its **native** endpoints
(``/v1/runs``) instead of the OpenAI-compatible Chat Completions path.

Why native instead of the OpenAI compat path:
  * Server-side session management (no need to ship history every turn)
  * Long-term memory scoping via the ``X-Hermes-Session-Key`` header
  * Structured run lifecycle (``run_id``, ``status``, ``output``, ``usage``)
  * Future-friendly: approval events, tool progress, SSE streaming

Reference:
  https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server
  gateway/platforms/api_server.py — POST /v1/runs, GET /v1/runs/{run_id}
"""

import asyncio
import uuid
from typing import Any

import aiohttp

from core.openai import OpenAIManager
from core.utils.base import get_env
from core.utils.config import ConfigManager
from core.utils.logger import logger


class HermesManager(OpenAIManager):
    """Manager for Hermes Agent native API backend.

    Inherits all the conversation/TTS/queueing machinery from
    :class:`OpenAIManager` but overrides:
      * config loading (``hermes`` config block)
      * the actual HTTP call (``_request_chat_completion``) to use
        ``POST /v1/runs`` + polling.

    All class-level state is re-declared so this manager does *not*
    share ``_sessions`` / ``_response_events`` etc. with ``OpenAIManager``.
    """

    # ------------------------------------------------------------------
    # Re-declare every class attribute that OpenAIManager defines so
    # the two managers maintain independent state. (Without this, both
    # subclasses would mutate the same dicts on the parent class.)
    # ------------------------------------------------------------------
    _initialized = False
    _reload_listener_registered = False
    _enabled = False
    _base_url = "http://127.0.0.1:8642/v1"
    _api_key = ""
    _model = ""  # empty → server picks default
    _session_key = "open-xiaoai-bridge"
    _system_prompt = ""
    _temperature: float | None = None
    _max_tokens: int | None = None
    _timeout = 120
    _history_max_messages = 20
    _extra_body: dict[str, Any] = {}
    _tts_speaker = None
    _session_tts_speakers: dict[str, str] = {}
    _tts_speed = 1.0
    _rule_prompt = ""
    _rule_prompt_for_skill = ""
    _sessions: dict[str, list[dict[str, str]]] = {}
    _response_events: dict[str, asyncio.Future] = {}
    _response_texts: dict[str, str] = {}
    _response_tts_speakers: dict[str, str | None] = {}
    last_error: str | None = None

    # ------------------------------------------------------------------
    # Hermes-specific configuration
    # ------------------------------------------------------------------
    # Polling interval (seconds) when waiting for a run to complete.
    _poll_interval: float = 0.5
    # When True, send local conversation_history with each request and let
    # the server treat each call as stateless. When False (default), rely on
    # server-side session_id continuity (cheaper, recommended).
    _send_local_history: bool = False
    # Optional X-Hermes-Session-Key — scopes long-term memory on the server.
    # When unset, falls back to ``_session_key``.
    _memory_session_key: str = ""
    # Hermes server's own ``session_id`` for chat continuity. Defaults to
    # ``_session_key`` so each logical conversation is a stable thread on
    # the server.
    _server_session_id: str = ""

    # ------------------------------------------------------------------
    # Multi-profile routing (issue #4)
    # ------------------------------------------------------------------
    # Snapshot of the *default* settings loaded from config — kept so
    # ``set_profile()`` can fall back to it when a profile leaves a
    # field unspecified, and ``reset_profile()`` can restore everything.
    _default_settings: dict[str, Any] = {}
    # Profiles keyed by name (e.g. {"cto": {...}, "ops": {...}}).
    _profiles: dict[str, dict[str, Any]] = {}
    # Currently active profile name, or "" when on the default config.
    _active_profile: str = ""

    @classmethod
    def initialize_from_config(cls, enabled: bool | None = None):
        logger.info("[Hermes] Initializing from config...")
        cls.reload_from_config(enabled=enabled)
        cls._initialized = True

    @classmethod
    def reload_from_config(cls, enabled: bool | None = None):
        """Refresh Hermes settings from ``config.py`` ``hermes`` block."""
        config_manager = ConfigManager.instance()
        if not cls._reload_listener_registered:
            config_manager.add_reload_listener(
                lambda _old, _new: cls.reload_from_config()
            )
            cls._reload_listener_registered = True

        config = config_manager.get_app_config("hermes", {})

        if enabled is not None:
            cls._enabled = enabled
        else:
            env_enabled = get_env("HERMES_ENABLE")
            cls._enabled = (
                env_enabled.lower() in ("1", "true", "yes")
                if env_enabled is not None
                else False
            )

        cls._base_url = str(
            config.get("base_url", "http://127.0.0.1:8642/v1")
        ).rstrip("/")
        cls._api_key = str(config.get("api_key", "") or "")
        cls._model = str(config.get("model", "") or "")
        cls._session_key = str(config.get("session_key", "open-xiaoai-bridge"))
        cls._system_prompt = str(config.get("system_prompt", "") or "")
        cls._timeout = int(config.get("response_timeout", 120))
        cls._history_max_messages = max(
            0, int(config.get("history_max_messages", 20))
        )
        cls._temperature = cls._optional_float(config.get("temperature"))
        cls._max_tokens = cls._optional_int(config.get("max_tokens"))
        cls._extra_body = config.get("extra_body", {})
        if not isinstance(cls._extra_body, dict):
            cls._extra_body = {}
        cls._tts_speaker = config.get("tts_speaker", None)
        cls._session_tts_speakers = (
            {
                str(key): str(value)
                for key, value in config.get("session_tts_speakers", {}).items()
                if key and value
            }
            if isinstance(config.get("session_tts_speakers", {}), dict)
            else {}
        )
        cls._tts_speed = float(config.get("tts_speed", 1.0))
        cls._rule_prompt = str(config.get("rule_prompt", "") or "")
        cls._rule_prompt_for_skill = str(
            config.get("rule_prompt_for_skill", "") or ""
        )

        cls._poll_interval = float(config.get("poll_interval", 0.5))
        cls._send_local_history = bool(config.get("send_local_history", False))
        cls._memory_session_key = str(config.get("memory_session_key", "") or "")
        cls._server_session_id = str(config.get("server_session_id", "") or "")

        # Load profiles map (e.g. {"cto": {"base_url": ..., "system_prompt": ...}})
        raw_profiles = config.get("profiles", {})
        cls._profiles = (
            {str(k): dict(v) for k, v in raw_profiles.items() if isinstance(v, dict)}
            if isinstance(raw_profiles, dict)
            else {}
        )
        # Snapshot defaults so set_profile() can layer profile overrides on top.
        cls._default_settings = {
            "base_url": cls._base_url,
            "api_key": cls._api_key,
            "model": cls._model,
            "session_key": cls._session_key,
            "server_session_id": cls._server_session_id,
            "memory_session_key": cls._memory_session_key,
            "system_prompt": cls._system_prompt,
            "temperature": cls._temperature,
            "max_tokens": cls._max_tokens,
            "tts_speaker": cls._tts_speaker,
        }
        # If a profile was active before reload, re-apply it on top of the
        # fresh defaults so hot-reload doesn't silently drop the override.
        if cls._active_profile:
            cls.set_profile(cls._active_profile, log=False)

        if cls._enabled:
            logger.info(
                f"[Hermes] Enabled, base_url={cls._base_url}, "
                f"model={cls._model or '<server default>'}, "
                f"session_key={cls._session_key}, "
                f"server_session_id={cls._server_session_id or cls._session_key}, "
                f"send_local_history={cls._send_local_history}, "
                f"profiles={list(cls._profiles)}, "
                f"active_profile={cls._active_profile or '<default>'}"
            )

    # ------------------------------------------------------------------
    # Profile routing
    # ------------------------------------------------------------------
    @classmethod
    def list_profiles(cls) -> list[str]:
        """Return the list of configured profile names."""
        if not cls._initialized:
            cls.initialize_from_config()
        return sorted(cls._profiles)

    @classmethod
    def active_profile(cls) -> str:
        """Return the currently active profile name (``""`` = default)."""
        return cls._active_profile

    @classmethod
    def set_profile(cls, profile: str, *, log: bool = True) -> bool:
        """Switch to ``profile`` — overlay its settings on top of the
        default config snapshot.

        Returns ``True`` when the profile exists and is now active,
        ``False`` when the profile is unknown (active state unchanged).

        Profile overlays may set: ``base_url``, ``api_key``, ``model``,
        ``session_key``, ``server_session_id``, ``memory_session_key``,
        ``system_prompt``, ``temperature``, ``max_tokens``, ``tts_speaker``.
        Any field omitted falls back to the default config value.
        """
        if not cls._initialized:
            cls.initialize_from_config()

        overlay = cls._profiles.get(profile)
        if overlay is None:
            if log:
                logger.warning(
                    f"[Hermes] Unknown profile {profile!r}; "
                    f"available: {sorted(cls._profiles)}"
                )
            return False

        defaults = cls._default_settings or {}

        def _pick(key: str, fallback):
            return overlay.get(key, defaults.get(key, fallback))

        cls._base_url = str(_pick("base_url", cls._base_url)).rstrip("/")
        cls._api_key = str(_pick("api_key", cls._api_key) or "")
        cls._model = str(_pick("model", cls._model) or "")
        cls._session_key = str(_pick("session_key", cls._session_key))
        cls._server_session_id = str(
            _pick("server_session_id", cls._server_session_id) or ""
        )
        cls._memory_session_key = str(
            _pick("memory_session_key", cls._memory_session_key) or ""
        )
        cls._system_prompt = str(_pick("system_prompt", cls._system_prompt) or "")
        cls._temperature = cls._optional_float(_pick("temperature", cls._temperature))
        cls._max_tokens = cls._optional_int(_pick("max_tokens", cls._max_tokens))
        cls._tts_speaker = _pick("tts_speaker", cls._tts_speaker)

        cls._active_profile = profile
        if log:
            logger.info(
                f"[Hermes] Profile switched to {profile!r}: "
                f"base_url={cls._base_url}, model={cls._model or '<server default>'}, "
                f"session_key={cls._session_key}"
            )
        return True

    @classmethod
    def reset_profile(cls):
        """Drop any active profile and restore the default config snapshot."""
        if not cls._default_settings:
            return
        d = cls._default_settings
        cls._base_url = str(d.get("base_url", cls._base_url)).rstrip("/")
        cls._api_key = str(d.get("api_key", "") or "")
        cls._model = str(d.get("model", "") or "")
        cls._session_key = str(d.get("session_key", cls._session_key))
        cls._server_session_id = str(d.get("server_session_id", "") or "")
        cls._memory_session_key = str(d.get("memory_session_key", "") or "")
        cls._system_prompt = str(d.get("system_prompt", "") or "")
        cls._temperature = d.get("temperature")
        cls._max_tokens = d.get("max_tokens")
        cls._tts_speaker = d.get("tts_speaker")
        prev, cls._active_profile = cls._active_profile, ""
        if prev:
            logger.info(f"[Hermes] Profile reset (was {prev!r})")

    # ------------------------------------------------------------------
    # Logging tweak — let logs distinguish Hermes from OpenAI.
    # ------------------------------------------------------------------
    @classmethod
    async def _send_and_track(cls, text: str) -> str | None:
        if not cls._initialized:
            cls.initialize_from_config()
        if not cls._enabled:
            logger.warning("[Hermes] send called but backend is disabled")
            return None

        run_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        cls._response_events[run_id] = loop.create_future()
        cls._response_texts[run_id] = ""
        cls._response_tts_speakers[run_id] = cls.get_tts_speaker_for_session_key()
        logger.user_speech(text, module=f"Hermes({cls._session_key})")
        asyncio.create_task(cls._run_chat_completion(run_id, text))
        return run_id

    @classmethod
    async def _run_chat_completion(cls, run_id: str, text: str):
        try:
            response_text = await cls._request_chat_completion(text)
            if response_text:
                cls._response_texts[run_id] = response_text
                logger.ai_response(
                    response_text, module=f"Hermes({cls._session_key})"
                )
        except Exception as exc:
            cls.last_error = f"{type(exc).__name__}: {exc}"
            logger.error(f"[Hermes] Run failed: {cls.last_error}")
        finally:
            waiter = cls._response_events.get(run_id)
            if waiter and not waiter.done():
                waiter.get_loop().call_soon_threadsafe(waiter.set_result, None)

    # ------------------------------------------------------------------
    # Native Hermes /v1/runs implementation
    # ------------------------------------------------------------------
    @classmethod
    async def _request_chat_completion(cls, text: str) -> str | None:
        """Send ``text`` via ``POST /v1/runs`` and poll until completion.

        Server-side session continuity is requested via ``session_id``;
        long-term memory scope is set via ``X-Hermes-Session-Key``.
        """
        session_key = cls._session_key
        history = cls._sessions.setdefault(session_key, [])

        # Decide what to send as ``input``: a single string when the server
        # is managing history, or a multi-message array when local history
        # is forced (stateless mode).
        if cls._send_local_history:
            payload_input: Any = cls._build_messages(history, text)
        else:
            payload_input = text

        payload: dict[str, Any] = {"input": payload_input}

        # Server-side conversation continuity: stable session_id per
        # logical conversation thread.
        server_session_id = cls._server_session_id or cls._session_key
        if server_session_id:
            payload["session_id"] = server_session_id

        if cls._system_prompt:
            payload["instructions"] = cls._system_prompt
        if cls._model:
            payload["model"] = cls._model

        # Stash optional extras (temperature/max_tokens/etc.) under
        # extra_body for forward compat — Hermes ignores unknown fields.
        for key, value in cls._extra_body.items():
            payload.setdefault(key, value)
        if cls._temperature is not None:
            payload.setdefault("temperature", cls._temperature)
        if cls._max_tokens is not None:
            payload.setdefault("max_tokens", cls._max_tokens)

        headers = {"Content-Type": "application/json"}
        if cls._api_key:
            headers["Authorization"] = f"Bearer {cls._api_key}"
        memory_scope = cls._memory_session_key or cls._session_key
        if memory_scope:
            headers["X-Hermes-Session-Key"] = memory_scope

        timeout = aiohttp.ClientTimeout(total=cls._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1. Start the run.
            async with session.post(
                cls._runs_url(),
                json=payload,
                headers=headers,
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    message = (
                        body.get("error", body) if isinstance(body, dict) else body
                    )
                    raise RuntimeError(f"HTTP {response.status}: {message}")

            run_id = (
                body.get("run_id") if isinstance(body, dict) else None
            ) or (body.get("id") if isinstance(body, dict) else None)
            if not run_id:
                raise RuntimeError(
                    f"Hermes /v1/runs returned no run_id: {body!r}"
                )

            # 2. Poll for completion.
            response_text = await cls._poll_run_until_done(
                session, run_id, headers
            )

        if response_text:
            cls._append_history(history, text, response_text)
        return response_text

    @classmethod
    async def _poll_run_until_done(
        cls,
        session: aiohttp.ClientSession,
        run_id: str,
        headers: dict[str, str],
    ) -> str | None:
        """Poll ``GET /v1/runs/{run_id}`` until status is terminal."""
        status_url = f"{cls._base_url}/runs/{run_id}"
        loop = asyncio.get_running_loop()
        # Allow up to ``cls._timeout`` total wall-clock for the run.
        deadline = loop.time() + cls._timeout

        while loop.time() < deadline:
            await asyncio.sleep(cls._poll_interval)
            try:
                async with session.get(status_url, headers=headers) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status >= 400:
                        message = (
                            body.get("error", body)
                            if isinstance(body, dict)
                            else body
                        )
                        raise RuntimeError(
                            f"HTTP {resp.status} polling run {run_id}: {message}"
                        )
            except aiohttp.ClientError as exc:
                logger.warning(
                    f"[Hermes] Transient error polling run {run_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            status = (body or {}).get("status")
            if status in ("completed", "succeeded", "success"):
                return cls._extract_run_output(body)
            if status in ("failed", "error"):
                err = (body or {}).get("error") or "run failed"
                raise RuntimeError(f"Hermes run failed: {err}")
            if status in ("cancelled", "canceled"):
                logger.warning(f"[Hermes] Run {run_id} was cancelled by server")
                return None
            # queued / running / waiting_for_approval — keep polling.

        raise RuntimeError(
            f"Hermes run {run_id} did not finish within {cls._timeout}s"
        )

    @classmethod
    def _runs_url(cls) -> str:
        if cls._base_url.endswith("/runs"):
            return cls._base_url
        return cls._base_url.rstrip("/") + "/runs"

    @classmethod
    def _extract_run_output(cls, body: Any) -> str | None:
        """Pull the assistant text out of a finished run status payload."""
        if not isinstance(body, dict):
            return None
        # Native Hermes /v1/runs returns ``output`` (stringified final reply)
        # alongside status when completed.
        output = body.get("output")
        if isinstance(output, str) and output.strip():
            return output.strip()
        # Some deployments wrap the output as a list of message dicts.
        if isinstance(output, list):
            parts = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for sub in content:
                        if (
                            isinstance(sub, dict)
                            and sub.get("type") in ("text", "output_text")
                            and isinstance(sub.get("text"), str)
                        ):
                            parts.append(sub["text"])
            joined = "".join(parts).strip()
            if joined:
                return joined
        # Fallback to ``response`` / ``final_response`` keys some servers
        # use in their /runs payload.
        for key in ("response", "final_response", "text"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
