"""Voice-note transcription through an OpenAI-compatible /audio/transcriptions endpoint.

Works with OpenAI, Groq, speaches/faster-whisper-server, LocalAI and similar.
"""

from __future__ import annotations

import aiohttp

from .backends.base import error_text


class TranscriptionError(Exception):
    pass


class Transcriber:
    def __init__(self, base_url: str, model: str = "whisper-1", api_key: str = "",
                 session: aiohttp.ClientSession | None = None):
        self.url = f"{base_url.rstrip('/')}/audio/transcriptions"
        self.model = model
        self.api_key = api_key
        self._session = session

    async def __call__(self, audio: bytes, filename: str = "voice.ogg") -> str:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        form = aiohttp.FormData()
        form.add_field("file", audio, filename=filename, content_type="audio/ogg")
        form.add_field("model", self.model)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with self._session.post(self.url, data=form, headers=headers,
                                          timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    raise TranscriptionError(error_text(resp.status, await resp.text()))
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise TranscriptionError(f"cannot reach {self.url}: {e}") from e
        return (data.get("text") or "").strip()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
