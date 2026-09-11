"""Prove the free-model adapter without calling any provider.

The HTTP layer is replaced, so nothing here touches Groq, Ollama or anything
else. What is being checked is that a zero-cost brain behaves the same as a
paid one where it matters:

*   The action vocabulary offered to a local model is generated from the one
    the executor implements, so the two cannot drift.
*   Rate limiting - the normal condition on a free tier - is waited out, not
    treated as failure. A dropped case is worse than a slow one.
*   A small model that narrates instead of acting produces a precise, useful
    complaint that the loop can feed back, not a guessed action.
*   Nothing ever quietly selects a paid provider.

    python tools/brain_tests.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from sentinel import brains as brains_module  # noqa: E402
from sentinel.actions import TOOL_SCHEMA, build  # noqa: E402
from sentinel.brains import (  # noqa: E402
    BrainError,
    OpenAICompatibleBrain,
    PROVIDERS,
    _as_openai_tools,
    make_brain,
)

_failures: list[str] = []
_checks = 0


def check(name: str, got, want) -> None:
    global _checks
    _checks += 1
    if got == want:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def check_true(name: str, condition: bool) -> None:
    check(name, bool(condition), True)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


class FakeResponse:
    def __init__(self, status_code=200, body=None, text="", headers=None):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeRequestException(Exception):
    pass


class FakeRequests:
    """Stands in for the requests module inside brains only."""

    RequestException = FakeRequestException

    def __init__(self, post=None, get=None):
        self.post = post or (lambda *a, **k: FakeResponse())
        self.get = get or (lambda *a, **k: FakeResponse())


REAL = brains_module.requests


def install(post=None, get=None):
    brains_module.requests = FakeRequests(post, get)


def restore():
    brains_module.requests = REAL


def tool_reply(name: str, arguments: dict, prompt=100, completion=20):
    return FakeResponse(body={
        "choices": [{"message": {"tool_calls": [
            {"function": {"name": name, "arguments": json.dumps(arguments)}}
        ]}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    })


def env(**kv):
    class _Ctx:
        def __enter__(self):
            self.prev = {k: os.environ.get(k) for k in kv}
            for k, v in kv.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        def __exit__(self, *exc):
            for k, prev in self.prev.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev
    return _Ctx()


# --------------------------------------------------------------------------- #
section("The verbs offered to a free model are the verbs that actually run")

openai_tools = _as_openai_tools()
check("every implemented verb is offered",
      sorted(t["function"]["name"] for t in openai_tools),
      sorted(t["name"] for t in TOOL_SCHEMA))
check_true("each carries its description across the protocols",
           all(t["function"]["description"] for t in openai_tools))
check_true("each carries a parameter schema",
           all("parameters" in t["function"] for t in openai_tools))
check_true("the required verbs for honest adjudication are present",
           {"confirm_screen", "report_attempt", "give_up"}
           <= {t["function"]["name"] for t in openai_tools})


# --------------------------------------------------------------------------- #
section("A decision comes back as a real, buildable action")

install(post=lambda *a, **k: tool_reply("tap", {"index": 7}))
brain = OpenAICompatibleBrain(provider="ollama")
name, arguments = brain.decide("sys", [{"role": "user", "content": "screen"}])
check("the verb survives the round trip", name, "tap")
check("so do its arguments", arguments, {"index": 7})
check_true("and it builds into a real action", build(name, arguments).index == 7)
check("token usage was recorded for the cost report", brain.usage.calls, 1)
check("input tokens counted", brain.usage.input_tokens, 100)
restore()


# --------------------------------------------------------------------------- #
section("Free-tier rate limiting is waited out, not surrendered to")

calls = {"n": 0}


def limited(*a, **k):
    calls["n"] += 1
    if calls["n"] < 3:
        return FakeResponse(status_code=429, text="slow down", headers={"retry-after": "0"})
    return tool_reply("scroll", {"direction": "down"})


install(post=limited)
brains_module.time.sleep = lambda s: None  # do not actually wait in a test
brain = OpenAICompatibleBrain(provider="ollama")
name, arguments = brain.decide("sys", [{"role": "user", "content": "screen"}])
check("it kept trying and got an answer", name, "scroll")
check("it took exactly the attempts it needed", calls["n"], 3)
restore()


# --------------------------------------------------------------------------- #
section("A model that narrates instead of acting is told so precisely")

install(post=lambda *a, **k: FakeResponse(body={
    "choices": [{"message": {"content": "I think you should probably tap the Materials tab."}}],
    "usage": {},
}))
brain = OpenAICompatibleBrain(provider="ollama")
try:
    brain.decide("sys", [{"role": "user", "content": "screen"}])
    check_true("narration is refused rather than guessed at", False)
except BrainError as exc:
    check_true("narration is refused rather than guessed at", True)
    check_true("the complaint quotes what the model actually said",
               "Materials tab" in str(exc))
restore()

install(post=lambda *a, **k: FakeResponse(body={
    "choices": [{"message": {"tool_calls": [
        {"function": {"name": "tap", "arguments": "{not json"}}
    ]}}], "usage": {},
}))
brain = OpenAICompatibleBrain(provider="ollama")
try:
    brain.decide("sys", [{"role": "user", "content": "s"}])
    check_true("unparseable arguments are refused", False)
except BrainError as exc:
    check_true("unparseable arguments are refused", "unparseable" in str(exc))
restore()


# --------------------------------------------------------------------------- #
section("Nothing quietly costs money")

with env(SENTINEL_BRAIN=None, GROQ_API_KEY="k"):
    brain = make_brain()
    check("the default provider is a free one", brain.provider, "groq")

with env(SENTINEL_BRAIN="claude", ANTHROPIC_API_KEY=None):
    try:
        make_brain()
        check_true("a paid brain without a key fails loudly", False)
    except BrainError as exc:
        check_true("a paid brain without a key fails loudly", True)
        check_true("and it names the free alternatives",
                   "groq" in str(exc) and "ollama" in str(exc))

with env(SENTINEL_BRAIN="ollama"):
    brain = make_brain()
    check("a local brain needs no key at all", brain.api_key, "")
    check_true("and points at localhost", "localhost" in brain.url)

with env(SENTINEL_BRAIN="groq", GROQ_API_KEY=None):
    try:
        make_brain()
        check_true("a free tier missing its key says how to get one", False)
    except BrainError as exc:
        check_true("a free tier missing its key says how to get one",
                   "free tier" in str(exc).lower())

with env(SENTINEL_BRAIN="nonsense"):
    try:
        make_brain()
        check_true("an unknown provider is refused", False)
    except BrainError as exc:
        check_true("an unknown provider is refused", "unknown brain" in str(exc))


# --------------------------------------------------------------------------- #
section("The model is a setting, not a code change")

with env(SENTINEL_BRAIN="ollama", SENTINEL_BRAIN_MODEL="llama3.1:8b"):
    brain = make_brain()
    check("the model can be swapped from the environment", brain.model, "llama3.1:8b")

with env(SENTINEL_BRAIN="ollama", SENTINEL_BRAIN_MODEL=None,
         SENTINEL_BRAIN_URL="http://192.168.1.9:11434/v1/chat/completions"):
    brain = make_brain()
    check_true("so can the server it runs on", "192.168.1.9" in brain.url)

check_true("every provider ships a working default model",
           all(p.get("model") for p in PROVIDERS.values()))
check_true("local providers require no key",
           PROVIDERS["ollama"]["key_env"] == "" and PROVIDERS["lmstudio"]["key_env"] == "")


# --------------------------------------------------------------------------- #
section("A run can report what it cost")

install(post=lambda *a, **k: tool_reply("back", {}, prompt=800, completion=40))
brain = OpenAICompatibleBrain(provider="ollama")
for _ in range(5):
    brain.decide("sys", [{"role": "user", "content": "screen"}])
check("calls are counted", brain.usage.calls, 5)
check("input tokens accumulate", brain.usage.input_tokens, 4000)
check("output tokens accumulate", brain.usage.output_tokens, 200)
check_true("the summary is human-readable", "calls" in brain.usage.summary())
restore()


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
print("The brain is swappable, free by default, and never silently paid.")
