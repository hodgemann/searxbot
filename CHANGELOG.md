# Changelog

The initial tags reconstruct the original development milestones in order.
Each tag is a standalone build. Use `vMAJOR.MINOR.PATCH` for release tags.
During `0.x`, bump the minor version for functionality changes and the patch
version for compatible fixes. Once the public API is stable at `1.0.0`, use a
major bump for incompatible changes.

## v0.3.0

Add `/results` and `/result N` with bounded natural-language recall.
Keep the last search in conversation memory and clear it on reset.

## v0.2.0

Recognize bounded whole-message requests and add the `pybot_search.py` launcher.
Improve the CLI error for a misconfigured PyBot checkout.

Qwen Code contributed the PyBot import fix, regression tests, and review.

## v0.1.0

Route explicit `/search` requests to SearXNG and ordinary chat to PyBot.
Include the Python API, CLI, structured errors, and fixture tests.

Qwen Code contributed to the original adapter and tests. Codex reviewed the integration.
