"""
Reduce HTML boilerplate before RAG indexing so chunks resemble PDF/text extraction quality.

There is no perfect generic extractor for all sites; this pass removes executable/style
noise and common chrome, and prefers semantic main content when substantial.
"""

from html import escape
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - container should install beautifulsoup4
    BeautifulSoup = None  # type: ignore[misc, assignment]

# Tags that never contribute useful text for legal/doc RAG
_NOISE_TAGS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "template",
    "link",
    "meta",
    "object",
    "embed",
    "canvas",
    "picture",
    "source",
    "track",
    "base",
)


_bs4_missing_logged = False


def _host_is_wetten_overheid(host: str) -> bool:
    h = (host or "").lower()
    return h == "wetten.overheid.nl" or h.endswith(".wetten.overheid.nl")


def clean_html_for_rag_indexing(
    raw_html: str,
    page_url: str = "",
    *,
    plain_text_only: bool = False,
) -> str:
    """
    Return minimal valid HTML (DOCTYPE + head charset + title + body) with fewer
    scripts, styles, and navigation shells. Helps dense legal pages (e.g. wetten.nl)
    chunk more like a clean PDF/text export.

    If ``plain_text_only`` is True, markup inside the chosen region is stripped to a
    single ``<pre>`` under ``<body>`` (escaped plain text, newlines preserved). RAG then
    sees almost no structural tags—similar to PDF text extraction.

    Not a substitute for dedicated readability libraries on difficult sites.
    """
    if not raw_html or not isinstance(raw_html, str):
        return _minimal_html("(empty)")
    stripped = raw_html.strip()
    if not stripped:
        return _minimal_html("(empty)")

    global _bs4_missing_logged
    if BeautifulSoup is None:
        if not _bs4_missing_logged:
            logger.warning(
                "beautifulsoup4 is not installed; URL HTML cleaning is skipped. "
                "Rebuild the image after `pip install -r requirements.txt` (see beautifulsoup4)."
            )
            _bs4_missing_logged = True
        return stripped

    soup = BeautifulSoup(stripped, "html.parser")

    title = ""
    t_el = soup.find("title")
    if t_el and t_el.get_text(strip=True):
        title = t_el.get_text(strip=True)

    parsed_url = urlparse(page_url or "")
    host = parsed_url.netloc or ""

    # wetten.nl: full <body> embeds a huge sidebar TOC (#sidebar) before the statute (#content).
    # That outline dominates chunking vs substantive legal text and hurts retrieval vs PDF exports.
    if _host_is_wetten_overheid(host):
        sb = soup.find(id="sidebar")
        if sb:
            sb.decompose()

    for tag in list(soup.find_all(list(_NOISE_TAGS))):
        tag.decompose()

    if soup.head:
        soup.head.decompose()

    for tag_name in ("header", "footer", "nav", "aside"):
        for el in list(soup.find_all(tag_name)):
            el.decompose()

    for role in ("banner", "navigation", "complementary", "search"):
        for el in list(soup.find_all(attrs={"role": role})):
            el.decompose()

    body = soup.body
    inner: str

    content_el = soup.find(id="content")
    if content_el and _host_is_wetten_overheid(host):
        ct_len = len(content_el.get_text(strip=True))
        if ct_len >= 800:
            inner = content_el.decode_contents()
        else:
            main = soup.find("main") or soup.find(attrs={"role": "main"})
            main_text_len = len(main.get_text(strip=True)) if main else 0
            if main and main_text_len >= 400:
                inner = main.decode_contents()
            elif body:
                inner = body.decode_contents()
            else:
                inner = soup.decode_contents()
    else:
        main = soup.find("main") or soup.find(attrs={"role": "main"})
        main_text_len = len(main.get_text(strip=True)) if main else 0
        if main and main_text_len >= 400:
            inner = main.decode_contents()
        elif body:
            inner = body.decode_contents()
        else:
            inner = soup.decode_contents()

    if not inner.strip():
        hint = escape((page_url or "source document")[:500])
        inner = f"<p>{hint}</p>"

    if plain_text_only:
        frag = BeautifulSoup(inner, "html.parser")
        plain = frag.get_text("\n", strip=False)
        plain = re.sub(r"[ \t\f\v]+\n", "\n", plain)
        plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
        if not plain:
            plain = (page_url or "source document")[:10000] or "(empty)"
        inner = f"<pre>{escape(plain)}</pre>"

    ttl = escape(title) if title else escape((page_url or "document")[:200])
    return (
        "<!DOCTYPE html>\n"
        '<html lang="nl">\n'
        '<head><meta charset="utf-8"/>'
        f"<title>{ttl}</title></head>\n"
        f"<body>\n{inner}\n</body>\n"
        "</html>"
    )


def _minimal_html(msg: str) -> str:
    m = escape(msg)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="nl"><head><meta charset="utf-8"/>'
        f"<title>{m}</title></head>"
        f"<body><p>{m}</p></body></html>"
    )
