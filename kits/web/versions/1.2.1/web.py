# kits/web_kit.py

kit_name        = "Web Kit"
kit_description = "Web crawling, search, and HTTP request toolkit"
requirements    = ["crawl4ai", "requests"]
config          = {"SEARXNG_INSTANCE": "http://localhost:8081"}

import asyncio
import json
import re

import requests
from crawl4ai import AsyncWebCrawler
from crawl4ai.async_configs import CrawlerRunConfig
from html.parser import HTMLParser
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from utils import tool

# ---------------------------------------------------------------------------
# Shared configuration
# ---------------------------------------------------------------------------

# SearXNG instance — read directly from the kit's config dict, which Etna merges
# with user overrides set via `etna config set web_kit SEARXNG_INSTANCE <url>`.
# Public instances: https://searx.space/
SEARXNG_INSTANCE = config["SEARXNG_INSTANCE"]

TIMEOUT = 20
USER_AGENT = "Mozilla/5.0"

_VALID_OUTPUT_TYPES = {"html", "markdown", "text", "links", "media"}
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "ref", "fbclid", "gclid",
}


def _canonicalize_url(u):
    """Normalize a URL so equivalent forms dedupe to one node."""
    if not u:
        return None
    try:
        p = urlparse(u)
        p = p._replace(fragment="")
        p = p._replace(path=(p.path.rstrip("/") or "/"))
        keep = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING_PARAMS]
        p = p._replace(query=urlencode(keep))
        return urlunparse(p)
    except Exception:
        return u


def _build_crawl_config(extract_depth="basic"):
    """Build a CrawlerRunConfig, wiring 'advanced' to crawl4ai's magic mode."""
    if extract_depth == "advanced":
        return CrawlerRunConfig(magic=True, remove_overlay_elements=True)
    return CrawlerRunConfig()


def _normalize_links(links):
    """Convert crawl4ai's links object into a plain, JSON-safe dict."""
    if not links:
        return {}

    def _items(key):
        if hasattr(links, "get"):
            raw = links.get(key, [])
        else:
            raw = getattr(links, key, [])
        out = []
        for l in raw or []:
            out.append({
                "href": getattr(l, "href", None),
                "text": getattr(l, "text", None),
                "title": getattr(l, "title", None),
            })
        return out

    return {"internal": _items("internal"), "external": _items("external")}


def _normalize_media(media):
    """Convert crawl4ai's media items into a plain, JSON-safe list of dicts."""
    if not media:
        return []
    # Already a plain dict structure (e.g. {"images": [...], "videos": [...], "audios": [...]}).
    if isinstance(media, dict):
        return media
    out = []
    for m in media:
        if isinstance(m, dict):
            out.append(m)
        else:
            out.append({
                "type": getattr(m, "type", None),
                "src": getattr(m, "src", None),
                "alt": getattr(m, "alt", None),
            })
    return out


# ---------------------------------------------------------------------------
# Extract single or multiple page(s) content
# ---------------------------------------------------------------------------

@tool
def extract_page_content(urls: "str | list[str]", output_types: "str | list[str]" = "markdown,text", extract_depth: str = "basic") -> dict:
    """
    WHEN TO USE: Retrieve the content of one or more web pages when you have specific URLs.
    Prefer this over web_search when you already know where the information lives.

    urls: A single URL or a list of URLs to fetch. Pass a list to batch multiple pages
          into a single call — this is much more efficient than calling this tool repeatedly.
          Equivalent URLs (trailing slashes, fragments, tracking params) are deduplicated
          so each page is fetched only once.

    output_types: Whitelist of content types to return, as a comma-separated string or a
          list. Only the requested types are included in the result. Options: html, markdown,
          text, links, media. Default is "markdown,text".

    extract_depth: "basic" (default) or "advanced". Use "advanced" when the page is likely
          to contain tables or embedded content (e.g. wiki infoboxes, data grids) and basic
          extraction is returning incomplete or missing data. Advanced mode also auto-handles
          popups and consent banners. Avoid advanced unnecessarily as it costs more.

    Returns a dict keyed by URL. Each value contains the requested output types plus
    "success" and "status_code", or {"error": str} for a page that failed to crawl.
    links and media are returned as plain, JSON-safe data.
    """
    # Normalize the output_types whitelist
    if isinstance(output_types, str):
        types = [t.strip().lower() for t in output_types.split(",") if t.strip()]
    else:
        types = [t.strip().lower() for t in output_types]
    types = [t for t in types if t in _VALID_OUTPUT_TYPES]

    async def _extract():
        async with AsyncWebCrawler() as crawler:
            config = _build_crawl_config(extract_depth)
            url_list = [urls] if isinstance(urls, str) else urls

            # Deduplicate equivalent URLs so each page is fetched only once.
            seen = set()
            unique_urls = []
            for u in url_list:
                canon = _canonicalize_url(u) or u
                if canon not in seen:
                    seen.add(canon)
                    unique_urls.append(canon)

            crawl_results = await crawler.arun_many(urls=unique_urls, config=config)

            results = {}
            for result in crawl_results:
                url = result.url
                if not result.success:
                    results[url] = {"error": "Crawl failed", "status_code": result.status_code}
                    continue

                entry = {"success": True, "status_code": result.status_code}
                if "html" in types:
                    entry["html"] = result.html
                if "markdown" in types:
                    entry["markdown"] = result.markdown
                if "text" in types:
                    entry["text"] = result.extracted_content
                if "links" in types:
                    entry["links"] = _normalize_links(result.links)
                if "media" in types:
                    entry["media"] = _normalize_media(result.media)
                results[url] = entry
            return results

    try:
        return dict(asyncio.run(_extract()))
    except Exception as e:
        return {"error": f"Crawl4AI extract failed: {e}"}


