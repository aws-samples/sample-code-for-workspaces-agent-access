# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fetch MCP server — Python equivalent of @modelcontextprotocol/server-fetch.

Fetches web content and converts HTML to markdown for LLM consumption.
"""
import re
import urllib.request
import urllib.error
from html.parser import HTMLParser
from fastmcp import FastMCP

mcp = FastMCP("fetch")


class _HTMLToText(HTMLParser):
    """Minimal HTML to plain text converter."""

    def __init__(self):
        super().__init__()
        self._text = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip = True
        elif tag in ("p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
            self._text.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href", "")
            if href:
                self._text.append(f" [{href}] ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip = False
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._text.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._text.append(data)

    def get_text(self):
        text = "".join(self._text)
        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)
        return text.strip()


@mcp.tool
def fetch(url: str, max_length: int = 5000, start_index: int = 0, raw: bool = False) -> str:
    """Fetch a URL and extract its contents as text.

    Args:
        url: URL to fetch
        max_length: Maximum number of characters to return (default: 5000)
        start_index: Start content from this character index (default: 0)
        raw: Get raw content without HTML-to-text conversion (default: false)
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MCP-Fetch-Server/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8", errors="replace")

            if raw or "text/plain" in content_type:
                text = body
            elif "text/html" in content_type or "<html" in body[:200].lower():
                parser = _HTMLToText()
                parser.feed(body)
                text = parser.get_text()
            else:
                text = body

            chunk = text[start_index:start_index + max_length]
            total = len(text)
            result = f"Content from {url} (showing {start_index}-{start_index + len(chunk)} of {total} chars):\n\n{chunk}"
            if start_index + max_length < total:
                result += f"\n\n[Content truncated. Use start_index={start_index + max_length} to continue.]"
            return result
    except urllib.error.HTTPError as e:
        return f"HTTP Error {e.code}: {e.reason} for {url}"
    except urllib.error.URLError as e:
        return f"URL Error: {e.reason} for {url}"
    except Exception as e:
        return f"Error fetching {url}: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
