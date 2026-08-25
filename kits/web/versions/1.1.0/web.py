# kits/web_kit.py

kit_name        = "Web Kit"
kit_description = "Web crawling, search, and HTTP request toolkit"
requirements    = ["crawl4ai", "requests"]
config          = {"SEARXNG_INSTANCE": "http://localhost:8081"}

import os
import requests
from utils import tool
from crawl4ai import AsyncWebCrawler
import asyncio



# ------------------------------------------------------------
# Extract single or multiple page(s) content
# ------------------------------------------------------------

@tool
def extract_page_content(urls: "str | list[str]", extract_depth: str = "basic"):
    """
    Use this to retrieve the full content of one or more web pages when you have specific URLs.
    Prefer this over web_search when you already know where the information lives.

    Pass a list of URLs to batch multiple pages into a single call — this is much more
    efficient than calling this tool repeatedly in a loop.

    Use extract_depth="advanced" when the page is likely to contain tables or embedded
    content (e.g. wiki infoboxes, data grids) and basic extraction is returning incomplete
    or missing data. Avoid advanced unnecessarily as it costs more credits.
    """
    from crawl4ai.async_configs import CrawlerRunConfig

    async def _extract():
        async with AsyncWebCrawler() as crawler:
            if isinstance(urls, str):
                url_list = [urls]
            else:
                url_list = urls

            results = {}
            for url in url_list:
                try:
                    config = CrawlerRunConfig()
                    container = await crawler.arun(url=url, config=config)
                    # CrawlResultContainer is iterable, get the first result
                    result_list = list(container)  # type: ignore
                    result = result_list[0] if len(result_list) > 0 else None

                    if result:
                        results[url] = {
                            "html": result.html,
                            "markdown": result.markdown,
                            "text": result.extracted_content,
                            "success": result.success,
                            "status_code": result.status_code,
                            "links": result.links,
                            "media": result.media,
                        }
                    else:
                        results[url] = {"error": "No result returned"}
                except Exception as e:
                    results[url] = {"error": str(e)}
            return results

    try:
        return dict(asyncio.run(_extract()))
    except Exception as e:
        return {"error": f"Crawl4AI extract failed: {e}"}


# ------------------------------------------------------------
# Search the web using SearXNG (privacy-respecting metasearch engine)
# ------------------------------------------------------------

# SearXNG instance — set via: etna config set web_kit SEARXNG_INSTANCE <url>
# Public instances: https://searx.space/
SEARXNG_INSTANCE = os.getenv("SEARXNG_INSTANCE", "http://localhost:8081")

@tool
def web_search(query: str, num_results: int = 10, categories: str = "general"):
    """
    Use this to find pages and information when you don't have specific URLs.
    Returns a list of results with titles, URLs, and content snippets.

    Uses SearXNG, a privacy-respecting metasearch engine (no API key required).
    You can configure which SearXNG instance to use via the SEARXNG_INSTANCE environment variable.
    Public instances: https://searx.space/

    Args:
        query: Search query string
        num_results: Maximum number of results to return (default: 10)
        categories: Comma-separated list of search categories (default: "general")
                   Options: general, images, videos, news, map, music, it, science
    """
    try:
        import urllib.parse

        # Build SearXNG API URL
        params = {
            "q": query,
            "format": "json",
            "pageno": 1,
            "language": "en",
            "categories": categories,
        }

        search_url = f"{SEARXNG_INSTANCE}/search?{urllib.parse.urlencode(params)}"

        response = requests.get(search_url, timeout=10)
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


# ------------------------------------------------------------
# Crawl: follow links + extract multiple pages
# ------------------------------------------------------------

@tool
def web_crawl(url: str, instructions: str = "", max_depth: int = 1, max_breadth: int = 20, limit: int = 20, chunks_per_source: int = 3):
    """
    Use this to systematically gather content across many pages of a site starting
    from a root URL. Good for wiki category pages, documentation sites, or any case
    where the information you need is spread across multiple linked pages.

    Keep max_depth=1 unless you need to follow links-of-links. Increase chunks_per_source
    if pages are long and you're getting truncated content. Reduce limit if you only
    need a subset of the linked pages.
    """
    from crawl4ai.async_configs import CrawlerRunConfig

    async def _crawl():
        visited = set()
        results = []

        async def crawl_recursive(current_url, depth):
            if depth > max_depth or len(visited) >= limit or current_url in visited:
                return

            visited.add(current_url)

            try:
                async with AsyncWebCrawler() as crawler:
                    config = CrawlerRunConfig()
                    container = await crawler.arun(url=current_url, config=config)
                    result_list = list(container)  # type: ignore
                    result = result_list[0] if len(result_list) > 0 else None

                    if result and result.success:
                        page_data = {
                            "url": current_url,
                            "markdown": result.markdown,
                            "html": result.html,
                            "links": [],
                            "depth": depth,
                        }

                        # Extract links for further crawling
                        if result.links and hasattr(result.links, 'internal'):
                            internal_links = result.links.internal[:max_breadth]
                            page_data["links"] = [link.href for link in internal_links if link.href]

                            # Recursively crawl internal links
                            for link in internal_links:
                                if link.href and len(visited) < limit:
                                    await crawl_recursive(link.href, depth + 1)

                        results.append(page_data)
            except Exception as e:
                results.append({"url": current_url, "error": str(e), "depth": depth})

        await crawl_recursive(url, 0)
        return {"results": results, "total_pages": len(results)}

    try:
        return dict(asyncio.run(_crawl()))
    except Exception as e:
        return {"error": f"Crawl4AI crawl failed: {e}"}


