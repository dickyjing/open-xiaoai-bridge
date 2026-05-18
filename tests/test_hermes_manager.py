"""Unit tests for the Hermes Agent native backend manager.

Exercises the full ``/v1/runs`` lifecycle (start → poll → completed)
against a mock aiohttp server, plus error handling paths.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest import mock


class _StubLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _StubConfigManager:
    """Minimal stand-in for ``core.utils.config.ConfigManager``."""

    _instance = None

    def __init__(self, app_config: dict[str, Any]):
        self._app_config = app_config
        self._listeners: list = []

    @classmethod
    def install(cls, app_config: dict[str, Any]):
        cls._instance = cls(app_config)
        return cls._instance

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls({})
        return cls._instance

    def get_app_config(self, key: str, default=None):
        node: Any = self._app_config
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def add_reload_listener(self, callback):
        self._listeners.append(callback)


def _install_stubs():
    """Install lightweight stubs so ``core.hermes`` imports cleanly."""
    # Rust native extension — never used by the manager itself.
    sys.modules.setdefault("open_xiaoai_server", mock.MagicMock())

    # ``core.utils.logger`` — we just need a no-op logger object.
    logger_mod = sys.modules.get("core.utils.logger") or mock.MagicMock()
    logger_mod.logger = _StubLogger()
    sys.modules["core.utils.logger"] = logger_mod

    # ``core.utils.base.get_env`` — return None so HERMES_ENABLE is unset.
    base_mod = sys.modules.get("core.utils.base") or mock.MagicMock()
    base_mod.get_env = lambda _key: None
    sys.modules["core.utils.base"] = base_mod

    # ``core.utils.config.ConfigManager`` — backed by our stub.
    cfg_mod = sys.modules.get("core.utils.config") or mock.MagicMock()
    cfg_mod.ConfigManager = _StubConfigManager
    sys.modules["core.utils.config"] = cfg_mod


_install_stubs()


class _RunsHandler(BaseHTTPRequestHandler):
    """In-process mock for ``POST /v1/runs`` + ``GET /v1/runs/{id}``."""

    # Per-test override hooks.
    poll_sequence: list[dict[str, Any]] = []
    last_post_payload: dict[str, Any] | None = None
    last_post_headers: dict[str, str] | None = None
    poll_index = 0

    def log_message(self, *_args, **_kwargs):
        pass  # silence default access log

    def do_POST(self):  # noqa: N802 — http.server contract
        if self.path != "/v1/runs":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        try:
            _RunsHandler.last_post_payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            _RunsHandler.last_post_payload = None
        _RunsHandler.last_post_headers = dict(self.headers.items())
        _RunsHandler.poll_index = 0
        response = {"run_id": "run_test_123", "status": "started"}
        data = json.dumps(response).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if not self.path.startswith("/v1/runs/"):
            self.send_error(404)
            return
        seq = _RunsHandler.poll_sequence
        idx = min(_RunsHandler.poll_index, len(seq) - 1)
        payload = seq[idx]
        _RunsHandler.poll_index += 1
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _MockServer:
    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _RunsHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()


class HermesManagerTest(unittest.TestCase):
    def setUp(self):
        _StubConfigManager.install({})
        _RunsHandler.poll_sequence = []
        _RunsHandler.last_post_payload = None
        _RunsHandler.last_post_headers = None
        _RunsHandler.poll_index = 0

        # Re-import a fresh ``core.hermes`` so class-level state is clean.
        for module_name in ("core.hermes", "core.openai"):
            sys.modules.pop(module_name, None)
        from core.hermes import HermesManager  # noqa: WPS433 — late import

        self.HermesManager = HermesManager

    def _configure(self, base_url: str, **overrides):
        cfg = {
            "hermes": {
                "base_url": base_url,
                "api_key": "test-token",
                "session_key": "unit-test",
                "response_timeout": 5,
                "poll_interval": 0.05,
                **overrides,
            }
        }
        _StubConfigManager.install(cfg)
        self.HermesManager.initialize_from_config(enabled=True)

    def test_run_completion_flow(self):
        with _MockServer() as srv:
            _RunsHandler.poll_sequence = [
                {"status": "queued"},
                {"status": "running"},
                {"status": "completed", "output": "hello world"},
            ]
            self._configure(srv.base_url)

            text = asyncio.run(self.HermesManager.send("hi", wait_response=True))

        self.assertEqual(text, "hello world")
        self.assertIsNotNone(_RunsHandler.last_post_payload)
        payload = _RunsHandler.last_post_payload
        self.assertEqual(payload["input"], "hi")
        self.assertEqual(payload["session_id"], "unit-test")
        # Auth + memory-scope headers must be wired up.
        headers = _RunsHandler.last_post_headers or {}
        self.assertEqual(headers.get("Authorization"), "Bearer test-token")
        self.assertEqual(headers.get("X-Hermes-Session-Key"), "unit-test")

    def test_run_failure_returns_none(self):
        with _MockServer() as srv:
            _RunsHandler.poll_sequence = [
                {"status": "running"},
                {"status": "failed", "error": "boom"},
            ]
            self._configure(srv.base_url)
            text = asyncio.run(self.HermesManager.send("hi", wait_response=True))
        self.assertIsNone(text)
        self.assertIsNotNone(self.HermesManager.last_error)

    def test_send_local_history_flag_sends_messages_array(self):
        with _MockServer() as srv:
            _RunsHandler.poll_sequence = [
                {"status": "completed", "output": "ok"},
            ]
            self._configure(srv.base_url, send_local_history=True, system_prompt="be brief")
            asyncio.run(self.HermesManager.send("turn-1", wait_response=True))

            # Second turn should include the prior assistant message in history.
            _RunsHandler.poll_sequence = [
                {"status": "completed", "output": "ok"},
            ]
            asyncio.run(self.HermesManager.send("turn-2", wait_response=True))

        payload = _RunsHandler.last_post_payload
        self.assertIsInstance(payload["input"], list)
        roles = [m["role"] for m in payload["input"]]
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles[-1], "user")
        # System prompt should map to ``instructions``.
        self.assertEqual(payload.get("instructions"), "be brief")

    def test_extract_run_output_handles_list_messages(self):
        body = {
            "status": "completed",
            "output": [
                {"role": "assistant", "content": [{"type": "text", "text": "part-A "}]},
                {"role": "assistant", "content": "part-B"},
            ],
        }
        result = self.HermesManager._extract_run_output(body)
        self.assertEqual(result, "part-A part-B")

    def test_profile_switch_layers_overlay_on_defaults(self):
        with _MockServer() as srv:
            self._configure(
                srv.base_url,
                system_prompt="default sys",
                profiles={
                    "cto": {
                        "system_prompt": "你是 CTO",
                        "session_key": "open-xiaoai-bridge:cto",
                    },
                    "ops": {
                        "system_prompt": "你是 Ops",
                        "model": "gpt-4o-mini",
                    },
                },
            )

            self.assertEqual(
                self.HermesManager.list_profiles(), ["cto", "ops"]
            )
            self.assertEqual(self.HermesManager.active_profile(), "")

            ok = self.HermesManager.set_profile("cto")
            self.assertTrue(ok)
            self.assertEqual(self.HermesManager.active_profile(), "cto")
            self.assertEqual(self.HermesManager._system_prompt, "你是 CTO")
            self.assertEqual(
                self.HermesManager._session_key, "open-xiaoai-bridge:cto"
            )
            # base_url should fall back to default since the profile didn't set it
            self.assertEqual(self.HermesManager._base_url, srv.base_url)

            # Switching to ops applies different overlay on top of defaults
            self.HermesManager.set_profile("ops")
            self.assertEqual(self.HermesManager._system_prompt, "你是 Ops")
            self.assertEqual(self.HermesManager._model, "gpt-4o-mini")
            # ops did NOT override session_key → fall back to default
            self.assertEqual(self.HermesManager._session_key, "unit-test")

            # Unknown profile is a no-op (returns False, state preserved)
            ok = self.HermesManager.set_profile("does-not-exist")
            self.assertFalse(ok)
            self.assertEqual(self.HermesManager.active_profile(), "ops")

            # Reset clears the overlay
            self.HermesManager.reset_profile()
            self.assertEqual(self.HermesManager.active_profile(), "")
            self.assertEqual(self.HermesManager._system_prompt, "default sys")
            self.assertEqual(self.HermesManager._model, "")

    def test_profile_session_key_in_post_payload(self):
        with _MockServer() as srv:
            _RunsHandler.poll_sequence = [
                {"status": "completed", "output": "ok"}
            ]
            self._configure(
                srv.base_url,
                profiles={
                    "cto": {"session_key": "open-xiaoai-bridge:cto"},
                },
            )
            self.HermesManager.set_profile("cto")
            asyncio.run(self.HermesManager.send("hi", wait_response=True))

        payload = _RunsHandler.last_post_payload
        self.assertIsNotNone(payload)
        # session_id (server-side thread) should be the profile's session_key
        self.assertEqual(payload["session_id"], "open-xiaoai-bridge:cto")
        # X-Hermes-Session-Key header should also follow the profile
        headers = _RunsHandler.last_post_headers or {}
        self.assertEqual(
            headers.get("X-Hermes-Session-Key"), "open-xiaoai-bridge:cto"
        )


if __name__ == "__main__":
    unittest.main()
