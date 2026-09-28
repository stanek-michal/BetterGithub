"""LLM summaries of individual comments (Gemini via its OpenAI-compatible API), cached in SQLite."""
import hashlib
import os
import re
from html import escape, unescape

import httpx

from . import db
from .sync import now_iso

PROMPT_VERSION = "3"

SYSTEM = """You rewrite one GitHub comment (from a pull request or issue discussion) as a short plain-language summary for a busy engineer.

Start with 1-3 plain sentences that give the gist: what the author is saying or wants, and why. If the comment makes several separate points or requests, follow with a short "- " list of them, one per bullet; otherwise stop after the sentences. Say what the author is saying or asking: key points, decisions, requested changes, concerns, and anything the reader must do or answer. If the comment asks a question, include the question. For review comments, say what should change in the code and why.

Use simple, direct words. No fluff, no preamble, no "The author...", no "In summary". Do not invent anything. Keep identifiers, file names, commands and numbers only when they matter, wrapped in `backticks`. No headings."""


def html_to_text(html: str) -> str:
    """GitHub bodyHTML -> readable text, keeping code blocks, list items and paragraphs."""
    h = html or ""
    h = re.sub(r"<pre[^>]*>(.*?)</pre>", lambda m: "\n```\n" + re.sub(r"<[^>]+>", "", m.group(1)) + "\n```\n", h, flags=re.S)
    h = re.sub(r"<li[^>]*>", "\n- ", h)
    h = re.sub(r"<br\s*/?>|</p>|</div>|</h\d>|</tr>|</blockquote>", "\n", h)
    h = re.sub(r"<img[^>]*alt=\"([^\"]*)\"[^>]*>", r"[image: \1]", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = unescape(h)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


def render_summary(text: str) -> str:
    """Tiny renderer for the model's output: '- ' bullets, `code`, **bold**."""
    out, in_list = [], False
    for line in text.strip().splitlines():
        line = line.rstrip()
        if not line:
            continue
        html = escape(line.lstrip("-*• ").strip() if re.match(r"^\s*[-*•]\s", line) else line)
        html = re.sub(r"`([^`]+)`", r"<code>\1</code>", html)
        html = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", html)
        if re.match(r"^\s*[-*•]\s", line):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{html}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{html}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


class Summarizer:
    def __init__(self, conf: dict):
        self.model = conf.get("model", "gemini-3.8-flash")
        self.base_url = conf.get("base_url", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
        self.min_chars = int(conf.get("min_chars", 400))
        self.api_key = conf.get("api_key") or os.environ.get("SUMMARIZER_API_KEY") or os.environ.get("GEMINI_API_KEY") or ""
        self.client = httpx.AsyncClient(timeout=45)
        db.conn().execute("CREATE TABLE IF NOT EXISTS summaries (key TEXT PRIMARY KEY, summary TEXT, model TEXT, created_at TEXT)")

    async def summarize(self, text: str, context: str) -> str:
        if not self.api_key:
            raise RuntimeError("No API key: set [summarizer] api_key in config.toml or GEMINI_API_KEY")
        key = hashlib.sha256(f"{PROMPT_VERSION}\0{self.model}\0{context}\0{text}".encode()).hexdigest()
        c = db.conn()
        row = c.execute("SELECT summary FROM summaries WHERE key=?", (key,)).fetchone()
        if row:
            return row["summary"]
        r = await self.client.post(f"{self.base_url}/chat/completions",
                                   headers={"Authorization": f"Bearer {self.api_key}"},
                                   json={"model": self.model, "max_tokens": 800, "reasoning_effort": "low", "messages": [
                                       {"role": "system", "content": SYSTEM},
                                       {"role": "user", "content": f"{context}\n\nCOMMENT TO SUMMARIZE:\n{text[:30000]}"},
                                   ]})
        if r.status_code != 200:
            raise RuntimeError(f"{self.model}: HTTP {r.status_code} {r.text[:200]}")
        summary = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
        if not summary:
            raise RuntimeError("empty summary")
        c.execute("INSERT OR REPLACE INTO summaries VALUES(?,?,?,?)", (key, summary, self.model, now_iso()))
        c.commit()
        return summary
