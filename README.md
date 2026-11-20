# SearxBot

SearxBot is a small terminal chat program that can search the web. Type a
search request and it asks your own [SearXNG](https://docs.searxng.org/)
server, then shows the titles, links, and short snippets it gets back. Anything
else you type goes to [PyBot](https://github.com/huggingface/pybot), a simple
rule-based chatbot, so you can talk to it normally between searches.

You can also use SearxBot from your own Python code.

## What you need

- **Python 3.8 or newer.** SearxBot has no other Python dependencies.
- **A running SearXNG server** with JSON output turned on. In SearXNG's
  `settings.yml`, make sure `json` is listed under `search.formats`.
  SearxBot looks for SearXNG at `http://127.0.0.1:8888` unless you tell it
  otherwise. If you get a "403" error, JSON output is usually what is missing.
  See the [SearXNG Search API docs](https://docs.searxng.org/dev/search_api.html).
- **PyBot (optional),** for ordinary chat. Searching works without it. SearxBot
  was tested with PyBot revision
  [`7d63c7f`](https://github.com/huggingface/pybot/tree/7d63c7fbebb44c7e466e5c8b5c1f341ab767929e).

## Quick start

From the SearxBot folder, run a single search:

```sh
python3 -m searxbot -p '/search SearXNG documentation'
```

If your SearXNG server is somewhere else, point SearxBot at it:

```sh
python3 -m searxbot --base-url http://localhost:8888 -p '/search Python pathlib'
```

You can also set the `SEARXNG_URL` environment variable instead.

To install a `searxbot` command, run `python3 -m pip install .` in the SearxBot
folder.

## Chatting

To chat as well as search, download PyBot next to SearxBot and start a session:

```sh
git clone https://github.com/huggingface/pybot.git ../pybot
python3 -m searxbot --pybot-path ../pybot
```

On Linux or macOS, `./start.sh` does the same thing. It expects PyBot in a
`pybot` folder next to the SearxBot folder. Set `PYBOT_PATH` or pass
`--pybot-path` to use a different location. Any other options are passed
along, and the script works from any directory if you call it by its full path.

Once it is running, you can type:

| You type | What happens |
| --- | --- |
| `/search QUERY` | Searches the web for QUERY |
| `search the web for QUERY` | Same as `/search` |
| Anything else | PyBot answers |
| `/results` | Shows the last search results again |
| `/result 2` | Shows result number 2 in full |
| `/reset` | Forgets the conversation and the last search |
| `/help` | Lists the commands |
| `/exit` or Ctrl+D | Quits |

Plain-English requests work too, as long as they are the whole message. For
example: `search the internet for cheap flights`, `search online for pasta
recipes`, or `could you please search the web for local weather`. Everything
after "for" becomes the search.

### Going back to earlier results

SearxBot remembers your most recent search, so you can ask about it:

```text
/search Python pathlib documentation
show the second one
/result 1
/results
what did I search for?
```

You can say things like `show the second one` or `open the third one` (first
through fifth), or use `/result N` for any number in the list.

A few things to know:

- Only the latest search is remembered. A new search replaces it, and a failed
  search clears it.
- Results are shown as they were when you searched. They are not fetched again.
- `/reset` clears everything, and each new run of SearxBot starts fresh.

## Command-line options

| Option | What it does |
| --- | --- |
| `-p`, `--prompt TEXT` | Handle one message and exit |
| `-i`, `--interactive` | Keep chatting after the `-p` message |
| `-s`, `--seed N` | Make PyBot's replies repeatable |
| `-v`, `--verbose` | Show where each reply came from and how long it took |
| `--json` | Print each reply as JSON instead of text |
| `--base-url URL` | SearXNG address (overrides `SEARXNG_URL`) |
| `--pybot-path DIR` | PyBot folder (overrides `PYBOT_PATH`) |
| `--timeout SECONDS` | Network timeout, 10 seconds by default |
| `--max-results N` | Show at most N results, 5 by default |
| `--page N` | Which page of results to fetch, 1 by default |
| `--version` | Print the version number |

When you use `-p`, the exit code tells you how it went: **0** means success,
**1** means the search (or result lookup) failed, and **2** means PyBot could
not be loaded or the options were wrong. Error messages are printed to normal
output.

Good to know:

- SearxBot ignores any HTTP proxy set in your environment and connects to
  SearXNG directly.
- The timeout applies to each network step, not the whole search.
- Responses larger than about 2 MB are refused.

## Using SearxBot from Python

```python
from searxbot import handle

memory = {}
result = handle("/search SearXNG documentation", memory)
print(result["reply"])
```

Keep one `memory` dictionary per conversation and pass it to every call so
follow-up questions work. Handle one message at a time per conversation. PyBot
keeps shared state for the whole program, so if you run several conversations
at once, don't call PyBot from more than one at a time.

PyBot must be importable, or you can pass it in yourself with
`runtime=pybot_runtime`.

`handle()` returns a dictionary with these keys:

| Key | Meaning |
| --- | --- |
| `reply` | The text to show the user |
| `source` | `"search"`, `"search_context"` (an answer about earlier results), or `"pybot"` |
| `module` | Which PyBot module answered, if any |
| `candidates` | PyBot's other possible replies |
| `search` | The search details and results, when there are any |
| `error` | `None`, or a dictionary with `kind` and `message` if the search failed |

If you call the lower-level `search()` function directly, it raises
`SearchError` instead of returning an error dictionary.

SearxBot only lists what SearXNG returns, in SearXNG's order. It does not open
the result pages, summarize them, or check them. Search requests are handled
before PyBot sees the message, so each search is sent exactly once.

## Running the tests

```sh
python3 -B -W error::ResourceWarning -m unittest discover -s tests
```

The tests use fake responses and a temporary local server, so they need no
internet access and no SearXNG. To include the PyBot chat tests, set
`PYBOT_PATH`:

```sh
PYBOT_PATH=/path/to/pybot python3 -B -W error::ResourceWarning -m unittest discover -s tests
```

## Version and license

This is version 0.4.1. See the [changelog](CHANGELOG.md) for what changed in
each release. SearxBot is released under the [Apache License 2.0](LICENSE), the
same license as PyBot.
