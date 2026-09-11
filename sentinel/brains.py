"""Where the thinking comes from - and how to get it for nothing.

The agent needs a model to decide its next action. That is the only part of
this whole system that costs money: BrowserStack's free tier covers the cloud
run, Maestro is open source, and the device is one you already own. So the
cost of a run is entirely the cost of this file's choices.

It is written to make that cost zero. One adapter speaks the OpenAI
chat-completions protocol, which is also spoken by Groq, Ollama, OpenRouter,
LM Studio, llama.cpp and vLLM - so a free hosted tier and a local model on
your own GPU are the same code path with a different base URL.

WHY A SMALL FREE MODEL IS ENOUGH HERE

Not because small models are secretly as good, but because this agent's job
was deliberately made narrow. On each step the model sees roughly 900
characters - a numbered list of what is on one screen - and picks one of
eight verbs against an element index. It never parses XML, never composes a
selector, never chooses from an open-ended action space. That is a job a 7B
model does reliably; free-form phone automation is not, and this design is
the difference.

Where a weaker model does show through is judgment under ambiguity - reading
a genuinely confusing screen, or deciding a case is unattemptable. Those
surface as `give_up`, which is honest and cheap, rather than as a confidently
wrong verdict, because no model here is ever allowed to decide a verdict.

CHOOSING ONE

    SENTINEL_BRAIN=groq       (free tier, fastest, strongest - needs signup)
    SENTINEL_BRAIN=ollama     (free forever, offline, no signup, no limits)
    SENTINEL_BRAIN=openrouter (free model variants)
    SENTINEL_BRAIN=claude     (paid; used if ANTHROPIC_API_KEY is present)

Everything else - model name, base URL - has a sensible default per provider
and can be overridden with SENTINEL_BRAIN_MODEL and SENTINEL_BRAIN_URL.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from sentinel.actions import TOOL_SCHEMA


class BrainError(Exception):
    """The model could not be reached, or would not answer usefully."""


@dataclass
class Usage:
    """What a run cost, in the only units that matter for the report."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    provider: str = ""
    model: str = ""

    def note(self, prompt: int, completion: int, elapsed: float) -> None:
        self.calls += 1
        self.input_tokens += prompt
        self.output_tokens += completion
        self.seconds += elapsed

    def summary(self) -> str:
        return (
            f"{self.provider}/{self.model}: {self.calls} calls, "
            f"{self.input_tokens:,} in / {self.output_tokens:,} out, "
            f"{self.seconds:.0f}s of thinking"
        )


# --------------------------------------------------------------------------- #
# Provider defaults. Base URL and a model that is free on that provider.

PROVIDERS: dict[str, dict[str, str]] = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        # Confirmed against the live model list, not assumed: Groq rotates its
        # lineup and llama-3.3-70b-versatile - the obvious choice a few months
        # ago - is no longer offered at all. A stale default here fails on the
        # first call of a run, so this is worth re-checking whenever the
        # provider changes (tools/brain_check.py --list).
        "model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
    },
    "ollama": {
        "url": "http://localhost:11434/v1/chat/completions",
        "model": "qwen2.5:7b-instruct",
        "key_env": "",  # local, no key
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "key_env": "OPENROUTER_API_KEY",
    },
    "lmstudio": {
        "url": "http://localhost:1234/v1/chat/completions",
        "model": "local-model",
        "key_env": "",
    },
}


