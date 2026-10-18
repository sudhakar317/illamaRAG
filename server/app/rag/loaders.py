"""Turning sources into plain text: PDF, TXT/Markdown, HTML files, and URLs.

Every loader returns `LoadedDocument`, so the chunker and the rest of the
pipeline never need to know where text came from. Section and page labels are
preserved where the format offers them, because a citation that says only
"report.pdf" is far less useful than one that says "report.pdf, page 12".
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md", ".markdown", ".html", ".htm"}

# Tried in order. An honest bot UA comes first because some sites (Wikipedia
# among them) refuse a browser UA that arrives without a real browser's other
# headers, and also insist that a bot UA carries a contact URL. The browser UA
# is the fallback for sites that serve a block page to anything announcing
# itself as a script.
_USER_AGENTS = (
    "illamaKnowledgeBot/1.0 (local knowledge-base indexer; "
    "+https://github.com/sudhakar317/illamaRAG) python-httpx",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
)

# Statuses that mean "not for this client" rather than "not here", so they are
# worth retrying with the next user agent.
_RETRY_STATUSES = {401, 403, 406, 429, 503}


@dataclass
class TextBlock:
    """A run of text plus where in the source it came from."""

    text: str
    page: int | None = None
    section: str | None = None


@dataclass
class LoadedDocument:
    title: str
    source_type: str  # pdf | text | html | url
    blocks: list[TextBlock] = field(default_factory=list)
    url: str | None = None

    def total_chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)


class LoaderError(Exception):
    """Raised for anything the user should see as a readable message."""


# --------------------------------------------------------------- text tidying


def _clean(text: str) -> str:
    """Normalise whitespace while keeping paragraph breaks.

    Paragraph boundaries survive because the chunker splits on them; collapsing
    everything to single spaces would destroy the only structural signal that
    plain text reliably has.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ")
    # Join words broken across a line by a hyphen, a common PDF artefact.
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # Collapse runs of spaces/tabs, but not newlines.
    text = re.sub(r"[ \t]+", " ", text)
    # Three or more newlines become a single paragraph break.
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip trailing spaces on each line.
    text = re.sub(r" +\n", "\n", text)
    return text.strip()


# --------------------------------------------------------------------- PDF


def load_pdf(path: Path, display_name: str | None = None) -> LoadedDocument:
    import fitz  # PyMuPDF

    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        raise LoaderError(f"Could not open the PDF: {exc}") from exc

    blocks: list[TextBlock] = []
    try:
        for index in range(len(doc)):
            text = _clean(doc.load_page(index).get_text("text"))
            if text:
                blocks.append(TextBlock(text=text, page=index + 1))
        fallback = Path(display_name).stem if display_name else path.stem
        title = (doc.metadata or {}).get("title") or fallback
    finally:
        doc.close()

    if not blocks:
        raise LoaderError(
            "No text found in this PDF. It is probably a scan of images, "
            "which needs OCR before it can be indexed."
        )
    fallback = Path(display_name).stem if display_name else path.stem
    return LoadedDocument(
        title=str(title).strip() or fallback, source_type="pdf", blocks=blocks
    )


# ------------------------------------------------------------- plain text


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def load_text(path: Path, display_name: str | None = None) -> LoadedDocument:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = _clean(raw)
    if not text:
        raise LoaderError("The file is empty.")

    # Markdown headings make good section labels for citations, so split on
    # them when they exist and fall back to one unlabelled block when they do not.
    blocks: list[TextBlock] = []
    matches = list(_MD_HEADING.finditer(text))
    if matches:
        if matches[0].start() > 0:
            lead = text[: matches[0].start()].strip()
            if lead:
                blocks.append(TextBlock(text=lead))
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[match.end() : end].strip()
            if body:
                blocks.append(TextBlock(text=body, section=match.group(2).strip()))
    if not blocks:
        blocks = [TextBlock(text=text)]

    title = Path(display_name).stem if display_name else path.stem
    return LoadedDocument(title=title, source_type="text", blocks=blocks)


# ------------------------------------------------------------------- HTML


def _extract_html(html: str, *, url: str | None = None) -> tuple[str, str | None]:
    """Return (main text, title), stripping navigation and boilerplate.

    trafilatura does the heavy lifting: naive tag-stripping keeps menus,
    cookie banners and footers, which then pollute retrieval with text nobody
    would ever want cited.
    """
    import trafilatura
    from trafilatura.settings import use_config

    config = use_config()
    config.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")

    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
        config=config,
    )

    title = None
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
        if meta:
            title = meta.title
    except Exception:  # noqa: BLE001 - metadata is a nicety, not required
        pass

    if not text:
        # Fall back to a crude strip so an unusual page still yields something.
        from trafilatura import html2txt

        text = html2txt(html)

    return _clean(text or ""), title


