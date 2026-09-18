import unittest

from sekaisync.llm_client import _parse_json_content


class LLMClientTest(unittest.TestCase):
    def test_parse_plain_json(self):
        self.assertEqual(_parse_json_content('{"a": 1}'), {"a": 1})

    def test_parse_fenced_json(self):
        content = '```json\n{"terms": [{"term": "网络天堂"}]}\n```'
        self.assertEqual(_parse_json_content(content)["terms"][0]["term"], "网络天堂")

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            _parse_json_content("not json")


import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self, *args, **kwargs):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _UrlopenSpy:
    """Replacement for urllib.request.urlopen that records/blocks calls."""

    def __init__(self, response=None, error=None, fail_hard=True):
        self.calls = []
        self.response = response
        self.error = error
        self.fail_hard = fail_hard

    def __call__(self, request, *args, **kwargs):
        self.calls.append((request, args, kwargs))
        if self.error is not None:
            raise self.error
        if self.response is None:
            if self.fail_hard:
                raise AssertionError(
                    "urlopen was called but this test requires zero network traffic"
                )
            raise AssertionError("no response configured")
        return self.response


class LLMNoKeyTest(unittest.TestCase):
    """P15: an unconfigured key means a controlled failure, zero traffic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = {
            key: os.environ.pop(key, None)
            for key in ("OPENAI_API_KEY", "SEKAISYNC_LLM_CONFIG")
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_missing_key_sends_no_network_traffic(self):
        from sekaisync.llm_client import LLMClient, LLMConfig, LLMConfigError

        spy = _UrlopenSpy()
        client = LLMClient(LLMConfig(api_key="", api_key_env="P15_MISSING_KEY"))
        with patch("urllib.request.urlopen", spy):
            with self.assertRaises(LLMConfigError):
                client.chat_json("system prompt", "user prompt")
        self.assertEqual(spy.calls, [])

    def test_missing_key_error_names_the_env_var_not_the_prompt(self):
        from sekaisync.llm_client import LLMClient, LLMConfig, LLMConfigError

        client = LLMClient(LLMConfig(api_key="", api_key_env="P15_MISSING_KEY"))
        with patch("urllib.request.urlopen", _UrlopenSpy()):
            with self.assertRaises(LLMConfigError) as ctx:
                client.chat_json("SYSTEM-SECRET-TEXT", "USER-SECRET-TEXT")
        message = str(ctx.exception)
        self.assertIn("P15_MISSING_KEY", message)
        self.assertNotIn("SYSTEM-SECRET-TEXT", message)
        self.assertNotIn("USER-SECRET-TEXT", message)

    def test_env_var_key_is_used(self):
        from sekaisync.llm_client import LLMClient, LLMConfig

        os.environ["P15_PRESENT_KEY"] = "sk-test-key-value"
        spy = _UrlopenSpy(response=_FakeResponse({
            "choices": [{"message": {"content": "{\"ok\": true}"}}]
        }))
        client = LLMClient(LLMConfig(api_key="", api_key_env="P15_PRESENT_KEY"))
        with patch("urllib.request.urlopen", spy):
            result = client.chat_json("s", "u")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(spy.calls), 1)
        headers = spy.calls[0][0].headers
        self.assertIn("Authorization", headers)
        self.assertIn("sk-test-key-value", headers["Authorization"])
        os.environ.pop("P15_PRESENT_KEY", None)

    def test_explicit_missing_config_path_raises(self):
        from sekaisync.llm_client import LLMConfigError, load_llm_config

        missing = Path(self.tmp.name) / "does-not-exist.json"
        with self.assertRaises(LLMConfigError):
            load_llm_config(missing)

    def test_env_config_path_missing_raises(self):
        from sekaisync.llm_client import LLMConfigError, load_llm_config

        os.environ["SEKAISYNC_LLM_CONFIG"] = str(
            Path(self.tmp.name) / "also-missing.json"
        )
        with self.assertRaises(LLMConfigError):
            load_llm_config()

    def test_unconfigured_state_keeps_defaults(self):
        from sekaisync.llm_client import load_llm_config

        config = load_llm_config()
        self.assertEqual(config.base_url, "https://api.openai.com/v1")
        self.assertEqual(config.api_key_env, "OPENAI_API_KEY")

    def test_existing_config_file_is_read(self):
        from sekaisync.llm_client import load_llm_config

        path = Path(self.tmp.name) / "llm.json"
        path.write_text(
            json.dumps({"model": "custom-model", "timeout": 5}), encoding="utf-8"
        )
        config = load_llm_config(path)
        self.assertEqual(config.model, "custom-model")
        self.assertEqual(config.timeout, 5)

    def test_client_never_retries_another_provider(self):
        """A failure must not silently re-point at a different endpoint."""
        from sekaisync.llm_client import LLMClient, LLMConfig, LLMResponseError

        spy = _UrlopenSpy(error=OSError("connection refused"))
        client = LLMClient(LLMConfig(api_key="sk-configured-key"))
        with patch("urllib.request.urlopen", spy):
            with self.assertRaises(LLMResponseError):
                client.chat_json("s", "u")
        self.assertEqual(len(spy.calls), 1)
        endpoint = spy.calls[0][0].full_url
        self.assertEqual(endpoint, "https://api.openai.com/v1/chat/completions")


class LLMResponseShapeTest(unittest.TestCase):
    """P15: validate the response shape instead of indexing blindly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = os.environ.pop("OPENAI_API_KEY", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._saved
        self.tmp.cleanup()

    def _client(self, **overrides):
        from sekaisync.llm_client import LLMClient, LLMConfig

        config = LLMConfig(api_key="sk-test-key-1234567890", **overrides)
        return LLMClient(config)

    def _call(self, payload):
        from sekaisync.llm_client import LLMResponseError

        spy = _UrlopenSpy(response=_FakeResponse(payload))
        client = self._client()
        with patch("urllib.request.urlopen", spy):
            with self.assertRaises(LLMResponseError) as ctx:
                client.chat_json("s", "u")
        return str(ctx.exception)

    def test_empty_choices_raises_controlled_error(self):
        message = self._call({"choices": []})
        self.assertIn("choices", message)

    def test_missing_choices_raises_controlled_error(self):
        message = self._call({"id": "x"})
        self.assertIn("choices", message)

    def test_null_content_raises_controlled_error(self):
        message = self._call({"choices": [{"message": {"content": None}}]})
        self.assertIn("content", message)

    def test_non_string_content_raises_controlled_error(self):
        message = self._call({"choices": [{"message": {"content": {"a": 1}}}]})
        self.assertIn("content", message)

    def test_missing_message_raises_controlled_error(self):
        message = self._call({"choices": [{}]})
        self.assertIn("message", message)

    def test_non_object_response_raises_controlled_error(self):
        message = self._call([1, 2, 3])
        self.assertIn("object", message)

    def test_errors_do_not_echo_the_api_key(self):
        secret = "sk-super-secret-value-abcdef"
        from sekaisync.llm_client import LLMClient, LLMConfig, LLMResponseError

        client = LLMClient(LLMConfig(api_key=secret))
        spy = _UrlopenSpy(response=_FakeResponse({"choices": []}))
        with patch("urllib.request.urlopen", spy):
            with self.assertRaises(LLMResponseError) as ctx:
                client.chat_json("s", "u")
        self.assertNotIn(secret, str(ctx.exception))
        # The key legitimately travels in the request's Authorization
        # header; what must never happen is it reaching an error message
        # or a log line.
        self.assertIn("Authorization", spy.calls[0][0].headers)

    def test_http_error_body_is_not_echoed(self):
        """A provider body may echo the request; it must not reach the error."""
        import urllib.error

        from sekaisync.llm_client import LLMClient, LLMConfig, LLMResponseError

        secret = "sk-leaked-through-http-error"
        body = json.dumps({"error": "bad key", "key": secret}).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions", 401, "Unauthorized",
            {}, io.BytesIO(body),
        )
        client = LLMClient(LLMConfig(api_key=secret))
        spy = _UrlopenSpy(error=error)
        with patch("urllib.request.urlopen", spy):
            with self.assertRaises(LLMResponseError) as ctx:
                client.chat_json("SENSITIVE-PROMPT", "SENSITIVE-PROMPT")
        message = str(ctx.exception)
        self.assertIn("401", message)
        self.assertNotIn(secret, message)
        self.assertNotIn("SENSITIVE-PROMPT", message)
        self.assertNotIn("bad key", message)

    def test_timeout_is_honoured(self):
        from sekaisync.llm_client import LLMResponseError

        class _Timeout(_UrlopenSpy):
            def __call__(self, request, *args, **kwargs):
                raise TimeoutError("timed out")

        client = self._client(timeout=7)
        spy = _Timeout()
        with patch("urllib.request.urlopen", spy):
            with self.assertRaises(LLMResponseError):
                client.chat_json("s", "u")
        self.assertEqual(spy.calls, [])

    def test_timeout_passed_to_urlopen(self):
        spy = _UrlopenSpy(response=_FakeResponse({
            "choices": [{"message": {"content": "{}"}}]
        }))
        client = self._client(timeout=7)
        with patch("urllib.request.urlopen", spy):
            client.chat_json("s", "u")
        _, _, kwargs = spy.calls[0]
        self.assertEqual(kwargs.get("timeout"), 7)

    def test_oversize_request_is_refused_locally(self):
        from sekaisync.llm_client import (
            MAX_REQUEST_BYTES,
            LLMResponseError,
        )

        spy = _UrlopenSpy()
        client = self._client()
        with patch("urllib.request.urlopen", spy):
            with self.assertRaises(LLMResponseError):
                client.chat_json("s", "u" * MAX_REQUEST_BYTES)
        self.assertEqual(spy.calls, [])

    def test_oversize_response_is_refused(self):
        from sekaisync.llm_client import MAX_RESPONSE_BYTES, LLMResponseError

        huge = json.dumps({
            "choices": [{"message": {"content": "x" * (MAX_RESPONSE_BYTES + 10)}}]
        }).encode("utf-8")
        spy = _UrlopenSpy(response=_FakeResponse(huge))
        client = self._client()
        with patch("urllib.request.urlopen", spy):
            with self.assertRaises(LLMResponseError):
                client.chat_json("s", "u")

    def test_valid_response_still_parses(self):
        spy = _UrlopenSpy(response=_FakeResponse({
            "choices": [{"message": {"content": "```json\n{\"terms\": []}\n```"}}]
        }))
        client = self._client()
        with patch("urllib.request.urlopen", spy):
            self.assertEqual(client.chat_json("s", "u"), {"terms": []})


if __name__ == "__main__":
    unittest.main()
