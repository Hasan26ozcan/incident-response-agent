"""Wiki publishing (Stage 15): a file-backed wiki for offline use and a Confluence REST client.

``publish`` is idempotent: publishing the same content again returns ``unchanged`` and creates no new version. Both
implementations share the ``Wiki`` protocol, so the notifier does not know which one it talks to.

``FileWiki`` is a real, versioned wiki on disk (current page, history, index). ``ConfluenceWiki`` talks to the
Confluence Cloud REST API (``/rest/api/content``); it is tested against a local fake server that implements the same
endpoints, not against a live Confluence site.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from incident_agent.reporting.http import HttpClient, HttpError
from incident_agent.reporting.postmortem import PostMortem
from incident_agent.reporting.sanitize import md_text, one_line

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class WikiError(RuntimeError):
    """Publishing failed. The message never contains credentials or the wiki URL."""


@dataclass(frozen=True)
class PublishResult:
    action: str  # created | updated | unchanged
    url: str
    version: int


class Wiki(Protocol):
    name: str

    def publish(self, page: PostMortem) -> PublishResult: ...


# --- file wiki -----------------------------------------------------------------------------------------------------


class FileWiki:
    name = "file"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _check(self, slug: str) -> None:
        if not _SLUG.fullmatch(slug):
            raise WikiError("invalid page slug")

    def _meta_path(self, slug: str) -> Path:
        return self.root / f"{slug}.json"

    def _write(self, path: Path, text: str) -> None:
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def read_meta(self, slug: str) -> dict[str, Any] | None:
        self._check(slug)
        path = self._meta_path(slug)
        if not path.exists():
            return None
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            return meta if isinstance(meta, dict) else None
        except (OSError, ValueError) as exc:
            raise WikiError(f"cannot read page metadata: {type(exc).__name__}") from exc

    def publish(self, page: PostMortem) -> PublishResult:
        self._check(page.slug)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / ".history").mkdir(exist_ok=True)
            meta = self.read_meta(page.slug)
            url = (self.root / f"{page.slug}.md").resolve().as_uri()
            if meta is not None and meta.get("hash") == page.content_hash:
                return PublishResult("unchanged", url, int(meta["version"]))
            version = int(meta["version"]) + 1 if meta else 1
            self._write(self.root / ".history" / f"{page.slug}.v{version}.md", page.markdown)
            self._write(self.root / f"{page.slug}.md", page.markdown)
            self._write(
                self._meta_path(page.slug),
                json.dumps({"slug": page.slug, "title": page.title, "hash": page.content_hash, "version": version}),
            )
            self._write_index()
        except OSError as exc:
            raise WikiError(f"cannot write wiki page: {type(exc).__name__}") from exc
        return PublishResult("created" if version == 1 else "updated", url, version)

    def _write_index(self) -> None:
        rows = []
        for meta_file in sorted(self.root.glob("*.json")):
            try:
                m = json.loads(meta_file.read_text(encoding="utf-8"))
                rows.append(f"- [{md_text(m['title'], 120)}]({m['slug']}.md) (version {int(m['version'])})")
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self._write(self.root / "index.md", "# Post-mortems\n\n" + "\n".join(rows) + "\n")


# --- Markdown to Confluence storage format ---------------------------------------------------------------------------

_INLINE = re.compile(r"`([^`]*)`|\*\*(.+?)\*\*|\\([\\`*_\[\]<>|#!])")


def _inline(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(html.escape(text[pos : m.start()], quote=False))
        if m.group(1) is not None:
            out.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        elif m.group(2) is not None:
            out.append(f"<strong>{html.escape(m.group(2), quote=False)}</strong>")
        else:
            out.append(html.escape(m.group(3), quote=False))
        pos = m.end()
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


def to_storage(markdown: str) -> str:
    """Convert the (small, known) Markdown subset the post-mortem uses. All text is HTML-escaped."""
    out: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif m := re.match(r"(#{1,3}) (.*)", line):
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in re.split(r"(?<!\\)\|", lines[i].strip().strip("|"))]
                if not all(re.fullmatch(r"-+", c) for c in cells):
                    rows.append(cells)
                i += 1
            head, *rest = rows
            out.append(
                "<table><tbody><tr>"
                + "".join(f"<th>{_inline(c)}</th>" for c in head)
                + "</tr>"
                + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rest)
                + "</tbody></table>"
            )
        elif line.startswith("- ") or re.match(r"\d+\. ", line):
            ordered = not line.startswith("- ")
            items = []
            while i < len(lines) and (lines[i].startswith("- ") or re.match(r"\d+\. ", lines[i])):
                items.append(re.sub(r"^(- |\d+\. )", "", lines[i]).replace("[ ] ", "☐ ", 1))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + f"</{tag}>")
        elif line.startswith("> "):
            out.append(f"<blockquote><p>{_inline(line[2:])}</p></blockquote>")
            i += 1
        else:
            out.append(f"<p>{_inline(line)}</p>")
            i += 1
    return "".join(out)


# --- Confluence ------------------------------------------------------------------------------------------------------


class ConfluenceWiki:
    name = "confluence"

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        space: str,
        *,
        parent_id: str | None = None,
        http: HttpClient | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9~_-]{1,255}", space):
            raise ValueError("invalid Confluence space key")
        self.base_url = base_url.rstrip("/")
        self.space = space
        self.parent_id = parent_id
        self._http = http or HttpClient()
        token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        from incident_agent.reporting.http import check_url

        check_url(self.base_url)

    def _call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        try:
            resp = self._http.request(method, self.base_url + path, headers=self._headers, body=body)
        except HttpError as exc:
            raise WikiError(str(exc)) from exc
        if resp.status not in (200, 201):
            raise WikiError(
                f"Confluence answered HTTP {resp.status}: {one_line(resp.body.decode('utf-8', 'replace'), 160)}"
            )
        try:
            data = json.loads(resp.body)
        except ValueError as exc:
            raise WikiError("Confluence answered with something that is not JSON") from exc
        if not isinstance(data, dict):
            raise WikiError("unexpected Confluence response")
        return data

    def _page_url(self, data: Mapping[str, Any]) -> str:
        webui = (data.get("_links") or {}).get("webui")
        if isinstance(webui, str) and webui.startswith("/"):
            return self.base_url + webui
        return f"{self.base_url}/pages/viewpage.action?pageId={data.get('id', '')}"

    def publish(self, page: PostMortem) -> PublishResult:
        from urllib.parse import quote

        marker = f"<!-- postmortem-sha256:{page.content_hash} -->"
        storage = marker + to_storage(page.markdown)
        found = self._call(
            "GET",
            f"/rest/api/content?spaceKey={quote(self.space)}&title={quote(page.title)}&type=page&expand=version,body.storage",
        )
        results = found.get("results") or []
        payload: dict[str, Any] = {
            "type": "page",
            "title": page.title,
            "body": {"storage": {"value": storage, "representation": "storage"}},
        }
        if not results:
            payload["space"] = {"key": self.space}
            if self.parent_id:
                payload["ancestors"] = [{"id": self.parent_id}]
            data = self._call("POST", "/rest/api/content", payload)
            return PublishResult("created", self._page_url(data), int((data.get("version") or {}).get("number", 1)))
        existing = results[0]
        version = int((existing.get("version") or {}).get("number", 1))
        current = ((existing.get("body") or {}).get("storage") or {}).get("value", "")
        if marker in str(current):
            return PublishResult("unchanged", self._page_url(existing), version)
        payload["id"] = existing["id"]
        payload["version"] = {"number": version + 1}
        data = self._call("PUT", f"/rest/api/content/{quote(str(existing['id']))}", payload)
        return PublishResult("updated", self._page_url(data), version + 1)