def load_html_file(path: Path, display_name: str | None = None) -> LoadedDocument:
    html = path.read_text(encoding="utf-8", errors="replace")
    text, title = _extract_html(html)
    if not text:
        raise LoaderError("No readable text could be extracted from this HTML file.")
    fallback = Path(display_name).stem if display_name else path.stem
    return LoadedDocument(
        title=(title or fallback).strip(),
        source_type="html",
        blocks=[TextBlock(text=text)],
    )


# -------------------------------------------------------------------- URL


# DNS64/NAT64 prefix (RFC 6052). Networks without native IPv4, common on
# mobile ISPs, hand back public sites as 64:ff9b::<ipv4>, and Python counts
# the whole prefix as private, which would block every such site.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _unwrap(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):  # type: ignore[no-untyped-def]
    """The IPv4 address inside an IPv4-mapped or NAT64 IPv6 address, else `ip`."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _is_private_host(hostname: str) -> bool:
    """True if the hostname resolves to a loopback/private/link-local address.

    This server is reachable from the LAN with no authentication, so an
    unrestricted URL fetcher would let anyone on the Wi-Fi use it to reach
    services that are not otherwise exposed — a router admin page, a database,
    a cloud metadata endpoint. Resolution happens here so that a public-looking
    name pointing at an internal address is still refused.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False  # unresolvable: let the request fail normally

    for info in infos:
        try:
            ip = _unwrap(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return True
    return False


def load_url(url: str, *, timeout: float = 20.0) -> LoadedDocument:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in ("http", "https"):
        raise LoaderError("Only http:// and https:// links are supported.")
    if not parsed.hostname:
        raise LoaderError("That does not look like a valid link.")

    # Read through settings, not os.getenv: pydantic loads server/.env into the
    # settings object only, so os.getenv never sees a value set there.
    if not settings.allow_private_url_fetch:
        if _is_private_host(parsed.hostname):
            raise LoaderError(
                "That link points to a private or local address, which is "
                "blocked. Set ALLOW_PRIVATE_URL_FETCH=true in server/.env if "
                "you intend to index something on your own network."
            )

    full_url = parsed.geturl()
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            for user_agent in _USER_AGENTS:
                response = client.get(
                    full_url,
                    headers={
                        "User-Agent": user_agent,
                        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                        "Accept-Language": "en;q=0.9",
                    },
                )
                if response.status_code not in _RETRY_STATUSES:
                    break
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in _RETRY_STATUSES:
            raise LoaderError(
                f"The site refused the request (HTTP {status}). It blocks "
                "automated readers, so save the page as HTML or PDF from your "
                "browser and upload that file instead."
            ) from exc
        if status == 404:
            raise LoaderError("That page was not found (HTTP 404).") from exc
        raise LoaderError(f"The site returned HTTP {status}.") from exc
    except httpx.TimeoutException as exc:
        raise LoaderError(
            "The site took too long to respond. Try again, or raise "
            "URL_FETCH_TIMEOUT in server/.env."
        ) from exc
    except httpx.HTTPError as exc:
        raise LoaderError(f"Could not fetch that link: {exc}") from exc

    content_type = response.headers.get("content-type", "")
    if "application/pdf" in content_type:
        raise LoaderError(
            "That link is a PDF. Download it and upload the file instead."
        )

    text, title = _extract_html(response.text, url=full_url)
    if not text:
        raise LoaderError(
            "No readable article text was found at that link. Pages that build "
            "their content with JavaScript often cannot be read this way."
        )

    return LoadedDocument(
        title=(title or parsed.netloc).strip(),
        source_type="url",
        blocks=[TextBlock(text=text)],
        url=full_url,
    )


# ------------------------------------------------------------------ router


def load_file(path: Path, display_name: str | None = None) -> LoadedDocument:
    """Load a saved file. `display_name` is the name the user uploaded it as."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return load_pdf(path, display_name)
    if suffix in (".txt", ".md", ".markdown"):
        return load_text(path, display_name)
    if suffix in (".html", ".htm"):
        return load_html_file(path, display_name)
    raise LoaderError(
        f"Unsupported file type '{suffix}'. "
        f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
    )
