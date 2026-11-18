#!/usr/bin/env python3
"""Explicit ``/search QUERY`` routing around PyBot.

Standard library only. Lines starting with ``/search`` are answered by a SearXNG
JSON query and never reach ``bot.runtime.turn``; every other line is passed to
PyBot unchanged. A failed search reports the failure instead of falling through
to a conversational reply.

    python3 -m searxbot -p "/search SearXNG documentation"
    python3 -m searxbot                 # interactive chat + /search
    SEARXNG_URL=http://127.0.0.1:8888 python3 -m searxbot -p "/search cats"

This is retrieval only: the adapter shows what SearXNG returned and does not
read or summarize the result pages. ``timeout`` bounds each socket operation,
not the total transfer, so a server that dribbles bytes can take longer.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

__version__ = "0.1.0"

DEFAULT_BASE_URL = "http://127.0.0.1:8888"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 300
MAX_RESPONSE_BYTES = 2_000_000
USER_AGENT = "SearxBot/" + __version__
COMMAND_PREFIX = "/search"

# Kept in sync with the error kinds the tests assert on.
EMPTY_QUERY = "empty_query"
BAD_CONFIG = "bad_config"
UNREACHABLE = "unreachable"
FORBIDDEN = "forbidden"
HTTP_ERROR = "http_error"
TIMEOUT = "timeout"
MALFORMED = "malformed_payload"

_COMMAND = re.compile(r"/search(?P<rest>(?=$|\s).*)\Z", re.IGNORECASE | re.DOTALL)


class SearchError(Exception):
    """A search that did not produce results, with a machine-readable kind."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def parse_search_command(text):
    """The query after ``/search``, or None when the line is ordinary chat.

    Returns "" for a bare ``/search`` so the caller can report an empty query
    instead of handing the command to PyBot.
    """
    if not isinstance(text, str):
        return None
    match = _COMMAND.match(text.strip())
    return match.group("rest").strip() if match else None


def resolve_base_url(base_url=None):
    """The configured SearXNG origin: explicit argument, then ``$SEARXNG_URL``, then the default."""
    return (base_url or os.environ.get("SEARXNG_URL") or DEFAULT_BASE_URL).rstrip("/")


def redact_url(url):
    """A URL safe to show or log: no credentials, no query string."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparsable URL>"
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def search_endpoint(query, base_url=None, page=1):
    """Fully encoded SearXNG JSON search URL for ``query``.

    The base URL must be ASCII and must not carry credentials or a query string,
    so a token in the configuration cannot leak into a request or an error.
    """
    base = resolve_base_url(base_url)
    try:
        parts = urllib.parse.urlsplit(base)
        port = parts.port
    except ValueError as error:
        # The parser's own text can repeat the URL, credentials included.
        raise SearchError(BAD_CONFIG, "Malformed SearXNG base URL %s (%s)."
                          % (redact_url(base), type(error).__name__)) from error
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise SearchError(BAD_CONFIG,
                          "Unsupported SearXNG base URL %s: use http(s) with a plain host, "
                          "no credentials and no query string." % redact_url(base))
    if port is not None and not 0 < port < 65536:
        raise SearchError(BAD_CONFIG, "SearXNG base URL has an out-of-range port: %s." % redact_url(base))
    try:
        base.encode("ascii")
    except UnicodeEncodeError as error:
        raise SearchError(BAD_CONFIG,
                          "SearXNG base URL must be ASCII (%s)." % redact_url(base)) from error
    params = urllib.parse.urlencode(
        {"q": query, "format": "json", "pageno": page}, encoding="utf-8")
    return "%s/search?%s" % (base, params)


def _default_opener():
    # Loopback endpoints must not be routed through an ambient HTTP proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _results(raw_results, max_results):
    """The usable, de-duplicated results, plus counts of what was dropped."""
    results, unusable, duplicates = [], 0, 0
    seen = set()
    for item in raw_results:
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            unusable += 1
            continue
        url = url.strip()
        if url in seen:
            duplicates += 1
            continue
        seen.add(url)
        if len(results) < max_results:
            engines = item.get("engines")
            results.append({
                "title": " ".join(str(item.get("title") or "").split()) or "(untitled)",
                "url": url,
                "snippet": " ".join(str(item.get("content") or "").split()),
                "engines": [str(engine) for engine in engines] if isinstance(engines, list) else [],
            })
    return results, unusable, duplicates


def _engine_failures(raw):
    failures = []
    for entry in raw or []:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            failures.append({"engine": str(entry[0]), "error": str(entry[1])})
        elif entry is not None:
            failures.append({"engine": str(entry), "error": "unresponsive"})
    return failures


def _validate_payload(payload):
    """Check the shape SearXNG's JSON search response actually has.

    A gateway page or an error body is valid JSON but is not a search response,
    so it must not be reported as a successful search with zero results.
    """
    if not isinstance(payload, dict):
        raise SearchError(MALFORMED,
                          "SearXNG sent JSON %s, expected an object." % type(payload).__name__)
    if not isinstance(payload.get("results"), list):
        raise SearchError(MALFORMED,
                          "SearXNG response has no results list (keys: %s)."
                          % ", ".join(sorted(str(key) for key in payload) or "none"))
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        # Not part of the documented search response, but a gateway or plugin
        # that reports failure this way must not look like an empty search.
        raise SearchError(MALFORMED, "SearXNG reported an error: %s" % " ".join(error.split())[:200])
    # An absent or null optional list is treated as an empty one.
    for key in ("answers", "suggestions", "unresponsive_engines"):
        value = payload.get(key)
        if value is not None and not isinstance(value, list):
            raise SearchError(MALFORMED,
                              "SearXNG response field %s is %s, expected a list."
                              % (key, type(value).__name__))
    for item in payload["results"]:
        if not isinstance(item, dict):
            raise SearchError(MALFORMED,
                              "SearXNG result entry is %s, expected an object." % type(item).__name__)


def _validated_timeout(timeout):
    """A positive, finite timeout in seconds."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise SearchError(BAD_CONFIG, "timeout must be a positive number of seconds.")
    try:
        value = float(timeout)
    except OverflowError:
        raise SearchError(BAD_CONFIG, "timeout is too large to use.") from None
    if not math.isfinite(value) or value <= 0:
        raise SearchError(BAD_CONFIG, "timeout must be a positive number of seconds.")
    return value


