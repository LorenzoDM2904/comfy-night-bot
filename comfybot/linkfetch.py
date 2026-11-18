"""Downloads images from Pinterest links (pin.it / pinterest.com/pin/...)
or from direct image URLs (full-resolution i.pinimg.com ones included).

It bootstraps a draft: the user pastes one or more links in a message,
the bot downloads the images and adds them as the draft's inputs, just
as if the photos had been sent.

Checked on live pages: for a public pin, the pin page (requested with a
browser User-Agent) already contains the pin's JSON with the
`images_orig` key, i.e. the full-resolution URL on i.pinimg.com (the
same value gallery-dl extracts). Pinterest's placeholders (256x256) only
appear inside CSS (`url(...)`) and are never caught by the JSON patterns.
"""

import re

import httpx

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
TIMEOUT = 60

# Extraction patterns in order of preference. images_orig is the most
# reliable: the key only exists in the pin's JSON, never in the CSS.
URL_PATTERNS = [
    re.compile(r'"images_orig":\{"__typename":"[^"]*","url":"([^"]+)"'),
    re.compile(r'"images_orig":\{"url":"([^"]+)"'),
    re.compile(r'property="og:image[^"]*" content="([^"]+)"'),
    re.compile(r'rel="image_src" href="([^"]+)"'),
    re.compile(r'id="pin-image-preload"[^>]+href="([^"]+)"'),
]

URL_RE = re.compile(r"https?://[^\s<>\"']+")


def is_pinterest_url(url: str) -> bool:
    host = (url.split("://", 1)[-1] if "://" in url else url).split("/", 1)[0]
    host = host.split(":")[0].lower()
    if host == "pin.it":
        return True
    if host == "i.pinimg.com" or host.endswith(".pinimg.com"):
        return False  # a direct image, not a pin page
    return (
        "pinterest." in host
        or host == "pinterest.com"
    )


def is_direct_image_url(url: str) -> bool:
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith(tuple(IMAGE_EXTS))


def extract_urls(text: str):
    """Clean URLs: strips trailing sentence punctuation."""
    out = []
    for m in URL_RE.finditer(text.replace("\\", "")):
        url = m.group(0).rstrip(".,;:!?)]}>\"'")
        if is_pinterest_url(url) or is_direct_image_url(url):
            out.append(url)
    return out


async def fetch_page(url: str) -> str:
    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers=BROWSER_HEADERS, follow_redirects=True,
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


def _originals(url: str) -> str:
    """From a 736x/564x/... thumbnail back to the full-resolution URL."""
    return re.sub(r"i\.pinimg\.com/\d+x/", "i.pinimg.com/originals/", url)


async def extract_image_url(pin_url: str) -> str:
    """From a pin page (or pin.it), the full-resolution i.pinimg.com URL."""
    html = await fetch_page(pin_url)
    for pattern in URL_PATTERNS:
        for m in pattern.finditer(html):
            url = m.group(1).replace("&amp;", "&")
            if "i.pinimg.com/" in url:
                return _originals(url) if "/736x/" in url else url
    raise ValueError(
        "Image not found on the Pinterest pin page "
        "(the pin is private, removed or blocked for bots)."
    )


async def download_image(url: str, max_bytes: int) -> bytes:
    """Downloads the link's content; whoever saves it checks the format
    (images.acquire)."""
    async with httpx.AsyncClient(
        timeout=TIMEOUT, headers=BROWSER_HEADERS, follow_redirects=True,
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        content = r.content
    if len(content) > max_bytes:
        raise ValueError("Image too large.")
    return content