def _as_openai_tools() -> list[dict[str, Any]]:
    """The same action vocabulary, in the other protocol's shape.

    Generated from TOOL_SCHEMA rather than written out again, so the verbs a
    local model is offered cannot drift from the verbs the executor
    implements.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for tool in TOOL_SCHEMA
    ]


class OpenAICompatibleBrain:
    """Any server speaking OpenAI chat-completions: hosted free tier or local.

    Deliberately built on plain HTTP rather than a vendor SDK. The protocol is
    small, every candidate provider implements it, and depending on one
    vendor's client library to talk to four different providers would be the
    tail wagging the dog.
    """

    def __init__(
        self,
        provider: str = "groq",
        model: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        timeout: int = 120,
        # Real, live-caught reason this is not 3: a local DNS resolver
        # (Cloudflare WARP's warp-svc, confirmed via nslookup) proved
        # intermittently flaky rather than cleanly up or down - one failing
        # call showed 3 genuine connection failures interleaved with 13
        # successful-but-rate-limited attempts, meaning resolution was
        # working *some* of the time throughout. A short retry budget treats
        # that pattern as a hard failure when waiting it out would have
        # worked; each retry costs nothing but a little wall-clock time.
        max_retries: int = 8,
    ) -> None:
        defaults = PROVIDERS.get(provider, PROVIDERS["groq"])
        self.provider = provider
        self.model = model or os.environ.get("SENTINEL_BRAIN_MODEL") or defaults["model"]
        self.url = url or os.environ.get("SENTINEL_BRAIN_URL") or defaults["url"]
        key_env = defaults.get("key_env", "")
        self.api_key = api_key or (os.environ.get(key_env, "") if key_env else "")
        if key_env and not self.api_key:
            raise BrainError(
                f"{provider} needs {key_env} set. It is a free tier - sign up, "
                f"create a key, and put it in .env as {key_env}=..."
            )
        # temperature 0: the route may vary between runs for good reasons, but
        # it should not vary for no reason. Verdicts do not depend on the
        # route, yet a needlessly wandering agent still costs time and tokens.
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.usage = Usage(provider=provider, model=self.model)
        self.tools = _as_openai_tools()

    def decide(self, system: str, messages: list[dict[str, Any]]) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": self.tools,
            "tool_choice": "required",
            "max_tokens": 512,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last: Exception | None = None
        real_failures = 0
        attempt = 0
        max_attempts = self.max_retries + 20  # generous ceiling on rate-limit waits alone
        while real_failures < self.max_retries and attempt < max_attempts:
            attempt += 1
            started = time.monotonic()
            try:
                response = requests.post(
                    self.url, json=payload, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last = exc
                real_failures += 1
                time.sleep(2 * real_failures)
                continue

            # Free tiers rate-limit on a per-minute token budget, not a daily
            # one - confirmed live (Groq: 8000 tokens/min on gpt-oss-120b).
            # That is small enough that a multi-step agent run hits it
            # routinely, and it always clears within the window the response
            # itself states. So this does NOT count against max_retries: a
            # 429 with a known wait time is not the same kind of problem as a
            # dropped connection, and treating it as one would abandon a live
            # run over something that was always going to resolve itself.
            if response.status_code == 429:
                wait = float(
                    response.headers.get("retry-after")
                    or response.headers.get("x-ratelimit-reset-tokens", "5").rstrip("s")
                    or 5
                )
                time.sleep(min(wait, 65) + 0.5)
                last = BrainError("rate limited")
                continue
            if response.status_code >= 400:
                # Real, live-confirmed provider quirk: with tool_choice
                # "required", gpt-oss models sometimes emit a perfectly good
                # action as raw JSON in the content field rather than as a
                # tool call, and the API rejects its own response with a 400
                # rather than returning it. The generation is handed back in
                # the error body though, so the decision is not actually lost
                # - throwing it away would discard a step the model got right
                # and cost a retry for nothing.
                recovered = _recover(response)
                if recovered:
                    self.usage.note(0, 0, time.monotonic() - started)
                    return recovered
                raise BrainError(
                    f"{self.provider} returned {response.status_code}: {response.text[:300]}"
                )

            elapsed = time.monotonic() - started
            try:
                body = response.json()
            except ValueError as exc:
                raise BrainError(f"{self.provider} returned non-JSON: {exc}") from exc

            usage = body.get("usage") or {}
            self.usage.note(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), elapsed
            )
            return _extract_call(body, self.provider)

        raise BrainError(
            f"{self.provider} unreachable after {real_failures} real failure(s) "
            f"(and {attempt - real_failures} rate-limit wait(s)): {last}"
        )


def _tool_signatures() -> list[tuple[str, set[str], set[str]]]:
    """(name, required keys, all allowed keys) for every verb, from one source."""
    signatures = []
    for tool in TOOL_SCHEMA:
        schema = tool.get("input_schema", {})
        properties = set(schema.get("properties", {}))
        required = set(schema.get("required", []))
        signatures.append((tool["name"], required, properties))
    return signatures


def _identify(arguments: dict) -> str | None:
    """Work out which verb a bare argument object was meant to be.

    The verbs have distinctive shapes - only `finish` takes
    {summary, reached_target_screen}, only `give_up` takes {reason} - so a
    generation that lost its tool name can usually be matched back to exactly
    one. Ambiguity returns nothing rather than guessing, because a wrong verb
    would be acted on.
    """
    keys = set(arguments)
    matches = [
        name
        for name, required, allowed in _tool_signatures()
        if required <= keys <= allowed
    ]
    return matches[0] if len(matches) == 1 else None


def _recover(response: Any) -> tuple[str, dict] | None:
    """Salvage a good decision from a provider error that carries it."""
    try:
        body = response.json()
    except Exception:
        return None
    error = body.get("error") or {}
    if error.get("code") != "tool_use_failed":
        return None
    generation = error.get("failed_generation")
    if not generation:
        return None
    try:
        parsed = json.loads(generation)
    except (json.JSONDecodeError, TypeError):
        return None

    # Some generations carry the name themselves; most are bare arguments.
    if isinstance(parsed, dict) and "name" in parsed and isinstance(
        parsed.get("arguments") or parsed.get("parameters"), dict
    ):
        return parsed["name"], (parsed.get("arguments") or parsed.get("parameters"))
    if not isinstance(parsed, dict):
        return None
    name = _identify(parsed)
    return (name, parsed) if name else None


def _extract_call(body: dict, provider: str) -> tuple[str, dict]:
    """Pull the chosen action out, and say clearly when there isn't one."""
    choices = body.get("choices") or []
    if not choices:
        raise BrainError(f"{provider} returned no choices: {str(body)[:300]}")
    message = choices[0].get("message") or {}
    calls = message.get("tool_calls") or []
    if not calls:
        # Smaller models sometimes narrate instead of calling a tool. Say so
        # precisely - the loop feeds this back and lets the model correct
        # itself, which works far better than silently guessing an action.
        content = (message.get("content") or "").strip()
        raise BrainError(
            f"{provider} answered with words instead of an action: {content[:200]!r}"
        )
    function = calls[0].get("function") or {}
    name = function.get("name", "")
    raw_args = function.get("arguments", "{}")
    if isinstance(raw_args, dict):
        return name, raw_args
    try:
        return name, json.loads(raw_args or "{}")
    except json.JSONDecodeError as exc:
        raise BrainError(f"{provider} sent unparseable arguments {raw_args!r}: {exc}") from exc