def search(query, base_url=None, timeout=DEFAULT_TIMEOUT, max_results=DEFAULT_MAX_RESULTS,
           page=1, opener=None):
    """Query SearXNG and return a retrieval record. Raises SearchError on failure."""
    if not isinstance(query, str):
        raise SearchError(EMPTY_QUERY, "The search query must be a string.")
    query = query.strip()
    if not query:
        raise SearchError(EMPTY_QUERY, "The search query is empty.")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results < 1:
        raise SearchError(BAD_CONFIG, "max_results must be a positive integer.")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise SearchError(BAD_CONFIG, "page must be a positive integer.")
    timeout = _validated_timeout(timeout)

    url = search_endpoint(query, base_url, page)
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    opener = opener or _default_opener()

    started = time.perf_counter()
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # urllib leaves the error body open; closing it releases the connection.
        exc.close()
        if exc.code == 403:
            raise SearchError(FORBIDDEN,
                              "SearXNG answered HTTP 403 Forbidden. The json output format is not "
                              "enabled in settings.yml (search.formats), or a proxy in front of "
                              "SearXNG refused the request.") from exc
        raise SearchError(HTTP_ERROR, "SearXNG returned HTTP %s for the search request." % exc.code) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise SearchError(TIMEOUT,
                          "SearXNG did not complete a socket operation within %gs." % timeout) from exc
    except http.client.InvalidURL as exc:
        # Only the exception type is quoted: its text can repeat the URL, including credentials.
        raise SearchError(BAD_CONFIG,
                          "SearXNG base URL is not a usable HTTP endpoint (%s)." % type(exc).__name__) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise SearchError(TIMEOUT,
                              "SearXNG did not complete a socket operation within %gs." % timeout) from exc
        raise SearchError(UNREACHABLE,
                          "Could not reach SearXNG at %s (%s)." % (redact_url(url), exc.reason)) from exc
    except (http.client.HTTPException, ValueError) as exc:
        # Malformed authorities surface as plain ValueErrors or http.client errors
        # rather than URLError, and must stay structured. The exception text is left
        # out because it can echo the request URL and query.
        raise SearchError(UNREACHABLE,
                          "SearXNG request to %s failed (%s)."
                          % (redact_url(url), type(exc).__name__)) from exc
    try:
        with response:
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            declared = response.headers.get("Content-Length")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except (http.client.HTTPException, OSError) as exc:
        raise SearchError(UNREACHABLE,
                          "The SearXNG response from %s ended early (%s)."
                          % (redact_url(url), type(exc).__name__)) from exc
    latency = round(time.perf_counter() - started, 3)

    if content_type not in {"application/json", "text/json"}:
        raise SearchError(MALFORMED,
                          "SearXNG answered with %s, not JSON." % (content_type or "an unknown type"))
    if len(body) > MAX_RESPONSE_BYTES:
        raise SearchError(MALFORMED,
                          "SearXNG response exceeded %d bytes." % MAX_RESPONSE_BYTES)
    # A bounded read returns early instead of raising IncompleteRead, so a body
    # shorter than the declared length has to be detected here.
    try:
        declared_bytes = int(declared.strip()) if declared else None
    except ValueError:
        declared_bytes = None
    if declared_bytes is not None and len(body) < declared_bytes:
        raise SearchError(MALFORMED,
                          "SearXNG sent %d of %d declared bytes." % (len(body), declared_bytes))
    try:
        text = body.decode(charset)
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    except UnicodeDecodeError as exc:
        raise SearchError(MALFORMED, "SearXNG response is not valid %s (%s)." % (charset, exc)) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SearchError(MALFORMED, "SearXNG sent JSON that could not be parsed (%s)." % exc) from exc
    _validate_payload(payload)
    results, unusable, duplicates = _results(payload["results"], max_results)

    return {
        "query": query,
        "request_url": url,
        "retrieved_at": retrieved_at,
        "latency_seconds": latency,
        "page": page,
        "results": results,
        "answers": [str(answer) for answer in payload.get("answers") or [] if str(answer).strip()],
        "suggestions": [str(s) for s in payload.get("suggestions") or [] if str(s).strip()],
        "unresponsive_engines": _engine_failures(payload.get("unresponsive_engines")),
        "result_count_reported": len(payload["results"]),
        "unusable_results": unusable,
        "duplicate_results": duplicates,
    }


