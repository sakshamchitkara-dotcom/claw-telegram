"""Claude fallback backend via the official `anthropic` SDK (plain chat, no tools)."""

from __future__ import annotations

import base64

from .base import Backend, BackendError, TextDelta, Turn


class ClaudeBackend(Backend):
    name = "claude"
    supports_images = True

    def __init__(self, model: str = "claude-opus-5-5", system_prompt: str = "", client=None,
                 effort: str = "medium"):
        try:
            import anthropic
        except ImportError as e:  # optional dependency
            raise ValueError("claude backend needs: pip install 'claw-telegram[claude]'") from e
        self._anthropic = anthropic
        self.client = client or anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY etc.
        self.model = model
        self.system_prompt = system_prompt
        self.effort = effort

    def _messages(self, turn: Turn) -> list[dict]:
        msgs = [dict(m) for m in turn.history]
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": img.mime,
                                         "data": base64.standard_b64encode(img.data).decode()}}
            for img in turn.images
        ]
        content.append({"type": "text", "text": turn.text or "(see image)"})
        msgs.append({"role": "user", "content": content})
        return msgs

    async def stream(self, turn: Turn):
        a = self._anthropic
        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=16000,
                system=self.system_prompt or a.NOT_GIVEN,
                # Opus 5.5 always thinks; effort is the only knob and defaults to medium.
                output_config={"effort": self.effort},
                messages=self._messages(turn),
            ) as stream:
                async for text in stream.text_stream:
                    yield TextDelta(text)
                final = await stream.get_final_message()
        except a.RateLimitError as e:
            raise BackendError("claude: rate limited, try again shortly") from e
        except a.APIStatusError as e:
            raise BackendError(f"claude: HTTP {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise BackendError("claude: cannot reach the Anthropic API") from e
        if final.stop_reason == "refusal":
            yield TextDelta("\n\n[Claude declined to answer this request.]")
        elif final.stop_reason == "max_tokens":
            yield TextDelta("\n\n[reply truncated at max_tokens]")

    async def health(self) -> str:
        try:
            m = await self.client.models.retrieve(self.model)
            return f"ok ({m.id})"
        except self._anthropic.APIStatusError as e:
            return f"error HTTP {e.status_code}"
        except self._anthropic.APIConnectionError:
            return "unreachable"

    async def close(self) -> None:
        await self.client.close()