# ---------------------------------------------------------------------------
# Search the web using SearXNG (privacy-respecting metasearch engine)
# ---------------------------------------------------------------------------

@tool
def web_search(query: str, num_results: int = 10, categories: str = "general", language: str = "en") -> dict:
    """
    WHEN TO USE: Find pages and information on the web when you don't have specific URLs.
    Returns a list of results with titles, URLs, and content snippets.

    Uses SearXNG, a privacy-respecting metasearch engine (no API key required).
    You can configure which SearXNG instance to use via the SEARXNG_INSTANCE kit config
    (set with `etna config set web_kit SEARXNG_INSTANCE <url>`).
    Public instances: https://searx.space/

    Args:
        query: Search query string
        num_results: Maximum number of results to return (default: 10)
        categories: Comma-separated list of search categories (default: "general")
                   Options: general, images, videos, news, map, music, it, science
        language: Two-letter language code for results (default: "en")

    Returns {"results": list, "query": str, "number_of_results": int} on success,
    optionally with a "suggestions" list, or {"error": str} on failure.
    """
    try:
        params = {
            "q": query,
            "format": "json",
            "pageno": 1,
            "language": language,
            "categories": categories,
        }

        search_url = f"{SEARXNG_INSTANCE}/search?{urlencode(params)}"

        response = requests.get(search_url, timeout=TIMEOUT)
        response.raise_for_status()

        data = response.json()

        # Extract results
        results = []
        for result in data.get("results", [])[:num_results]:
            results.append({
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "snippet": result.get("content", ""),
                "engine": result.get("engine", ""),
                "category": result.get("category", ""),
            })

        # Include suggestions if available
        response_data = {
            "results": results,
            "query": query,
            "number_of_results": data.get("number_of_results", len(results)),
        }

        if data.get("suggestions"):
            response_data["suggestions"] = data["suggestions"]

        return response_data

    except Exception as e:
        return {"error": f"SearXNG search failed: {e}"}


# ---------------------------------------------------------------------------
# Crawl: build a nested site map (summary of pages + their structure)
# ---------------------------------------------------------------------------

@tool
def web_crawl(url: str, max_breadth: int = 5, max_pages: int = 20, max_depth: int = 3) -> dict:
    """
    WHEN TO USE: Map the structure of a site — what pages exist,
    how they relate, and what each covers. Returns a nested tree where every node
    carries its own parent URL and depth, so the hierarchy is readable at a glance
    without reconstructing edges.

    Each node is a summary ({url, title, description, parent, depth, children}).
    If you need full page content instead of summaries, use extract_page_content on
    the specific URLs — it is cheaper than crawling the whole site.

    url: The root URL to start from.
    max_breadth: Maximum links to follow per page (default 5).
    max_pages: Global cap on the total number of pages visited (default 20).
    max_depth: Maximum link depth from the root to follow (default 3).

    Returns {"root", "tree", "pages", "total_pages"} where "tree" is the nested
    structure and "pages" is a flat list of all visited nodes for an alternative
    linear view, or {"error": str} on failure.

    """
    async def _crawl():
        # canonical url -> node dict (None placeholder during in-progress to cut cycles)
        visited = {}
        order = []

        async def crawl_node(current_url, parent_canon, depth):
            canon = _canonicalize_url(current_url)
            if canon is None:
                return None
            if canon in visited:
                return visited[canon] if visited[canon] else None
            if len(visited) >= max_pages or depth > max_depth:
                return None

            visited[canon] = None  # placeholder: mark in-progress to avoid infinite loops
            node = {
                "url": canon,
                "title": "",
                "description": "",
                "parent": parent_canon,
                "depth": depth,
                "children": [],
            }

            try:
                config = _build_crawl_config()
                container = await crawler.arun(url=canon, config=config)
                result_list = list(container)
                result = result_list[0] if result_list else None

                if result and result.success:
                    node["title"] = result.metadata.get("title", "") if result.metadata else ""
                    node["description"] = result.metadata.get("description", "") if result.metadata else ""

                    if result.links and hasattr(result.links, "internal"):
                        followed = 0
                        for link in result.links.internal:
                            if followed >= max_breadth or len(visited) >= max_pages:
                                break
                            if not link.href:
                                continue
                            child = await crawl_node(link.href, canon, depth + 1)
                            if child is not None:
                                node["children"].append(child)
                                followed += 1
            except Exception as e:
                node["error"] = str(e)

            visited[canon] = node
            order.append(node)
            return node

        # Single crawler for the whole walk.
        async with AsyncWebCrawler() as crawler:
            tree = await crawl_node(url, None, 0)

        # Flat alternative view of all visited nodes, in discovery order.
        # Lightweight entries (no nested children) — the tree carries the hierarchy.
        pages = [{"url": n["url"], "title": n["title"], "description": n["description"],
                  "parent": n["parent"], "depth": n["depth"]} for n in order]
        return {
            "root": tree["url"] if tree else url,
            "tree": tree,
            "pages": pages,
            "total_pages": len(visited),
        }

    try:
        return dict(asyncio.run(_crawl()))
    except Exception as e:
        return {"error": f"Crawl4AI crawl failed: {e}"}