# --------------------------------------------------------------------------- #
# The compiler speaks Anthropic's shape. Rather than rewrite a tested module,
# give it a client that answers to the same calls and talks to a free provider.


@dataclass
class _ToolUse:
    """One tool call, in the shape compiler.py already knows how to read."""

    name: str
    input: dict
    id: str = "call_0"
    type: str = "tool_use"


@dataclass
class _Reply:
    content: list[_ToolUse]


class AnthropicShapedClient:
    """`client.messages.create(...)` over an OpenAI-compatible endpoint.

    Exists so the compile stage costs nothing without touching compiler.py,
    which is covered by tests that would otherwise all need rewriting to
    prove the same behaviour again. The translation is small and total: tools
    and tool_choice into the other protocol's spelling on the way out, tool
    calls back into Anthropic-shaped blocks on the way in, and the repair
    round-trip's assistant/tool_result pair converted in both directions so
    the compiler's second attempt still carries the rejection with it.
    """

    def __init__(self, brain: OpenAICompatibleBrain) -> None:
        self.brain = brain
        self.messages = self  # so `client.messages.create(...)` resolves

    def create(
        self,
        model: str = "",
        max_tokens: int = 4096,
        system: str = "",
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        messages: list[dict] | None = None,
        **_: Any,
    ) -> _Reply:
        payload_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
            for tool in (tools or [])
        ]
        forced = (tool_choice or {}).get("name")
        choice: Any = "required"
        if forced:
            choice = {"type": "function", "function": {"name": forced}}

        payload = {
            "model": self.brain.model,
            "temperature": self.brain.temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                *(_to_openai(m) for m in (messages or [])),
            ],
            "tools": payload_tools,
            "tool_choice": choice,
        }
        headers = {"Content-Type": "application/json"}
        if self.brain.api_key:
            headers["Authorization"] = f"Bearer {self.brain.api_key}"

        last: Exception | None = None
        real_failures = 0
        attempt = 0
        max_attempts = self.brain.max_retries + 20
        while real_failures < self.brain.max_retries and attempt < max_attempts:
            attempt += 1
            started = time.monotonic()
            try:
                response = requests.post(
                    self.brain.url, json=payload, headers=headers,
                    timeout=self.brain.timeout,
                )
            except requests.RequestException as exc:
                last = exc
                real_failures += 1
                time.sleep(2 * real_failures)
                continue

            # Same distinction as OpenAICompatibleBrain.decide(): a 429 with a
            # known wait time is not a failure worth counting against the
            # retry budget - the compile stage does 85 sequential calls on
            # this same client, and abandoning a case over a per-minute
            # token window that was always going to clear would be a self
            # -inflicted gap in coverage.
            if response.status_code == 429:
                wait = float(
                    response.headers.get("retry-after")
                    or response.headers.get("x-ratelimit-reset-tokens", "5").rstrip("s")
                    or 5
                )
                time.sleep(min(wait, 65) + 0.5)
                last = BrainError("rate limited")
                continue
            if response.status_code >= 400:
                recovered = _recover(response)
                if recovered:
                    name, arguments = recovered
                    return _Reply(content=[_ToolUse(name=name, input=arguments)])
                raise BrainError(
                    f"{self.brain.provider} returned {response.status_code}: "
                    f"{response.text[:300]}"
                )

            body = response.json()
            usage = body.get("usage") or {}
            self.brain.usage.note(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                time.monotonic() - started,
            )
            message = (body.get("choices") or [{}])[0].get("message") or {}
            blocks = []
            for index, call in enumerate(message.get("tool_calls") or []):
                function = call.get("function") or {}
                raw = function.get("arguments", "{}")
                try:
                    parsed = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                except json.JSONDecodeError:
                    continue
                blocks.append(_ToolUse(
                    name=function.get("name", ""), input=parsed,
                    id=call.get("id") or f"call_{index}",
                ))
            # No tool call is not an exception here: compiler.py already
            # treats an empty result as "the model did not emit a plan" and
            # retries, which is the behaviour worth preserving.
            return _Reply(content=blocks)

        raise BrainError(f"{self.brain.provider} unreachable: {last}")


