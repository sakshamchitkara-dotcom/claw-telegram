"""Turn LLM-style Markdown into Telegram-safe HTML and split long replies.

Telegram's MarkdownV2 needs escaping of 18 characters and rejects the whole
message on one mistake, so we render a small Markdown subset to HTML instead,
escaping everything else. If Telegram still rejects it, callers fall back to
plain text.
"""

from __future__ import annotations

import html
import re

TG_LIMIT = 4096

_FENCE = re.compile(r"^```")
_INLINE = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"<b>\1</b>"),
    (re.compile(r"~~(.+?)~~", re.S), r"<s>\1</s>"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"<i>\1</i>"),
    (re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)"), r'<a href="\2">\1</a>'),
]
_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_CODE_SPAN = re.compile(r"`([^`\n]+)`")


def _inline(text: str) -> str:
    # Protect inline code first so its contents are not formatted.
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = _CODE_SPAN.sub(stash, text)
    text = html.escape(text, quote=False)
    text = _HEADING.sub(r"<b>\1</b>", text)
    for pattern, repl in _INLINE:
        text = pattern.sub(repl, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def to_html(md: str) -> str:
    out: list[str] = []
    buf: list[str] = []
    code: list[str] | None = None
    lang = ""
    for line in md.split("\n"):
        if _FENCE.match(line.strip()):
            if code is None:
                out.append(_inline("\n".join(buf)))
                buf, code, lang = [], [], line.strip()[3:].strip()
            else:
                body = html.escape("\n".join(code), quote=False)
                cls = f' class="language-{html.escape(lang)}"' if re.fullmatch(r"[\w+#.-]+", lang) else ""
                out.append(f"<pre><code{cls}>{body}</code></pre>")
                code = None
            continue
        (buf if code is None else code).append(line)
    if code is not None:  # unterminated fence: still render as code
        out.append(f"<pre>{html.escape(chr(10).join(code), quote=False)}</pre>")
    else:
        out.append(_inline("\n".join(buf)))
    return "\n".join(p for p in out if p).strip()


def split_markdown(md: str, limit: int) -> list[str]:
    """Split on line boundaries, never mid code-fence (fences are closed and reopened)."""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    fence: str | None = None

    def flush() -> None:
        nonlocal cur, size
        if cur:
            if fence is not None:
                cur.append("```")
            chunks.append("\n".join(cur))
        cur = [fence] if fence is not None else []
        size = sum(len(x) + 1 for x in cur)

    for line in md.split("\n"):
        while len(line) > limit - 10:  # hard-wrap absurdly long lines
            head, line = line[: limit - 10], line[limit - 10:]
            if size + len(head) > limit - 10:
                flush()
            cur.append(head)
            size += len(head) + 1
            flush()
        if size + len(line) + 1 > limit - 4:
            flush()
        cur.append(line)
        size += len(line) + 1
        if _FENCE.match(line.strip()):
            fence = None if fence is not None else line.strip()
    if cur and any(x.strip() for x in cur if x != fence):
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


def render(md: str, limit: int = TG_LIMIT, size: int | None = None) -> list[str]:
    """Markdown -> list of HTML messages, each within Telegram's limit."""
    size = size or limit - 600
    out: list[str] = []
    for chunk in split_markdown(md, size):
        rendered = to_html(chunk)
        if len(rendered) <= limit:
            out.append(rendered)
        elif size > 400:  # escaping blew it up (lots of < > &), retry with smaller pieces
            out.extend(render(chunk, limit, size // 2))
        else:
            out.append(rendered[:limit])
    return out or [""]


def split_plain(text: str, limit: int = TG_LIMIT) -> list[str]:
    return split_markdown(text, limit) or [""]
