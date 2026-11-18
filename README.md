# SearxBot

SearxBot connects a terminal or Python application to SearXNG. It displays
titles, links, and snippets for explicit search requests and can route ordinary
messages to PyBot for rule-based chat.
[SearXNG](https://docs.searxng.org/) ·
[Hugging Face PyBot](https://github.com/huggingface/pybot)

## Requirements

- Python 3.8 or newer. SearxBot uses the standard library at runtime.
- A reachable SearXNG instance with `json` included in `search.formats` in its
  settings. The default URL is `http://127.0.0.1:8888`. A 403 can indicate a
  disabled output format or another server access policy.
  [SearXNG Search API](https://docs.searxng.org/dev/search_api.html)
- PyBot for ordinary chat. It is loaded on the first chat turn.
  [Tested PyBot revision `7d63c7f`](https://github.com/huggingface/pybot/tree/7d63c7fbebb44c7e466e5c8b5c1f341ab767929e)

## Run from a checkout

```sh
python3 -m searxbot --version
python3 -m searxbot -p '/search SearXNG documentation'
python3 -m searxbot --base-url http://localhost:8888 -p '/search Python pathlib'
```

To install the `searxbot` command from the repository, run `python3 -m pip install .`
in your Python environment. From a checkout, `python3 -m searxbot` works directly.

## Chat with PyBot

Clone PyBot separately and pass its checkout directory.

```sh
git clone https://github.com/huggingface/pybot.git ../pybot
python3 -m searxbot --pybot-path ../pybot
```

Enter `/search QUERY` for web results, an ordinary message for PyBot chat,
`/reset` to clear the conversation, `/help` for commands, or `/exit` to leave.

Whole-message requests also work, including `search the web for QUERY`,
`search the internet for QUERY`, and `search online for QUERY`. The wording can
include `please` or `can/could/would you`. Text after `for` is sent as the query.

## Command line

| Option | Purpose |
| --- | --- |
| `-p`, `--prompt TEXT` | Handle one line and exit |
| `-i`, `--interactive` | Stay in chat after `-p` |
| `-s`, `--seed N` | Seed PyBot's wording choices |
| `-v`, `--verbose` | Show routing source, winning PyBot module, and timing |
| `--json` | Print a structured record for each handled turn |
| `--base-url URL` | Set the SearXNG base URL, overriding `SEARXNG_URL` |
| `--pybot-path DIR` | Set the PyBot checkout path, overriding `PYBOT_PATH` |
| `--timeout SECONDS` | Set the socket-operation timeout, default 10 seconds |
| `--max-results N` | Cap results after dropping duplicates, default 5 |
| `--page N` | Request a SearXNG result page, default 1 |
| `--version` | Print the installed or checkout version |

One-shot exit codes are 0 for a handled turn, 1 for a failed search,
and 2 for a PyBot import configuration error or invalid CLI arguments. Search
errors are printed to standard output. Requests bypass environment HTTP proxy
settings. The timeout applies to each socket operation. Responses over
2,000,000 bytes are rejected.

## Python API

```python
from searxbot import handle

memory = {}
result = handle("/search SearXNG documentation", memory)
print(result["reply"])
```

Use one `memory` dictionary per conversation and serialize turns in each
conversation. Serialize PyBot calls across conversations because the runtime
uses process-wide state. Make PyBot importable or pass its runtime as
`runtime=pybot_runtime`. The returned record has `reply`, `source`, `module`,
`candidates`, `search`, and `error` fields.
`source` can be "search" or "pybot". `search` holds retrieval metadata and
results when available. A handled search
failure has an `error` dictionary with `kind` and `message`. The lower-level
`search()` function raises `SearchError` for those failures.

SearxBot dispatches search before PyBot's response hooks, so a search request is
issued once per recognized turn. SearxBot displays engine-supplied snippets in
SearXNG's order. Search stops at retrieval of the result list.

## Tests

```sh
python3 -B -W error::ResourceWarning -m unittest discover -s tests
PYBOT_PATH=/path/to/pybot python3 -B -W error::ResourceWarning -m unittest discover -s tests
```

The suite uses mock responses and disposable loopback servers. Set `PYBOT_PATH`
to include PyBot chat integration tests.

## Releases and license

Version 0.2.0. See [the changelog](CHANGELOG.md) for release history.
SearxBot uses the [Apache License 2.0](LICENSE), the same license as PyBot.
