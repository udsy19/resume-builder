"""
Model providers behind one interface.

The agent only needs one capability: stream a call that may carry a PDF, may be
constrained to a JSON schema, and reports reasoning progress as it goes. Anthropic and
OpenAI both offer that, with different spellings — this module normalizes them so the
loop, prompts and evaluation logic are provider-agnostic.

Both providers stream the same event shapes:
    {"event": "thinking", "phase", "text"}   summarized reasoning
    {"event": "writing",  "phase", "chars"}  output progress
    {"event": "done", "text", "usage"}       final text + token usage
"""

import base64
import json
import os
from dataclasses import dataclass
from typing import AsyncGenerator, Dict, List, Optional

# Anthropic effort names map 1:1 onto OpenAI's on gpt-5.6; older OpenAI models cap at
# "high", so anything above it is clamped rather than 400-ing mid-run.
_OPENAI_FULL_EFFORT = ("gpt-5.6", "gpt-5.5", "gpt-5.4", "gpt-5.2")
_EFFORT_CEILING = {"xhigh": "high", "max": "high"}


class ProviderError(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


class Provider:
    """Common surface. `name` is what the UI shows; `model` is the concrete model id."""

    name = "provider"

    def __init__(self, model: str, api_key: Optional[str] = None):
        self.model = model

    async def stream(self, *, phase: str, system, blocks: List[Dict],
                     schema: Optional[dict], effort: str,
                     max_tokens: int) -> AsyncGenerator[Dict, None]:
        raise NotImplementedError


# ── Anthropic ────────────────────────────────────────────────────────

class AnthropicProvider(Provider):
    name = "anthropic"
    DEFAULT_MODEL = "claude-opus-5"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        import anthropic

        key = (api_key or "").strip() or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set.")
        self.model = model or self.DEFAULT_MODEL
        self.client = anthropic.AsyncAnthropic(api_key=key)

    async def stream(self, *, phase, system, blocks, schema, effort, max_tokens):
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": blocks}],
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
        if schema:
            kwargs["output_config"]["format"] = {"type": "json_schema", "schema": schema}

        buf, chars = "", 0
        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type != "content_block_delta":
                    continue
                if event.delta.type == "thinking_delta":
                    buf += event.delta.thinking
                    if len(buf) >= 90 or "\n" in buf:
                        yield {"event": "thinking", "phase": phase, "text": buf}
                        buf = ""
                elif event.delta.type == "text_delta":
                    chars += len(event.delta.text)
                    if chars % 2000 < len(event.delta.text):
                        yield {"event": "writing", "phase": phase, "chars": chars}
            if buf.strip():
                yield {"event": "thinking", "phase": phase, "text": buf}
            msg = await stream.get_final_message()

        if msg.stop_reason == "refusal":
            raise ProviderError("The request was declined by the model's safety system.")
        if msg.stop_reason == "max_tokens":
            raise ProviderError("The model hit its output limit mid-response.")
        text = "".join(b.text for b in msg.content if b.type == "text")
        yield {"event": "done", "text": text, "usage": Usage(
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            cached_tokens=getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        )}


# ── OpenAI ───────────────────────────────────────────────────────────

class OpenAIProvider(Provider):
    """OpenAI via the Responses API.

    Differences handled here: `instructions` instead of a system role, `input_text` /
    `input_file` content blocks, `max_output_tokens` (which covers reasoning AND visible
    output), `text.format` with a mandatory schema name, and reasoning summaries that are
    emitted only when the model actually reasons — so the caller must tolerate silence.
    """

    name = "openai"
    DEFAULT_MODEL = "gpt-5.6"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        from openai import AsyncOpenAI

        key = (api_key or "").strip() or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderError("OPENAI_API_KEY is not set.")
        self.model = model or self.DEFAULT_MODEL
        self.client = AsyncOpenAI(api_key=key)

    def _effort(self, effort: str) -> str:
        if any(self.model.startswith(m) for m in _OPENAI_FULL_EFFORT):
            return effort
        return _EFFORT_CEILING.get(effort, effort)

    @staticmethod
    def _instructions(system) -> str:
        if isinstance(system, str):
            return system
        return "\n\n".join(b.get("text", "") for b in (system or []) if isinstance(b, dict))

    @staticmethod
    def _content(blocks: List[Dict]) -> List[Dict]:
        """Anthropic content blocks -> OpenAI input parts."""
        out = []
        for b in blocks:
            kind = b.get("type")
            if kind == "text":
                out.append({"type": "input_text", "text": b["text"]})
            elif kind == "document":
                src = b.get("source", {})
                if src.get("type") == "base64":
                    out.append({
                        "type": "input_file",
                        "filename": b.get("title") or "document.pdf",
                        "file_data": f"data:{src.get('media_type','application/pdf')};base64,{src['data']}",
                    })
            elif kind == "image":
                src = b.get("source", {})
                if src.get("type") == "base64":
                    out.append({
                        "type": "input_image",
                        "image_url": f"data:{src.get('media_type','image/png')};base64,{src['data']}",
                    })
        return out

    async def stream(self, *, phase, system, blocks, schema, effort, max_tokens):
        kwargs = dict(
            model=self.model,
            instructions=self._instructions(system),
            input=[{"role": "user", "content": self._content(blocks)}],
            reasoning={"effort": self._effort(effort), "summary": "auto"},
            # Covers reasoning + visible output on this API, so give it real headroom.
            max_output_tokens=max(max_tokens, 24000),
            prompt_cache_key=f"resume-builder-{phase}",
        )
        if schema:
            kwargs["text"] = {"format": {
                "type": "json_schema", "name": f"{phase}_result", "strict": True, "schema": schema,
            }}

        buf, chars, text = "", 0, ""
        final = None
        async with self.client.responses.stream(**kwargs) as stream:
            async for event in stream:
                etype = getattr(event, "type", "")
                if etype == "response.reasoning_summary_text.delta":
                    buf += event.delta
                    if len(buf) >= 90 or "\n" in buf:
                        yield {"event": "thinking", "phase": phase, "text": buf}
                        buf = ""
                elif etype == "response.output_text.delta":
                    text += event.delta
                    chars += len(event.delta)
                    if chars % 2000 < len(event.delta):
                        yield {"event": "writing", "phase": phase, "chars": chars}
                elif etype == "response.completed":
                    final = event.response
            if buf.strip():
                yield {"event": "thinking", "phase": phase, "text": buf}
            if final is None:
                final = await stream.get_final_response()

        status = getattr(final, "status", "completed")
        if status == "incomplete":
            reason = getattr(getattr(final, "incomplete_details", None), "reason", "unknown")
            raise ProviderError(f"The model stopped early ({reason}) — try again.")
        text = text or (getattr(final, "output_text", "") or "")
        # A refusal arrives as a content part rather than schema-shaped JSON.
        for item in getattr(final, "output", []) or []:
            for part in getattr(item, "content", []) or []:
                if getattr(part, "type", "") == "refusal":
                    raise ProviderError("The request was declined by the model's safety system.")

        u = getattr(final, "usage", None)
        usage = Usage()
        if u:
            usage = Usage(
                input_tokens=getattr(u, "input_tokens", 0) or 0,
                output_tokens=getattr(u, "output_tokens", 0) or 0,
                cached_tokens=getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0,
                reasoning_tokens=getattr(getattr(u, "output_tokens_details", None), "reasoning_tokens", 0) or 0,
            )
        yield {"event": "done", "text": text, "usage": usage}


# ── Selection ────────────────────────────────────────────────────────

PROVIDERS = {"anthropic": AnthropicProvider, "openai": OpenAIProvider}


def make_provider(name: Optional[str] = None, model: Optional[str] = None,
                  api_key: Optional[str] = None) -> Provider:
    """Pick a provider explicitly, by env (RESUME_PROVIDER), or by whichever key exists."""
    name = (name or os.environ.get("RESUME_PROVIDER") or "").strip().lower()
    if not name:
        if os.environ.get("ANTHROPIC_API_KEY") or (api_key or "").startswith("sk-ant"):
            name = "anthropic"
        elif os.environ.get("OPENAI_API_KEY") or (api_key or "").startswith("sk-proj"):
            name = "openai"
        else:
            raise ProviderError(
                "No model provider configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
                "or add a key in the app's settings."
            )
    if name not in PROVIDERS:
        raise ProviderError(f"Unknown provider '{name}'. Choose one of: {', '.join(PROVIDERS)}.")
    model = model or os.environ.get("RESUME_MODEL") or None
    return PROVIDERS[name](model=model, api_key=api_key)
