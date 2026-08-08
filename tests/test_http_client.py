"""Protocol-shape tests for the dependency-free HTTP model clients."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dispatch  # noqa: E402


class FakeResponse:
    def __init__(self, value):
        self.raw = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit):
        return self.raw[:limit]


class TestHttpModelClient(unittest.TestCase):
    def test_anthropic_request_and_response_schema(self):
        response = FakeResponse(
            {
                "content": [{"type": "text", "text": '{"ok":true}'}],
                "usage": {"input_tokens": 12, "output_tokens": 5},
            }
        )
        with (
            mock.patch.dict(os.environ, {"TEST_ANTHROPIC_KEY": "secret"}),
            mock.patch("dispatch._http_open", return_value=response) as opened,
        ):
            client = dispatch.HttpModelClient(
                "anthropic", "https://api.anthropic.test", "TEST_ANTHROPIC_KEY"
            )
            result = client.complete("model-id", "system", "user", 100, 3)
        request = opened.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.anthropic.test/v1/messages")
        self.assertEqual(body["model"], "model-id")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(result.text, '{"ok":true}')
        self.assertEqual(result.input_tokens, 12)
        self.assertEqual(result.output_tokens, 5)

    def test_openai_compatible_request_and_response_schema(self):
        response = FakeResponse(
            {
                "choices": [{"message": {"content": "result"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4},
            }
        )
        with mock.patch("dispatch._http_open", return_value=response) as opened:
            client = dispatch.HttpModelClient("openai", "http://local.test", "")
            result = client.complete("local-model", "system", "user", 50, 2)
        request = opened.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "http://local.test/v1/chat/completions")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(result.text, "result")
        self.assertEqual(result.input_tokens, 20)

    def test_invalid_endpoint_schema_fails_closed(self):
        response = FakeResponse({"unexpected": "shape"})
        with mock.patch("dispatch._http_open", return_value=response):
            client = dispatch.HttpModelClient("openai", "http://local.test", "")
            with self.assertRaises(dispatch.ValidationError):
                client.complete("local-model", "system", "user", 50, 2)

    def test_missing_anthropic_key_fails_before_claim_or_budget_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dispatch.db"
            conn = dispatch.db(path)
            job_id = dispatch.enqueue(
                conn,
                "dossier_section",
                {"task": "Extract", "evidence": []},
            )
            conn.close()
            client = dispatch.HttpModelClient(
                "anthropic", "https://api.anthropic.test", "ABSENT_TEST_KEY"
            )
            with (
                mock.patch.dict(os.environ, {"ABSENT_TEST_KEY": ""}),
                self.assertRaises(dispatch.DispatchError),
            ):
                dispatch.run(client, db_path=path)
            check = dispatch.db(path)
            job = check.execute("SELECT status,attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            reserved = check.execute(
                "SELECT calls_reserved FROM campaign_budget WHERE id=1"
            ).fetchone()[0]
            self.assertEqual((job["status"], job["attempts"]), ("pending", 0))
            self.assertEqual(reserved, 0)
            check.close()


class TestHttpErrorTaxonomy(unittest.TestCase):
    """v2.1: HTTP failures must be classified, not treated uniformly."""

    @staticmethod
    def _http_error(code, headers=None):
        import email.message
        import urllib.error

        message = email.message.Message()
        for key, value in (headers or {}).items():
            message[key] = value
        return urllib.error.HTTPError("https://api.test", code, "err", message, None)

    def _call(self, side_effect):
        with mock.patch("dispatch._http_open", side_effect=side_effect):
            client = dispatch.HttpModelClient("openai", "http://local.test", "")
            client.complete("m", "system", "user", 50, 2)

    def test_401_is_fatal_for_the_whole_run(self):
        with self.assertRaises(dispatch.ModelEndpointFatalError):
            self._call(self._http_error(401))

    def test_400_is_permanent_for_the_job(self):
        with self.assertRaises(dispatch.ModelCallPermanentError):
            self._call(self._http_error(400))

    def test_429_is_transient_and_carries_retry_after(self):
        with self.assertRaises(dispatch.ModelCallTransientError) as caught:
            self._call(self._http_error(429, {"Retry-After": "17"}))
        self.assertEqual(caught.exception.retry_after, 17.0)

    def test_retry_after_is_capped(self):
        with self.assertRaises(dispatch.ModelCallTransientError) as caught:
            self._call(self._http_error(503, {"Retry-After": "86400"}))
        self.assertEqual(caught.exception.retry_after, dispatch._MAX_RETRY_AFTER_SECONDS)

    def test_timeout_is_transient(self):
        with self.assertRaises(dispatch.ModelCallTransientError):
            self._call(TimeoutError("timed out"))


class TestTruncationDetection(unittest.TestCase):
    """v2.1: token-limit truncation must surface as a validation failure."""

    def test_anthropic_max_tokens_stop_reason_fails_closed(self):
        response = FakeResponse(
            {
                "content": [{"type": "text", "text": '{"partial":'}],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 5, "output_tokens": 100},
            }
        )
        with (
            mock.patch.dict(os.environ, {"TEST_ANTHROPIC_KEY": "secret"}),
            mock.patch("dispatch._http_open", return_value=response),
        ):
            client = dispatch.HttpModelClient(
                "anthropic", "https://api.anthropic.test", "TEST_ANTHROPIC_KEY"
            )
            with self.assertRaises(dispatch.ValidationError) as caught:
                client.complete("m", "system", "user", 100, 3)
        self.assertIn("truncated", str(caught.exception))

    def test_openai_length_finish_reason_fails_closed(self):
        response = FakeResponse(
            {
                "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 50},
            }
        )
        with mock.patch("dispatch._http_open", return_value=response):
            client = dispatch.HttpModelClient("openai", "http://local.test", "")
            with self.assertRaises(dispatch.ValidationError):
                client.complete("m", "system", "user", 50, 2)


class TestProxyPolicy(unittest.TestCase):
    """v2.1: key-bearing traffic must not follow ambient proxy env vars."""

    def test_ambient_proxy_env_is_ignored(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://ambient.proxy:8080"}, clear=False):
            opener = dispatch._build_http_opener()
        proxies = [
            handler.proxies
            for handler in opener.handlers
            if handler.__class__.__name__ == "ProxyHandler"
        ]
        self.assertTrue(all(not p for p in proxies))

    def test_explicit_ralph_proxy_is_honored(self):
        with mock.patch.dict(
            os.environ, {"RALPH_HTTPS_PROXY": "http://audited.proxy:3128"}, clear=False
        ):
            opener = dispatch._build_http_opener()
        proxies = {}
        for handler in opener.handlers:
            if handler.__class__.__name__ == "ProxyHandler":
                proxies.update(handler.proxies)
        self.assertEqual(proxies.get("https"), "http://audited.proxy:3128")


if __name__ == "__main__":
    unittest.main(verbosity=2)