def _clip(text, limit=MAX_SNIPPET_CHARS):
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def format_results(record):
    """Plain-text reply for a retrieval record, labelled as retrieved data."""
    header = 'Search results for "%s" — SearXNG, %s, %.2fs' % (
        record["query"], record["retrieved_at"], record["latency_seconds"])
    lines = [header, ""]
    for answer in record["answers"]:
        lines.append("Answer card: %s" % _clip(answer))
        lines.append("")
    if not record["results"]:
        lines.append("SearXNG returned no results for this query."
                     if not record["result_count_reported"]
                     else "SearXNG returned results, but none had a usable URL.")
    for index, item in enumerate(record["results"], start=1):
        engines = ", ".join(item["engines"])
        lines.append("%d. %s" % (index, item["title"]))
        lines.append("   %s" % item["url"])
        if item["snippet"]:
            suffix = " (from: %s)" % engines if engines else ""
            lines.append("   %s%s" % (_clip(item["snippet"]), suffix))
        lines.append("")
    dropped = []
    if record["result_count_reported"] > len(record["results"]):
        dropped.append("showing %d of %d reported"
                       % (len(record["results"]), record["result_count_reported"]))
    if record["unusable_results"]:
        dropped.append("%d without a usable URL" % record["unusable_results"])
    if record["duplicate_results"]:
        dropped.append("%d duplicate URLs" % record["duplicate_results"])
    if dropped:
        lines.append("Note: %s." % ", ".join(dropped))
    if record["unresponsive_engines"]:
        failed = ", ".join("%s (%s)" % (f["engine"], f["error"]) for f in record["unresponsive_engines"])
        lines.append("Engines that did not answer: %s" % failed)
    if record["suggestions"]:
        lines.append("Suggestions: %s" % ", ".join(record["suggestions"][:5]))
    lines.append("")
    lines.append("Retrieved from SearXNG. PyBot did not generate or verify these answers.")
    return "\n".join(lines).strip()


def format_error(error):
    """Plain-text reply for a failed search."""
    if isinstance(error, SearchError):
        kind, message = error.kind, error.message
    else:
        kind, message = error["kind"], error["message"]
    return "Search failed (%s): %s" % (kind, message)


def add_pybot_to_path(path):
    """Make a PyBot checkout importable when its ``bot`` package is not on sys.path."""
    sys.path.insert(0, os.path.abspath(path))


def _load_runtime():
    """Import PyBot's runtime from an importable ``bot`` package.

    When ``bot`` is not importable, fall back to the checkout named by
    ``PYBOT_PATH``. Nothing here assumes a particular install location.
    """
    try:
        from bot import runtime
    except ModuleNotFoundError as error:
        if error.name != "bot":
            raise
        path = os.environ.get("PYBOT_PATH")
        if not path:
            raise RuntimeError(
                "PyBot is not importable. Put its `bot` package on PYTHONPATH, set PYBOT_PATH "
                "to a PyBot checkout, or pass --pybot-path.") from error
        add_pybot_to_path(path)
        from bot import runtime
    return runtime