# ------------------------------------------------------------
# Map: summarize + connect content from multiple pages
# ------------------------------------------------------------

@tool
def web_map(url: str, max_breadth: int = 5, max_depth: int = 3):
    """
    Use this to get a high-level structural overview of a site — what pages exist,
    how they relate, and what each covers. Useful for planning before committing to
    a full crawl, or when you need to understand a site's layout rather than extract
    specific content.

    Increase max_breadth or max_depth if the default results feel shallow or incomplete.
    If you need actual page content rather than summaries, use web_crawl instead.
    """
    from crawl4ai.async_configs import CrawlerRunConfig

    async def _map():
        visited = set()
        site_map = []

        async def map_recursive(current_url, depth):
            if depth > max_depth or len(visited) >= max_breadth or current_url in visited:
                return

            visited.add(current_url)

            try:
                async with AsyncWebCrawler() as crawler:
                    config = CrawlerRunConfig()
                    container = await crawler.arun(url=current_url, config=config)
                    result_list = list(container)  # type: ignore
                    result = result_list[0] if len(result_list) > 0 else None

                    if result and result.success:
                        # Create a summary of the page
                        page_info = {
                            "url": current_url,
                            "title": result.metadata.get('title', '') if result.metadata else '',
                            "description": result.metadata.get('description', '') if result.metadata else '',
                            "depth": depth,
                            "links": [],
                        }

                        # Extract links for mapping
                        if result.links and hasattr(result.links, 'internal'):
                            internal_links = result.links.internal[:max_breadth]
                            page_info["links"] = [
                                {"url": link.href, "text": link.text}
                                for link in internal_links
                                if link.href and len(visited) < max_breadth
                            ]

                            # Recursively map internal links
                            for link in internal_links:
                                if link.href and len(visited) < max_breadth:
                                    await map_recursive(link.href, depth + 1)

                        site_map.append(page_info)
            except Exception as e:
                site_map.append({"url": current_url, "error": str(e), "depth": depth})

        await map_recursive(url, 0)
        return {"site_map": site_map, "total_pages": len(site_map)}

    try:
        return dict(asyncio.run(_map()))
    except Exception as e:
        return {"error": f"Crawl4AI map failed: {e}"}


# ------------------------------------------------------------
# Generic HTTP/S request — for direct API calls
# ------------------------------------------------------------

@tool
def http_request(method: str, url: str, headers: str | None = None, params: str | None = None, body: str | None = None, max_body_chars: int | None = None):
    # Note: headers, params, and body must be passed as JSON strings.
    # Alternatively, embed query params directly in the URL to avoid parsing issues.
    """
    Use this to call REST APIs or any HTTP endpoint directly.

    Pass headers, params, and body as JSON strings, e.g. headers='{\"Accept\": \"application/json\"}'.
    An Accept: application/json header is included by default; override via headers if needed.

    Set max_body_chars when calling endpoints that might return large or unpredictable
    responses — HTML pages fetched by mistake, verbose API responses, etc. If the response
    is truncated, a truncation_notice field will tell you the original size so you can
    decide whether to re-call with a higher limit or adjust your request parameters.
    Leave unset for normal API calls where you expect a bounded JSON response.
    """
    import json

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
    parsed_params  = _parse(params)
    parsed_body    = _parse(body) if body else None

    parsed_headers.setdefault("Accept", "application/json")

    try:
        response = requests.request(
            method=method.upper(),
            url=url,
            headers=parsed_headers,
            params=parsed_params,
            json=parsed_body if parsed_body else None,
            timeout=20,
        )
        try:
            response_body = response.json()
        except Exception:
            response_body = response.text

        truncated = False
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
            result["truncation_notice"] = f"Body truncated to {max_body_chars} chars (original was {original_len} chars). Re-call with a higher max_body_chars or adjusted parameters if you need more."

        return result
    except Exception as e:
        return {"error": f"HTTP request failed: {e}"}


# ------------------------------------------------------------
# ------------------------------------------------------------
# Get plain text from a single page (lightweight alternative to extract_page_content)
# ------------------------------------------------------------

@tool
def get_page_text(url: str, max_chars: int = 20000):
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
    import re
    from html.parser import HTMLParser

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
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
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


# ------------------------------------------------------------
# End of web_kit.py
# ------------------------------------------------------------
# ------------------------------------------------------------
