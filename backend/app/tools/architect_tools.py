"""Tools available to the Architect agent during the interview and planning phases."""

from __future__ import annotations

import html
import logging
import re
from typing import Any

import httpx

from ..agents.base import ToolResult, ToolSpec
from ..state import ProjectStore

logger = logging.getLogger(__name__)


# ---------- Web search and fetch -----------------------------------------------------------------
#
# We deliberately use a simple HTTP-based search here so the architect can research without any
# paid API dependency. In production you'd swap in a real search provider.


_DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
_USER_AGENT = (
    "Mozilla/5.0 (compatible; DevTeamArchitect/0.1; +https://github.com/local/dev-team)"
)


async def _web_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.post(
            _DDG_HTML_ENDPOINT,
            data={"q": query},
            headers={"User-Agent": _USER_AGENT},
        )
        resp.raise_for_status()
        html_text = resp.text

    # Very simple HTML parsing — the ddg HTML layout is stable. We look for result blocks.
    # We intentionally don't use BeautifulSoup here to keep dependencies minimal; if this
    # becomes flaky we should switch to a proper parser.
    results: list[dict[str, str]] = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    for match in pattern.finditer(html_text):
        url = html.unescape(match.group(1))
        # DDG wraps target URLs — extract the uddg param if present
        uddg_match = re.search(r"uddg=([^&]+)", url)
        if uddg_match:
            from urllib.parse import unquote

            url = unquote(uddg_match.group(1))
        title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        snippet = re.sub(r"<[^>]+>", "", match.group(3)).strip()
        results.append({"url": url, "title": html.unescape(title), "snippet": html.unescape(snippet)})
        if len(results) >= max_results:
            break
    return results


async def _web_fetch(url: str, max_chars: int = 20_000) -> str:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        text = resp.text

    # Strip scripts/styles and tags to give the model readable text
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...truncated]"
    return text


# ---------- Tool factory --------------------------------------------------------------------------