def handle(text, memory, base_url=None, timeout=DEFAULT_TIMEOUT,
           max_results=DEFAULT_MAX_RESULTS, page=1, opener=None, runtime=None):
    """One routed turn.

    Returns ``{"reply", "source", "module", "candidates", "search", "error"}``.
    ``source`` is "search" for ``/search`` lines and "pybot" for everything else.
    Search turns never call PyBot and never write ``memory["turns"]``.

    ``runtime`` may be a PyBot runtime module, a callable returning one, or None
    to import it on demand. It is never imported for a search line.
    """
    query = parse_search_command(text)
    if query is None:
        if callable(runtime):
            runtime = runtime()
        runtime = runtime or _load_runtime()
        result = runtime.turn(text, memory)
        return {"reply": result["reply"], "source": "pybot", "module": result.get("module"),
                "candidates": result.get("candidates", []), "search": None, "error": None}
    try:
        record = search(query, base_url, timeout, max_results, page, opener)
    except SearchError as error:
        return {"reply": format_error(error), "source": "search", "module": None,
                "candidates": [], "search": None, "error": {"kind": error.kind, "message": error.message}}
    return {"reply": format_results(record), "source": "search", "module": None,
            "candidates": [], "search": record, "error": None}


HELP = """Commands:
  /search QUERY   ask SearXNG and show retrieved results
  /reset          start a new conversation (PyBot forgets what was said)
  /help           show this help
  /exit           quit (Ctrl+D works too)"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="searxbot",
        description="Chat with PyBot, with explicit /search QUERY routed to SearXNG.",
        epilog="Exit status: 0 = handled, 1 = a one-shot search failed, "
               "2 = PyBot is not importable.")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("-p", "--prompt", metavar="TEXT", help="handle this one line and exit")
    parser.add_argument("-i", "--interactive", action="store_true", help="with -p: keep chatting")
    parser.add_argument("-s", "--seed", type=int, metavar="N", help="seed PyBot's wording choices")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show the routing source, module, and timing after each reply")
    parser.add_argument("--base-url", metavar="URL", help="SearXNG base URL (default %s or $SEARXNG_URL)"
                        % DEFAULT_BASE_URL)
    parser.add_argument("--pybot-path", metavar="DIR",
                        help="a PyBot checkout containing the bot package (or set $PYBOT_PATH)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS")
    parser.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS, metavar="N")
    parser.add_argument("--page", type=int, default=1, metavar="N", help="SearXNG result page")
    parser.add_argument("--json", action="store_true", help="print the full routing record as JSON")
    args = parser.parse_args(argv)

    if args.pybot_path:
        os.environ["PYBOT_PATH"] = args.pybot_path
    if args.seed is not None:
        random.seed(args.seed)
    runtime = None

    def ensure_runtime():
        """Import PyBot the first time a line actually needs it."""
        nonlocal runtime
        if runtime is None:
            print("Loading PyBot...", end="", file=sys.stderr, flush=True)
            try:
                runtime = _load_runtime()
            except RuntimeError as error:
                parser.exit(2, "\r" + " " * 16 + "\n%s\n" % error)
            print("\r" + " " * 16 + "\r", end="", file=sys.stderr, flush=True)
        return runtime

    memory = {}

    def run(text):
        start = time.perf_counter()
        result = handle(text, memory, args.base_url, args.timeout, args.max_results,
                        args.page, None, ensure_runtime)
        return result, time.perf_counter() - start

    def show(result, seconds):
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(result["reply"])
        if args.verbose:
            print("[source: %s | module: %s | %.0f ms]"
                  % (result["source"], result["module"] or "none", seconds * 1000))

    if args.prompt is not None and not args.interactive:
        result, seconds = run(args.prompt)
        show(result, seconds)
        return 1 if result["error"] else 0

    print("PyBot + SearXNG search. %s" % HELP)
    first = args.prompt
    while True:
        if first is not None:
            text, first = first, None
            print("user: " + text)
        else:
            try:
                text = input("user: ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
        command = text.strip().lower()
        if command in ("/exit", "/quit", "/bye"):
            break
        if command == "/help":
            print(HELP)
            continue
        if command == "/reset":
            memory = {}
            if args.seed is not None:
                random.seed(args.seed)
            print("New conversation.")
            continue
        if not text.strip():
            continue
        result, seconds = run(text)
        show(result, seconds)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
