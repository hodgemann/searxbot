#!/usr/bin/env python3
"""Fixture tests for SearxBot.

No shared service is contacted: HTTP behaviour is faked, and the real-socket
tests use a disposable local fixture server on an ephemeral port. The PyBot
integration tests import a checkout read-only; point PYBOT_PATH at one, or they
skip.

    PYBOT_PATH=/path/to/pybot python3 -B -m unittest discover -s tests -v
"""
from __future__ import annotations

import email.message
import http.client
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import searxbot as adapter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The PyBot integration tests are the only ones that need a checkout. Point
# PYBOT_PATH at one, or make its `bot` package importable some other way.
PYBOT_PATH = os.environ.get("PYBOT_PATH")
if PYBOT_PATH:
    adapter.add_pybot_to_path(PYBOT_PATH)
try:
    from bot import runtime as PYBOT_RUNTIME
except ImportError:
    PYBOT_RUNTIME = None


class QuietServer(ThreadingHTTPServer):
    """A fixture server that stays quiet when the client hangs up first."""

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if not isinstance(error, (BrokenPipeError, ConnectionResetError)):
            ThreadingHTTPServer.handle_error(self, request, client_address)


class FakeResponse:
    def __init__(self, body, content_type="application/json", charset="utf-8"):
        self.body = body if isinstance(body, bytes) else body.encode(charset)
        headers = email.message.Message()
        headers["Content-Type"] = content_type if charset is None else "%s; charset=%s" % (content_type, charset)
        self.headers = headers

    def read(self, size=-1):
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    """Stands in for urllib.request.OpenerDirector and records the request."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.urls = []
        self.timeouts = []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.response


def payload(**overrides):
    data = {
        "query": "cats",
        "results": [
            {"title": "Cats of the world", "url": "https://example.org/cats",
             "content": "Small carnivorous mammals.", "engines": ["duckduckgo", "wikipedia"]},
            {"title": "Cat", "url": "https://example.com/wiki/Cat",
             "content": "The domestic cat.", "engines": ["google"]},
        ],
        "answers": [],
        "suggestions": [],
        "unresponsive_engines": [],
    }
    data.update(overrides)
    return json.dumps(data)


class ParseSearchCommand(unittest.TestCase):
    def test_recognizes_the_command(self):
        self.assertEqual(adapter.parse_search_command("/search cats"), "cats")
        self.assertEqual(adapter.parse_search_command("  /SEARCH  café 東京  "), "café 東京")
        self.assertEqual(adapter.parse_search_command("/search"), "")
        self.assertEqual(adapter.parse_search_command("/search   "), "")
        self.assertEqual(adapter.parse_search_command("/search\nmulti line"), "multi line")

    def test_leaves_ordinary_chat_to_pybot(self):
        for text in ("search cats", "/searchcats", "I /search cats", "hello", "", "/reset",
                     "what is my name?", None, 42):
            self.assertIsNone(adapter.parse_search_command(text), text)


class NaturalSearchRouting(unittest.TestCase):
    def test_clear_web_requests_preserve_the_query(self):
        for text in ("search the web for café 東京", "Search online for café 東京",
                     "please search the internet for café 東京",
                     "Can you search the web for café 東京",
                     "could you please search online for café 東京",
                     "Please would you search the web for café 東京"):
            self.assertEqual(adapter.parse_search_request(text), "café 東京", text)

    def test_non_requests_stay_local(self):
        for text in ("hello", "what is the latest news?", "search cats",
                     "don't search the web for cats", "do not search online for cats",
                     '"search the web for cats"', "I said search the web for cats",
                     "how do I search the web for cats?", "can you search my files for cats",
                     "search the web for cats\nDon't search",
                     "please\nsearch online for cats", "search\rthe web for cats", None, 42):
            self.assertIsNone(adapter.parse_search_request(text), text)

    def test_bare_web_request_reports_missing_query_without_http(self):
        for text in ("search the web", "please search online for"):
            opener = FakeOpener()
            result = adapter.handle(text, {}, opener=opener)
            self.assertEqual(result["error"]["kind"], adapter.EMPTY_QUERY)
            self.assertEqual(opener.urls, [])

    def test_web_request_dispatches_once_without_calling_pybot(self):
        def forbidden_runtime():
            self.fail("web requests must bypass PyBot arbitration")
        opener = FakeOpener(FakeResponse(payload()))
        memory = {"turns": [{"user": "hello", "bot": "hi"}]}
        before = list(memory["turns"])
        result = adapter.handle("could you search online for café 東京", memory,
                                opener=opener, runtime=forbidden_runtime)
        self.assertEqual(result["source"], "search")
        self.assertIsNone(result["error"])
        self.assertEqual(len(opener.urls), 1)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(opener.urls[0]).query)["q"],
                         ["café 東京"])
        self.assertEqual(memory["turns"], before)

    def test_ordinary_question_does_not_search(self):
        class Runtime:
            def turn(self, text, memory):
                return {"reply": "local answer", "module": "fixture", "candidates": []}
        opener = FakeOpener()
        result = adapter.handle("what is the latest news?", {}, opener=opener, runtime=Runtime())
        self.assertEqual(result["source"], "pybot")
        self.assertEqual(opener.urls, [])

    def test_natural_search_failure_does_not_fall_back_to_chat(self):
        def forbidden_runtime():
            self.fail("a failed search must not fall back to chat")
        result = adapter.handle("search online for cats", {},
                                opener=FakeOpener(error=urllib.error.URLError("fixture")),
                                runtime=forbidden_runtime)
        self.assertEqual(result["source"], "search")
        self.assertEqual(result["error"]["kind"], adapter.UNREACHABLE)

    def test_query_punctuation_is_preserved(self):
        self.assertEqual(adapter.parse_search_request("can you search online for C++?"), "C++?")


class EndpointConstruction(unittest.TestCase):
    def test_encodes_query_and_forces_json(self):
        url = adapter.search_endpoint("café 東京 & docs", "http://127.0.0.1:8888", page=2)
        self.assertTrue(url.startswith("http://127.0.0.1:8888/search?"))
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(params["q"], ["café 東京 & docs"])
        self.assertEqual(params["format"], ["json"])
        self.assertEqual(params["pageno"], ["2"])
        self.assertIn("%E6%9D%B1%E4%BA%AC", url)

    def test_base_url_is_configurable(self):
        os.environ["SEARXNG_URL"] = "http://127.0.0.1:9999/searx"
        try:
            self.assertEqual(adapter.resolve_base_url(), "http://127.0.0.1:9999/searx")
            self.assertTrue(adapter.search_endpoint("x").startswith("http://127.0.0.1:9999/searx/search?"))
        finally:
            del os.environ["SEARXNG_URL"]
        self.assertEqual(adapter.resolve_base_url("http://127.0.0.1:8888/"), "http://127.0.0.1:8888")
        self.assertEqual(adapter.resolve_base_url(), adapter.DEFAULT_BASE_URL)

    def test_rejects_unusable_base_urls(self):
        for base in ("ftp://127.0.0.1:8888", "http://user:pw@127.0.0.1:8888",
                     "http://127.0.0.1:8888/?debug=1", "not a url"):
            with self.assertRaises(adapter.SearchError) as caught:
                adapter.search_endpoint("x", base)
            self.assertEqual(caught.exception.kind, adapter.BAD_CONFIG)


class SearchFailurePaths(unittest.TestCase):
    def assert_failure(self, opener, kind, query="cats"):
        with self.assertRaises(adapter.SearchError) as caught:
            adapter.search(query, "http://127.0.0.1:8888", opener=opener)
        self.assertEqual(caught.exception.kind, kind)
        self.assertTrue(caught.exception.message)
        return caught.exception

    def test_empty_query(self):
        self.assert_failure(FakeOpener(FakeResponse(payload())), adapter.EMPTY_QUERY, "   ")
        self.assert_failure(FakeOpener(FakeResponse(payload())), adapter.EMPTY_QUERY, "")

    def test_http_403_is_reported_as_forbidden(self):
        error = self.assert_failure(
            FakeOpener(error=urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)),
            adapter.FORBIDDEN)
        self.assertIn("403", error.message)
        self.assertIn("search.formats", error.message)

    def test_other_http_status(self):
        self.assert_failure(
            FakeOpener(error=urllib.error.HTTPError("http://x", 502, "Bad Gateway", {}, None)),
            adapter.HTTP_ERROR)

    def test_http_error_response_handle_is_released(self):
        class ErrorBody:
            closed = False

            def read(self, size=-1):
                return b""

            def close(self):
                self.closed = True

        body = ErrorBody()
        error = urllib.error.HTTPError("http://x", 502, "Bad Gateway", {}, body)
        with self.assertRaises(adapter.SearchError):
            adapter.search("cats", "http://127.0.0.1:8888", opener=FakeOpener(error=error))
        self.assertTrue(body.closed, "the error response body must be closed")

    def test_timeout(self):
        self.assert_failure(FakeOpener(error=socket.timeout("timed out")), adapter.TIMEOUT)
        wrapped = self.assert_failure(
            FakeOpener(error=urllib.error.URLError(reason=socket.timeout("timed out"))), adapter.TIMEOUT)
        self.assertIn("10", wrapped.message)

    def test_unreachable_service(self):
        error = self.assert_failure(
            FakeOpener(error=urllib.error.URLError(reason=ConnectionRefusedError(111, "Connection refused"))),
            adapter.UNREACHABLE)
        self.assertIn("127.0.0.1:8888", error.message)

    def test_malformed_payload(self):
        self.assert_failure(FakeOpener(FakeResponse("<html>not json</html>", "text/html")), adapter.MALFORMED)
        self.assert_failure(FakeOpener(FakeResponse("{not json")), adapter.MALFORMED)
        self.assert_failure(FakeOpener(FakeResponse(json.dumps([1, 2]))), adapter.MALFORMED)

    def test_timeout_value_is_passed_through(self):
        opener = FakeOpener(FakeResponse(payload()))
        adapter.search("cats", "http://127.0.0.1:8888", timeout=3.5, opener=opener)
        self.assertEqual(opener.timeouts, [3.5])

    def test_transport_and_value_errors_stay_structured(self):
        for error in (http.client.IncompleteRead(b""), http.client.BadStatusLine("x"),
                      ValueError("nonnumeric port: 'abc'")):
            with self.assertRaises(adapter.SearchError) as caught:
                adapter.search("cats", "http://127.0.0.1:8888", opener=FakeOpener(error=error))
            self.assertEqual(caught.exception.kind, adapter.UNREACHABLE, repr(error))

    def test_bug_in_the_response_body_path_is_not_reported_as_unreachable(self):
        class BrokenBody(FakeResponse):
            def read(self, size=-1):
                raise ValueError("programming bug")

        with self.assertRaises(ValueError):
            adapter.search("cats", "http://127.0.0.1:8888", opener=FakeOpener(BrokenBody(payload())))

    def test_broken_response_stream_is_unreachable(self):
        class BrokenBody(FakeResponse):
            def read(self, size=-1):
                raise http.client.IncompleteRead(b"partial")

        with self.assertRaises(adapter.SearchError) as caught:
            adapter.search("cats", "http://127.0.0.1:8888", opener=FakeOpener(BrokenBody(payload())))
        self.assertEqual(caught.exception.kind, adapter.UNREACHABLE)

    def test_bad_arguments(self):
        for kwargs in ({"max_results": 0}, {"max_results": "5"}, {"page": 0}, {"page": True}):
            with self.assertRaises(adapter.SearchError) as caught:
                adapter.search("cats", "http://127.0.0.1:8888", opener=FakeOpener(FakeResponse(payload())), **kwargs)
            self.assertEqual(caught.exception.kind, adapter.BAD_CONFIG)


class SearchResults(unittest.TestCase):
    def record(self, **overrides):
        opener = FakeOpener(FakeResponse(payload(**overrides)))
        return adapter.search("cats", "http://127.0.0.1:8888", opener=opener)

    def test_record_keeps_traceable_fields(self):
        record = self.record()
        self.assertEqual(record["query"], "cats")
        self.assertTrue(record["request_url"].startswith("http://127.0.0.1:8888/search?"))
        self.assertRegex(record["retrieved_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
        self.assertGreaterEqual(record["latency_seconds"], 0)
        self.assertEqual([item["url"] for item in record["results"]],
                         ["https://example.org/cats", "https://example.com/wiki/Cat"])
        self.assertEqual(record["results"][0]["engines"], ["duckduckgo", "wikipedia"])

    def test_empty_result_set_is_not_an_error(self):
        record = self.record(results=[])
        self.assertEqual(record["results"], [])
        reply = adapter.format_results(record)
        self.assertIn("no results", reply)

    def test_duplicate_urls_are_dropped_and_limit_applied(self):
        record = self.record(results=[
            {"title": "A", "url": "https://example.org/a", "content": "one"},
            {"title": "A again", "url": "https://example.org/a", "content": "two"},
            {"title": "B", "url": "https://example.org/b", "content": "three"},
            {"title": "No url", "content": "skipped"},
        ])
        self.assertEqual([item["url"] for item in record["results"]],
                         ["https://example.org/a", "https://example.org/b"])
        self.assertEqual(record["duplicate_results"], 1)
        self.assertEqual(record["unusable_results"], 1)
        self.assertEqual(record["result_count_reported"], 4)
        self.assertIn("Note: showing 2 of 4 reported, 1 without a usable URL, 1 duplicate URLs.",
                      adapter.format_results(record))
        limited = adapter.search("cats", "http://127.0.0.1:8888", max_results=1,
                                 opener=FakeOpener(FakeResponse(payload())))
        self.assertEqual(len(limited["results"]), 1)

    def test_reply_shows_title_link_attributed_snippet(self):
        reply = adapter.format_results(self.record())
        self.assertIn("1. Cats of the world", reply)
        self.assertIn("https://example.org/cats", reply)
        self.assertIn("Small carnivorous mammals. (from: duckduckgo, wikipedia)", reply)
        self.assertIn("Retrieved from SearXNG", reply)

    def test_long_snippets_are_clipped(self):
        record = self.record(results=[{"title": "Long", "url": "https://example.org/long",
                                       "content": "x" * 900}])
        reply = adapter.format_results(record)
        self.assertNotIn("x" * 900, reply)
        self.assertIn("…", reply)

    def test_engine_failures_are_reported(self):
        record = self.record(unresponsive_engines=[["google", "timeout"], "bing", None])
        self.assertEqual(record["unresponsive_engines"],
                         [{"engine": "google", "error": "timeout"}, {"engine": "bing", "error": "unresponsive"}])
        self.assertIn("Engines that did not answer: google (timeout), bing (unresponsive)",
                      adapter.format_results(record))

    def test_answer_cards_are_shown(self):
        record = self.record(answers=["The domestic cat is a small carnivorous mammal."])
        self.assertIn("Answer card: The domestic cat", adapter.format_results(record))


class PayloadSchema(unittest.TestCase):
    """A well-formed JSON body that is not a search response must not look like one."""

    def assert_malformed(self, obj):
        opener = FakeOpener(FakeResponse(json.dumps(obj) if not isinstance(obj, str) else obj))
        with self.assertRaises(adapter.SearchError) as caught:
            adapter.search("cats", "http://127.0.0.1:8888", opener=opener)
        self.assertEqual(caught.exception.kind, adapter.MALFORMED)
        return caught.exception

    def test_empty_and_error_bodies_are_not_empty_searches(self):
        self.assert_malformed({})
        self.assert_malformed({"error": "backend failed"})
        self.assert_malformed({"results": [], "error": "backend failed"})

    def test_error_text_is_reported_but_bounded(self):
        message = self.assert_malformed({"results": [], "error": "limiter said no"}).message
        self.assertIn("limiter said no", message)
        self.assertLess(len(message), 300)

    def test_non_list_optional_fields(self):
        self.assert_malformed({"results": [], "answers": "a string"})
        self.assert_malformed({"results": [], "suggestions": {"a": 1}})
        self.assert_malformed({"results": [], "unresponsive_engines": "down"})

    def test_null_optional_fields_are_treated_as_empty(self):
        opener = FakeOpener(FakeResponse(json.dumps(
            {"results": [{"url": "https://e.org/x"}], "answers": None,
             "suggestions": None, "unresponsive_engines": None})))
        record = adapter.search("cats", "http://127.0.0.1:8888", opener=opener)
        self.assertEqual([item["url"] for item in record["results"]], ["https://e.org/x"])
        self.assertEqual(record["answers"], [])

    def test_non_list_engines_does_not_crash(self):
        opener = FakeOpener(FakeResponse(json.dumps(
            {"results": [{"url": "https://e.org/x", "engines": 42}]})))
        record = adapter.search("cats", "http://127.0.0.1:8888", opener=opener)
        self.assertEqual(record["results"][0]["engines"], [])

    def test_wrong_results_shape(self):
        self.assert_malformed({"results": "broken"})
        self.assert_malformed({"results": None})
        self.assert_malformed({"results": ["just a string"]})
        self.assert_malformed({"results": [[]]})

    def test_result_entry_without_a_url_is_skipped_not_fatal(self):
        opener = FakeOpener(FakeResponse(json.dumps(
            {"results": [{"title": "no url"}, {"title": "t", "url": "https://e.org/x"}]})))
        record = adapter.search("cats", "http://127.0.0.1:8888", opener=opener)
        self.assertEqual([item["url"] for item in record["results"]], ["https://e.org/x"])
        self.assertEqual(record["unusable_results"], 1)

    def test_error_message_names_the_problem(self):
        message = self.assert_malformed({"error": "backend failed"}).message
        self.assertIn("no results list", message)
        self.assertIn("error", message)

    def test_oversized_response_is_rejected(self):
        big = json.dumps({"results": [{"url": "https://e.org/x", "title": "t",
                                       "content": "y" * (adapter.MAX_RESPONSE_BYTES + 1)}]})
        opener = FakeOpener(FakeResponse(big))
        with self.assertRaises(adapter.SearchError) as caught:
            adapter.search("cats", "http://127.0.0.1:8888", opener=opener)
        self.assertEqual(caught.exception.kind, adapter.MALFORMED)


class MalformedConfiguration(unittest.TestCase):
    def test_bad_authorities_and_ports(self):
        for base in ("http://[::1", "http://localhost:abc", "http://127.0.0.1:99999",
                     "http://\u6771\u4eac:8888", "http://127.0.0.1:8888/\u6771\u4eac"):
            with self.assertRaises(adapter.SearchError) as caught:
                adapter.search_endpoint("cats", base)
            self.assertEqual(caught.exception.kind, adapter.BAD_CONFIG, base)

    def test_bad_timeout_values(self):
        for value in (0, -1, float("nan"), float("inf"), "10", True, None, 10 ** 1000):
            with self.assertRaises(adapter.SearchError) as caught:
                adapter.search("cats", "http://127.0.0.1:8888", timeout=value,
                               opener=FakeOpener(FakeResponse(payload())))
            self.assertEqual(caught.exception.kind, adapter.BAD_CONFIG, repr(value))

    def test_redaction_hides_credentials_and_query(self):
        self.assertEqual(
            adapter.redact_url("http://user:token@127.0.0.1:8888/search?q=secret"),
            "http://127.0.0.1:8888/search")
        self.assertEqual(adapter.redact_url("http://[::1"), "<unparsable URL>")

    def test_error_messages_do_not_leak_credentials(self):
        for base in ("http://user:token@127.0.0.1:8888/?key=secret", "http://user:token@host：80/"):
            with self.assertRaises(adapter.SearchError) as caught:
                adapter.search_endpoint("cats", base)
            self.assertNotIn("token", caught.exception.message, base)
            self.assertNotIn("secret", caught.exception.message, base)


class Routing(unittest.TestCase):
    class StubRuntime:
        def __init__(self):
            self.calls = []

        def turn(self, text, memory):
            self.calls.append(text)
            return {"reply": "Generic PyBot reply", "module": "greeting",
                    "candidates": [{"module": "greeting", "priority": 1, "reply": "Generic PyBot reply"}]}

    def test_search_line_never_reaches_pybot(self):
        runtime = self.StubRuntime()
        memory = {}
        result = adapter.handle("/search cats", memory, opener=FakeOpener(FakeResponse(payload())),
                                runtime=runtime)
        self.assertEqual(result["source"], "search")
        self.assertEqual(runtime.calls, [])
        self.assertIn("1. Cats of the world", result["reply"])
        self.assertIsNone(result["error"])

    def test_failed_search_reports_accurate_error_not_pybot_fallback(self):
        runtime = self.StubRuntime()
        opener = FakeOpener(error=urllib.error.URLError(reason=ConnectionRefusedError(111, "Connection refused")))
        result = adapter.handle("/search cats", {}, opener=opener, runtime=runtime)
        self.assertEqual(result["source"], "search")
        self.assertEqual(result["error"]["kind"], adapter.UNREACHABLE)
        self.assertEqual(runtime.calls, [])
        self.assertTrue(result["reply"].startswith("Search failed (unreachable):"))
        self.assertNotIn("Generic PyBot reply", result["reply"])

    def test_empty_search_command_is_a_search_error(self):
        runtime = self.StubRuntime()
        result = adapter.handle("/search", {}, runtime=runtime)
        self.assertEqual(result["source"], "search")
        self.assertEqual(result["error"]["kind"], adapter.EMPTY_QUERY)
        self.assertEqual(runtime.calls, [])

    def test_other_lines_go_to_pybot(self):
        runtime = self.StubRuntime()
        result = adapter.handle("hello there", {}, runtime=runtime)
        self.assertEqual(result["source"], "pybot")
        self.assertEqual(runtime.calls, ["hello there"])
        self.assertEqual(result["module"], "greeting")
        self.assertIsNone(result["search"])

    def test_search_does_not_write_runtime_owned_turns(self):
        memory = {"turns": [{"user": "hi", "bot": "Hello!", "module": "greeting"}]}
        before = json.dumps(memory["turns"])
        adapter.handle("/search cats", memory, opener=FakeOpener(FakeResponse(payload())))
        self.assertEqual(json.dumps(memory["turns"]), before)


class SearchFollowups(unittest.TestCase):
    def setUp(self):
        self.memory = {}
        self.opener = FakeOpener(FakeResponse(payload()))
        self.first = adapter.handle("/search cats", self.memory, opener=self.opener)

    def recall(self, text, memory=None):
        def forbidden_runtime():
            self.fail("follow-ups must not load PyBot")
        result = adapter.handle(text, self.memory if memory is None else memory,
                                opener=self.opener, runtime=forbidden_runtime)
        self.assertEqual(result["source"], "search_context")
        self.assertEqual(len(self.opener.urls), 1, "recall must not retrieve again")
        return result

    def test_second_result_uses_display_order_and_full_snippet(self):
        result = self.recall("open the second one")
        self.assertIsNone(result["error"])
        self.assertIn("https://example.com/wiki/Cat", result["reply"])
        self.assertIn("The domestic cat.", result["reply"])
        self.assertIn("page not fetched", result["reply"])
        self.assertIn(self.first["search"]["retrieved_at"], result["reply"])
        self.assertNotIn("turns", self.memory)

    def test_repeat_lists_original_query_and_timestamp(self):
        for text in ("/results", "show the results again", "repeat the results",
                     "what did I search for?"):
            result = self.recall(text)
            self.assertIsNone(result["error"])
            self.assertIn('Search results for "cats"', result["reply"])
            self.assertEqual(result["search"], self.first["search"])

    def test_command_and_bounded_natural_selectors(self):
        for text in ("/result 2", "  /RESULT 02 ", "show me the second result",
                     "please open the second one.", "show 2"):
            result = self.recall(text)
            self.assertIn("https://example.com/wiki/Cat", result["reply"], text)

    def test_invalid_explicit_selectors_report_errors(self):
        for text in ("/result", "/result 0", "/result -1", "/result 3", "/result two",
                     "/result 1.5", "/result " + "9" * 5000):
            self.assertEqual(self.recall(text)["error"]["kind"], "invalid_result")
        self.assertIsNone(self.recall("/result 1")["error"])

    def test_new_conversation_has_no_context(self):
        for text in ("/results", "show the second one"):
            result = self.recall(text, {})
            self.assertEqual(result["error"]["kind"], "no_search_context")
            self.assertIsNone(result["search"])

    def test_interleaved_conversations_keep_their_own_results(self):
        other = {}
        adapter.handle("/search dogs", other,
                       opener=FakeOpener(FakeResponse(payload(results=[
                           {"title": "Dogs", "url": "https://example.org/dogs"}]))))
        self.assertIn("https://example.org/dogs", self.recall("/result 1", other)["reply"])
        self.assertIn("https://example.org/cats", self.recall("/result 1")["reply"])
        other.clear()
        self.assertIsNone(self.recall("/result 2")["error"])

    def test_recall_keeps_full_returned_snippet_and_handles_missing_one(self):
        snippet = "x" * (adapter.MAX_SNIPPET_CHARS + 50) + " END"
        adapter.handle("/search snippets", self.memory,
                       opener=FakeOpener(FakeResponse(payload(results=[
                           {"title": "Long", "url": "https://example.org/long", "content": snippet},
                           {"title": "Empty", "url": "https://example.org/empty"}]))))
        self.assertIn(snippet, self.recall("/result 1")["reply"])
        self.assertIn("No snippet was returned.", self.recall("/result 2")["reply"])

    def test_reset_by_clearing_conversation_forgets_results(self):
        self.memory.clear()
        self.assertEqual(self.recall("/results")["error"]["kind"], "no_search_context")

    def test_new_search_replaces_previous_list(self):
        adapter.handle("search online for dogs", self.memory,
                       opener=FakeOpener(FakeResponse(payload(results=[
                           {"title": "Dogs", "url": "https://example.org/dogs"}]))))
        result = self.recall("/result 1")
        self.assertEqual(result["search"]["query"], "dogs")
        self.assertIn("https://example.org/dogs", result["reply"])
        self.assertNotIn("https://example.org/cats", result["reply"])

    def test_failed_new_search_invalidates_old_context(self):
        for text, opener in (("/search dogs", FakeOpener(error=urllib.error.URLError("fixture"))),
                             ("/search", FakeOpener())):
            adapter.handle(text, self.memory, opener=opener)
            self.assertEqual(self.recall("/results")["error"]["kind"], "no_search_context")

    def test_empty_search_replaces_old_context(self):
        adapter.handle("/search empty", self.memory,
                       opener=FakeOpener(FakeResponse(payload(results=[]))))
        self.assertIn("no results", self.recall("/results")["reply"])
        self.assertEqual(self.recall("/result 1")["error"]["kind"], "invalid_result")

    def test_returned_records_cannot_mutate_snapshot(self):
        self.first["search"]["results"][0]["title"] = "mutated"
        recalled = self.recall("/results")
        self.assertNotIn("mutated", recalled["reply"])
        recalled["search"]["results"].clear()
        self.assertIsNone(self.recall("/result 2")["error"])

    def test_context_survives_chat_and_stays_out_of_hooks(self):
        class Runtime:
            def turn(inner, text, memory):
                self.assertNotIn(adapter.SEARCH_CONTEXT_KEY, memory)
                memory.clear()
                memory["turns"] = [{"user": text, "bot": "hi"}]
                return {"reply": "hi"}
        adapter.handle("hello", self.memory, opener=self.opener, runtime=Runtime())
        turns = list(self.memory["turns"])
        self.assertIsNone(self.recall("/result 2")["error"])
        self.assertEqual(self.memory["turns"], turns)

    def test_runtime_exception_preserves_context(self):
        class Runtime:
            def turn(inner, text, memory):
                memory.clear()
                raise ValueError("fixture")
        with self.assertRaises(ValueError):
            adapter.handle("hello", self.memory, runtime=Runtime())
        self.assertIsNone(self.recall("/result 1")["error"])

    def test_negated_quoted_multiline_and_unsupported_phrases_stay_local(self):
        for text in ("don't open the second one", '"open the second one"',
                     "I said show the second one", "show the second one\nplease",
                     "tell me about it", "/results extra", "open the first file", "open result 2", None, 42):
            self.assertIsNone(adapter.parse_search_followup(text), text)

    def test_selection_after_deduplication_matches_display(self):
        adapter.handle("/search ordered", self.memory,
                       opener=FakeOpener(FakeResponse(payload(results=[
                           {"title": "Missing URL"},
                           {"title": "First", "url": "https://example.org/1"},
                           {"title": "Duplicate", "url": "https://example.org/1"},
                           {"title": "Second", "url": "https://example.org/2"}]))))
        self.assertIn("https://example.org/2", self.recall("/result 2")["reply"])

    def test_multidigit_result_numbers_and_ordinal_boundary(self):
        adapter.handle("/search many", self.memory, max_results=12,
                       opener=FakeOpener(FakeResponse(payload(results=[
                           {"title": str(i), "url": "https://example.org/%d" % i}
                           for i in range(1, 13)]))))
        for text in ("/result 12", "/result 0012", "show 12"):
            self.assertIn("https://example.org/12", self.recall(text)["reply"])
        self.assertEqual(self.recall("/result 13")["error"]["kind"], "invalid_result")
        self.assertIn("https://example.org/5", self.recall("show the fifth one")["reply"])
        self.assertIsNone(adapter.parse_search_followup("show the sixth one"))

    def test_repeat_preserves_retrieval_notes(self):
        record = adapter.handle("/search notes", self.memory, max_results=1,
                                opener=FakeOpener(FakeResponse(payload(
                                    answers=["Answer card fixture"], suggestions=["Suggestion fixture"],
                                    unresponsive_engines=[["fixture-engine", "timeout"]]))))
        result = self.recall("/results")
        self.assertEqual(result["reply"], "Remembered results (no new search):\n" + record["reply"])
        for text in ("Answer card fixture", "Suggestion fixture", "fixture-engine", "showing 1 of 2"):
            self.assertIn(text, result["reply"])


class LocalFixtureServer(unittest.TestCase):
    """Exercises the real urllib path against a disposable local server."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            LocalFixtureServer.captured.append(self.path)
            if self.path.startswith("/403"):
                self.send_response(403)
                self.end_headers()
                return
            if self.path.startswith("/slow"):
                time.sleep(0.5)
            if self.path.startswith("/short"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b'{"results":[]}')
                self.close_connection = True
                return
            body = json.dumps({"results": [{"title": "Fixture", "url": "https://example.org/f",
                                            "content": "fixture snippet"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    @classmethod
    def setUpClass(cls):
        cls.captured = []
        cls.server = QuietServer(("127.0.0.1", 0), cls.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def base_url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def test_unicode_query_survives_real_http(self):
        record = adapter.search("café 東京", self.base_url(), timeout=5)
        self.assertEqual(record["results"][0]["url"], "https://example.org/f")
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.captured[-1]).query)
        self.assertEqual(params["q"], ["café 東京"])
        self.assertEqual(params["format"], ["json"])

    def test_unreachable_port_is_reported(self):
        with QuietServer(("127.0.0.1", 0), self.Handler) as closed:
            port = closed.server_address[1]
        with self.assertRaises(adapter.SearchError) as caught:
            adapter.search("cats", "http://127.0.0.1:%d" % port, timeout=5)
        self.assertEqual(caught.exception.kind, adapter.UNREACHABLE)

    def test_403_from_real_server(self):
        with self.assertRaises(adapter.SearchError) as caught:
            adapter.search("cats", self.base_url() + "/403", timeout=5)
        self.assertEqual(caught.exception.kind, adapter.FORBIDDEN)

    def test_slow_server_times_out_over_a_real_socket(self):
        started = time.monotonic()
        with self.assertRaises(adapter.SearchError) as caught:
            adapter.search("cats", self.base_url() + "/slow", timeout=0.15)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.kind, adapter.TIMEOUT)
        self.assertLess(elapsed, 0.5, "the timeout should cut the request short")

    def test_truncated_body_is_not_an_empty_search(self):
        # The fixture declares 1000 bytes and sends 14.
        with self.assertRaises(adapter.SearchError) as caught:
            adapter.search("cats", self.base_url() + "/short", timeout=5)
        self.assertEqual(caught.exception.kind, adapter.MALFORMED)
        self.assertIn("of 1000 declared bytes", caught.exception.message)

    def test_unusable_port_in_base_url_is_configuration(self):
        host, _, port = self.base_url().rpartition(":")
        with self.assertRaises(adapter.SearchError) as caught:
            adapter.search("cats", "%s:notaport" % host, timeout=5)
        self.assertEqual(caught.exception.kind, adapter.BAD_CONFIG)


class CommandLineInterface(unittest.TestCase):
    """The CLI contract, run in a subprocess against a fixture server."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"results": [{"title": "CLI fixture result",
                                            "url": "https://example.org/cli",
                                            "content": "cli snippet"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), cls.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.pybot_absent = cls.check_pybot_absent()

    @staticmethod
    def check_pybot_absent():
        """True only when an isolated interpreter genuinely cannot import bot."""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYBOT_PATH", None)
        probe = subprocess.run([sys.executable, "-B", "-I", "-c", "import bot"],
                               capture_output=True, text=True, timeout=60, env=env, cwd=ROOT)
        return probe.returncode != 0

    def require_pybot_absent(self):
        if not self.pybot_absent:
            self.skipTest("bot is importable even in an isolated interpreter, so the "
                          "no-PyBot CLI cases cannot be controlled on this machine")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def run_cli(self, *cli_args, with_pybot=True):
        """Run the CLI. Without with_pybot, isolate from PYTHONPATH and site-packages
        so "PyBot is not importable" is a controlled condition, not a local accident."""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYBOT_PATH", None)
        command = [sys.executable, "-B"]
        if with_pybot and PYBOT_PATH:
            env["PYBOT_PATH"] = PYBOT_PATH
        else:
            command.append("-I")
        return subprocess.run(command + [os.path.join(ROOT, "searxbot.py")] + list(cli_args),
                              capture_output=True, text=True, timeout=180, env=env, cwd=ROOT)

    def test_search_works_without_pybot_installed(self):
        result = self.run_cli("-p", "/search cats", "--base-url",
                              "http://127.0.0.1:%d" % self.server.server_address[1], with_pybot=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1. CLI fixture result", result.stdout)
        self.assertNotIn("Loading PyBot", result.stderr)

    def test_missing_pybot_gives_a_clean_error_for_ordinary_chat(self):
        self.require_pybot_absent()
        result = self.run_cli("-p", "hello", with_pybot=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("PyBot is not importable", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_pybot_path_that_does_not_exist_exits_2_without_a_traceback(self):
        """--pybot-path is applied before retrying, so a typo is a config error."""
        self.require_pybot_absent()
        with tempfile.TemporaryDirectory() as parent:
            missing = os.path.join(parent, "no-such-checkout")
            result = self.run_cli("-p", "hello", "--pybot-path", missing, with_pybot=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(missing, result.stderr)
        self.assertIn("bot", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_pybot_path_without_a_bot_package_exits_2_without_a_traceback(self):
        """An existing directory that is not a PyBot checkout is reported the same way."""
        self.require_pybot_absent()
        with tempfile.TemporaryDirectory() as checkout:
            result = self.run_cli("-p", "hello", "--pybot-path", checkout, with_pybot=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(checkout, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_dependency_missing_inside_pybot_is_not_reported_as_a_bad_path(self):
        """A broken PyBot install keeps its own ImportError instead of exit 2."""
        self.require_pybot_absent()
        with tempfile.TemporaryDirectory() as checkout:
            package = os.path.join(checkout, "bot")
            os.makedirs(package)
            with open(os.path.join(package, "__init__.py"), "w", encoding="utf-8") as init_file:
                init_file.write("import searxbot_fixture_missing_dependency\n")
            result = self.run_cli("-p", "hello", "--pybot-path", checkout, with_pybot=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("searxbot_fixture_missing_dependency", result.stderr)
        self.assertIn("Traceback", result.stderr)

    def test_pybot_directory_without_runtime_is_a_configuration_error(self):
        self.require_pybot_absent()
        with tempfile.TemporaryDirectory() as checkout:
            os.makedirs(os.path.join(checkout, "bot"))
            result = self.run_cli("-p", "hello", "--pybot-path", checkout, with_pybot=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def closed_port(self):
        """A port just released by a bound socket, so connecting to it should fail."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_failed_search_exits_nonzero(self):
        result = self.run_cli("-p", "/search cats", "--base-url",
                              "http://127.0.0.1:%d" % self.closed_port(), with_pybot=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Search failed (unreachable)", result.stdout)

    def test_empty_search_command_exits_nonzero(self):
        result = self.run_cli("-p", "/search", "--json", with_pybot=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["kind"], adapter.EMPTY_QUERY)

    def test_json_output_is_machine_readable(self):
        result = self.run_cli("-p", "/search cats", "--base-url",
                              "http://127.0.0.1:%d" % self.server.server_address[1],
                              "--json", with_pybot=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["source"], "search")
        self.assertEqual(record["search"]["results"][0]["url"], "https://example.org/cli")
        self.assertIsNone(record["error"])

    def test_interactive_recall_and_reset_without_pybot(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYBOT_PATH", None)
        result = subprocess.run(
            [sys.executable, "-B", "-I", os.path.join(ROOT, "searxbot.py"),
             "--base-url", "http://127.0.0.1:%d" % self.server.server_address[1]],
            input="/search cats\n/result 1\nshow the first one\n/reset\n/results\n/exit\n",
            capture_output=True, text=True, timeout=60, env=env, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Remembered result 1 for "cats"', result.stdout)
        self.assertIn("https://example.org/cli", result.stdout)
        self.assertIn("New conversation.", result.stdout)
        self.assertIn("no_search_context", result.stdout)
        self.assertNotIn("Loading PyBot", result.stderr)

    def test_one_shot_recall_without_context_exits_one(self):
        result = self.run_cli("-p", "/result 1", "--json", with_pybot=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"]["kind"], "no_search_context")
        self.assertNotIn("Loading PyBot", result.stderr)

    @unittest.skipIf(PYBOT_PATH is None, "set PYBOT_PATH to test the chat path")
    def test_one_shot_chat_reply_comes_from_a_pybot_module(self):
        result = self.run_cli("-p", "hello", "-v")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())
        self.assertIn("[source: pybot | module:", result.stdout)
        self.assertNotIn("module: none", result.stdout)


@unittest.skipIf(PYBOT_RUNTIME is None,
                 "PyBot is not importable; set PYBOT_PATH to a checkout containing bot/")
class PyBotIntegration(unittest.TestCase):
    """Normal chat must keep working through the adapter."""

    runtime = PYBOT_RUNTIME

    def test_search_context_survives_real_pybot_turn(self):
        random.seed(0)
        memory = {}
        adapter.handle("/search cats", memory, opener=FakeOpener(FakeResponse(payload())))
        adapter.handle("hi, my name is Sam", memory, runtime=self.runtime)
        result = adapter.handle("show the second one", memory)
        self.assertEqual(result["source"], "search_context")
        self.assertIn("https://example.com/wiki/Cat", result["reply"])
        answer = adapter.handle("what is my name?", memory, runtime=self.runtime)
        self.assertIn("Sam", answer["reply"])
        self.assertEqual(len(memory["turns"]), 2)

    def test_plain_chat_still_works_through_the_adapter(self):
        random.seed(0)
        memory = {}
        first = adapter.handle("hi, my name is Sam", memory, runtime=self.runtime)
        self.assertEqual(first["source"], "pybot")
        self.assertTrue(first["reply"].strip())
        self.assertIsNotNone(first["module"], "a bare fallback would hide a broken integration")
        self.assertTrue(first["candidates"], "PyBot should propose at least one candidate")
        self.assertIn(first["module"], [c["module"] for c in first["candidates"]])
        follow_up = adapter.handle("what is my name?", memory, runtime=self.runtime)
        self.assertEqual(follow_up["source"], "pybot")
        self.assertIn("Sam", follow_up["reply"])
        self.assertTrue(memory["turns"], "the runtime must still own memory['turns']")

    def test_search_line_does_not_enter_pybot_memory(self):
        random.seed(0)
        memory = {}
        adapter.handle("hi, my name is Sam", memory, runtime=self.runtime)
        result = adapter.handle("/search SearXNG documentation", memory, runtime=self.runtime,
                                opener=FakeOpener(FakeResponse(payload())))
        self.assertEqual(result["source"], "search")
        self.assertNotIn("/search SearXNG documentation",
                         [turn["user"] for turn in memory["turns"]])


if __name__ == "__main__":
    unittest.main()