def _to_openai(message: dict) -> dict | list[dict]:
    """Translate one message, including the compiler's repair round-trip."""
    content = message.get("content")

    if message.get("role") == "assistant" and isinstance(content, list):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.input)},
                }
                for block in content
                if isinstance(block, _ToolUse)
            ],
        }

    if isinstance(content, list) and content and isinstance(content[0], dict):
        first = content[0]
        if first.get("type") == "tool_result":
            return {
                "role": "tool",
                "tool_call_id": first.get("tool_use_id", "call_0"),
                "content": str(first.get("content", "")),
            }

    return {"role": message.get("role", "user"), "content": content}


def make_compiler_client(provider: str | None = None) -> Any:
    """A client the compiler can use, on whichever brain is configured.

    Returns None when Claude is explicitly chosen and available, so the
    compiler falls back to its own Anthropic client and nothing changes for
    anyone already paying for that.
    """
    provider = (provider or os.environ.get("SENTINEL_BRAIN") or "groq").lower()
    if provider == "claude":
        return None
    brain = make_brain(provider)
    return AnthropicShapedClient(brain)


def make_brain(provider: str | None = None) -> Any:
    """Build whichever brain the environment asks for.

    Defaults to a free provider. Claude is used only when explicitly chosen
    and a key exists, so nothing here ever quietly starts costing money.
    """
    provider = (provider or os.environ.get("SENTINEL_BRAIN") or "groq").lower()

    if provider == "claude":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise BrainError(
                "SENTINEL_BRAIN=claude but ANTHROPIC_API_KEY is not set. "
                "Free alternatives: SENTINEL_BRAIN=groq or SENTINEL_BRAIN=ollama."
            )
        from sentinel.agent import ClaudeBrain

        return ClaudeBrain()

    if provider not in PROVIDERS:
        raise BrainError(
            f"unknown brain {provider!r}; choose one of "
            f"{sorted([*PROVIDERS, 'claude'])}"
        )
    return OpenAICompatibleBrain(provider=provider)


def preflight(brain: Any) -> list[str]:
    """Problems worth finding before an hour of device time, not during it."""
    problems: list[str] = []
    if isinstance(brain, OpenAICompatibleBrain) and brain.provider in ("ollama", "lmstudio"):
        host = brain.url.rsplit("/v1/", 1)[0]
        try:
            requests.get(host, timeout=5)
        except requests.RequestException:
            problems.append(
                f"no local model server answering at {host}. Start it "
                f"(`ollama serve`) and make sure {brain.model} is pulled "
                f"(`ollama pull {brain.model}`)."
            )
    return problems