# ---------------------------------------------------------------------------
# Generic HTTP/S request — for direct API calls
# ---------------------------------------------------------------------------

@tool
def http_request(method: str, url: str, headers: str | None = None, params: str | None = None, body: str | None = None, max_body_chars: int | None = None) -> dict:
    # Note: headers, params, and body must be passed as JSON strings.
    # Alternatively, embed query params directly in the URL to avoid parsing issues.
    """
    WHEN TO USE: Call REST APIs or any HTTP endpoint directly.

    Pass headers, params, and body as JSON strings, e.g. headers='{"Accept": "application/json"}'.
    An Accept: application/json header is included by default; override via headers if needed.

    Set max_body_chars when calling endpoints that might return large or unpredictable
    responses — HTML pages fetched by mistake, verbose API responses, etc. If the response
    is truncated, a truncation_notice field will tell you the original size so you can
    decide whether to re-call with a higher limit or adjust your request parameters.
Leave unset for normal API calls where you expect a bounded JSON response.

    Returns {"status_code": int, "headers": dict, "body": any} on success,
    with a "truncation_notice" when max_body_chars truncates the response,
    or {"error": str} on failure.
    """
    def _parse(val):
        if val is None:
            return {}
        if isinstance(val, dict):
            return val
        try:
            return json.loads(val)
        except Exception:
            pass
        try:
            import ast
            result = ast.literal_eval(val)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
        return {}

    parsed_headers = _parse(headers)
    parsed_params = _parse(params)
    parsed_body = _parse(body) if body else None

    parsed_headers.setdefault("Accept", "application/json")
    parsed_headers.setdefault("User-Agent", USER_AGENT)

    try:
        response = requests.request(
            method=method.upper(),
            url=url,
            headers=parsed_headers,
            params=parsed_params,
            json=parsed_body if parsed_body else None,
            timeout=TIMEOUT,
        )
        try:
            response_body = response.json()
        except Exception:
            response_body = response.text

        truncated = False
        original_len = None
        if max_body_chars is not None:
            if isinstance(response_body, str) and len(response_body) > max_body_chars:
                original_len = len(response_body)
                response_body = response_body[:max_body_chars]
                truncated = True
            elif isinstance(response_body, (dict, list)):
                serialized = str(response_body)
                if len(serialized) > max_body_chars:
                    original_len = len(serialized)
                    truncated = True

        result = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response_body,
        }
        if truncated:
            result["truncation_notice"] = (
                f"Body truncated to {max_body_chars} chars (original was {original_len} chars). "
                "Re-call with a higher max_body_chars or adjusted parameters if you need more."
            )

        return result
    except Exception as e:
        return {"error": f"HTTP request failed: {e}"}


# ---------------------------------------------------------------------------
# Get plain text from a single page (lightweight alternative to extract_page_content)
# ---------------------------------------------------------------------------

@tool
def get_page_text(url: str, max_chars: int = 20000) -> dict:
    """
    WHEN TO USE: Fetch a single URL and return its readable plain-text content.
    Use this when you only need the text of a page — not the full HTML, markdown,
    links, or media that extract_page_content returns. Lighter and faster than
    extract_page_content for simple text extraction, and it works even when
    crawl4ai is unavailable.

    url: The page to fetch.
    max_chars: Maximum number of characters of text to return (default 20000).
               Increase if the page is long and you're getting truncated content.

    Returns {"url": str, "title": str, "text": str, "truncated": bool} on success
    or {"error": str} on failure.
    """
    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.skip = 0
            self._in_title = False
            self.title = ""

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript", "head"):
                self.skip += 1
            if tag == "title":
                self._in_title = True
            if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript", "head"):
                self.skip = max(0, self.skip - 1)
            if tag == "title":
                self._in_title = False

        def handle_data(self, data):
            if self._in_title:
                self.title += data
            if self.skip:
                return
            text = data.strip()
            if text:
                self.parts.append(text)

    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()

        parser = _TextExtractor()
        parser.feed(resp.text)

        text = " ".join(p for p in parser.parts if p)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()

        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]

        return {
            "url": url,
            "title": parser.title.strip(),
            "text": text,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": f"Failed to fetch page text: {e}"}


# ---------------------------------------------------------------------------
# End of web_kit.py
# ---------------------------------------------------------------------------