def build_architect_tools(store: ProjectStore) -> list[ToolSpec]:
    """Build the tool set the Architect has access to.

    Tools close over the ProjectStore so they can read and write project state.
    """

    async def web_search_exec(args: dict[str, Any]) -> ToolResult:
        query = args.get("query", "").strip()
        max_results = int(args.get("max_results", 5))
        if not query:
            return ToolResult(content="Missing query", is_error=True)
        try:
            results = await _web_search(query, max_results=max_results)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Search failed: {exc}", is_error=True)

        # Log the search so the user has an audit trail of what the architect researched
        await store.append_decision(
            {"actor": "architect", "kind": "web_search", "query": query, "result_count": len(results)}
        )

        if not results:
            return ToolResult(content=f"No results for: {query}")
        formatted = "\n\n".join(
            f"[{i + 1}] {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(results)
        )
        return ToolResult(content=formatted)

    async def web_fetch_exec(args: dict[str, Any]) -> ToolResult:
        url = args.get("url", "").strip()
        if not url:
            return ToolResult(content="Missing url", is_error=True)
        if not (url.startswith("http://") or url.startswith("https://")):
            return ToolResult(content="URL must start with http:// or https://", is_error=True)
        try:
            text = await _web_fetch(url)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(content=f"Fetch failed: {exc}", is_error=True)

        await store.append_decision(
            {
                "actor": "architect",
                "kind": "web_fetch",
                "url": url,
                "content_length": len(text),
            }
        )
        return ToolResult(content=text)

    async def append_decision_log_exec(args: dict[str, Any]) -> ToolResult:
        note = args.get("note", "").strip()
        kind = args.get("kind", "note").strip() or "note"
        if not note:
            return ToolResult(content="Missing note", is_error=True)
        await store.append_decision({"actor": "architect", "kind": kind, "note": note})
        return ToolResult(content="Logged.")

    async def write_plan_exec(args: dict[str, Any]) -> ToolResult:
        content = args.get("content", "")
        if not content or not content.strip():
            return ToolResult(content="Plan content cannot be empty", is_error=True)
        store.write_plan(content)
        await store.append_decision(
            {"actor": "architect", "kind": "plan_written", "chars": len(content)}
        )
        return ToolResult(content=f"plan.md written ({len(content)} chars).")

    async def read_plan_exec(_args: dict[str, Any]) -> ToolResult:
        content = store.read_plan()
        if not content:
            return ToolResult(content="plan.md is empty or does not exist yet.")
        return ToolResult(content=content)

    async def mark_interview_complete_exec(args: dict[str, Any]) -> ToolResult:
        summary = args.get("summary", "").strip()
        await store.append_decision(
            {"actor": "architect", "kind": "interview_complete", "summary": summary}
        )
        meta = store.read_meta()
        from ..state import ProjectStatus

        meta.status = ProjectStatus.PLANNING
        store.write_meta(meta)
        return ToolResult(
            content="Interview marked complete. You may now draft the plan with write_plan."
        )

    async def request_approval_exec(args: dict[str, Any]) -> ToolResult:
        note = args.get("note", "").strip()
        await store.append_decision(
            {"actor": "architect", "kind": "approval_requested", "note": note}
        )
        meta = store.read_meta()
        from ..state import ProjectStatus

        meta.status = ProjectStatus.AWAIT_APPROVAL
        store.write_meta(meta)
        return ToolResult(
            content="Plan submitted to user for approval. Wait for user decision."
        )

    return [
        ToolSpec(
            name="web_search",
            description=(
                "Search the web for information about technologies, architectures, standard "
                "practices, and similar products. Use this to ask better interview questions "
                "or to research technical decisions. Every search is logged to decisions.log."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default 5)",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["query"],
            },
            executor=web_search_exec,
        ),
        ToolSpec(
            name="web_fetch",
            description=(
                "Fetch the textual content of a specific URL (e.g., a docs page, blog post, "
                "or README). Use after web_search to dig into promising results. Every fetch "
                "is logged."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                },
                "required": ["url"],
            },
            executor=web_fetch_exec,
        ),
        ToolSpec(
            name="append_decision_log",
            description=(
                "Append an entry to decisions.log explaining a significant decision, "
                "research finding, or reasoning step. Use this to make your thinking visible "
                "to the user and to downstream agents. `kind` is a short category tag; "
                "`note` is the content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "Short category (e.g., 'research_finding', 'rationale')",
                    },
                    "note": {"type": "string", "description": "The decision or note content"},
                },
                "required": ["note"],
            },
            executor=append_decision_log_exec,
        ),
        ToolSpec(
            name="read_plan",
            description="Read the current contents of plan.md. Empty if not yet written.",
            input_schema={"type": "object", "properties": {}},
            executor=read_plan_exec,
        ),
        ToolSpec(
            name="write_plan",
            description=(
                "Write (or replace) plan.md with the provided content. Use after the "
                "interview is complete and you've entered reflective practice on the plan."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Full plan.md content in Markdown",
                    }
                },
                "required": ["content"],
            },
            executor=write_plan_exec,
        ),
        ToolSpec(
            name="mark_interview_complete",
            description=(
                "Call this when you have conducted reflective practice on the interview and "
                "believe you have enough information to draft a concrete plan. The system "
                "transitions to PLANNING state. `summary` is an optional one-paragraph "
                "recap of what you learned."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "One-paragraph recap"},
                },
            },
            executor=mark_interview_complete_exec,
        ),
        ToolSpec(
            name="request_approval",
            description=(
                "Call after writing plan.md and entering reflective practice on it. Marks "
                "the project AWAIT_APPROVAL and hands the plan to the user to review."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "Optional note to the user (e.g., what to pay attention to)",
                    },
                },
            },
            executor=request_approval_exec,
        ),
    ]
