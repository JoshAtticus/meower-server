from quart import Blueprint, request, abort
from quart_schema import validate_querystring
from pydantic import BaseModel, Field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import asyncio, hashlib, ipaddress, json, socket

import requests
from requests.adapters import HTTPAdapter

import security
from database import rdb


embeds_bp = Blueprint("embeds_bp", __name__, url_prefix="/embed")

MAX_BYTES = 512 * 1024
MAX_REDIRECTS = 3
USER_AGENT = "Mozilla/5.0 (compatible; MeowerLinkPreview/1.0; +https://meower.org)"
CACHE_OK = 6 * 3600
CACHE_FAIL = 15 * 60


class EmbedQueryArgs(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class PinnedAdapter(HTTPAdapter):
    """Connects to an already-validated IP while still verifying TLS for the real hostname."""
    def __init__(self, hostname, *args, **kwargs):
        self.hostname = hostname
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["server_hostname"] = self.hostname
        pool_kwargs["assert_hostname"] = self.hostname
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def resolve_public_ip(host: str):
    """First public IP address for host, or None when it resolves to anything internal."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return None
        ips.append(ip)
    return str(ips[0]) if ips else None


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.title = ""
        self.icon = None
        self.canonical = None
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "title":
            self._in_title = True
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            if "icon" in rel and not self.icon and a.get("href"):
                self.icon = a["href"]
            elif "canonical" in rel and a.get("href"):
                self.canonical = a["href"]
        elif tag == "body":
            self.done = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


def fetch_page(url: str):
    """Fetches url (following a few redirects, each re-validated) and returns (final_url, html) or None."""
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.port not in (None, 80, 443):
            return None
        ip = resolve_public_ip(parsed.hostname)
        if not ip:
            return None

        session = requests.Session()
        session.mount("https://", PinnedAdapter(parsed.hostname))
        netloc_ip = f"[{ip}]" if ":" in ip else ip
        if parsed.port:
            netloc_ip += f":{parsed.port}"
        pinned = parsed._replace(netloc=netloc_ip).geturl()
        try:
            resp = session.get(
                pinned,
                headers={"Host": parsed.netloc, "User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                timeout=(3, 5), stream=True, allow_redirects=False
            )
        except requests.RequestException:
            return None

        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
            url = urljoin(url, resp.headers["Location"])
            resp.close()
            continue
        if resp.status_code != 200:
            resp.close()
            return None
        ctype = resp.headers.get("Content-Type", "").lower()
        if "html" not in ctype:
            resp.close()
            return None
        body = b""
        for chunk in resp.iter_content(16384):
            body += chunk
            if len(body) >= MAX_BYTES:
                break
        resp.close()
        encoding = resp.encoding or "utf-8"
        try:
            return url, body.decode(encoding, errors="replace")
        except LookupError:
            return url, body.decode("utf-8", errors="replace")
    return None


def abs_http(base: str, value):
    if not value:
        return None
    full = urljoin(base, value)
    return full if urlparse(full).scheme in ("http", "https") and len(full) <= 2000 else None


def build_preview(url: str):
    fetched = fetch_page(url)
    if not fetched:
        return {}
    final_url, html = fetched
    parser = MetaParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    m = parser.meta
    title = m.get("og:title") or m.get("twitter:title") or parser.title.strip()
    description = m.get("og:description") or m.get("twitter:description") or m.get("description") or ""
    image = abs_http(final_url, m.get("og:image") or m.get("og:image:url") or m.get("twitter:image") or m.get("twitter:image:src"))
    if not title and not description and not image:
        return {}
    return {
        "url": abs_http(final_url, parser.canonical) or final_url,
        "title": " ".join(title.split())[:200],
        "description": " ".join(description.split())[:400],
        "image": image,
        "site_name": (m.get("og:site_name") or urlparse(final_url).hostname or "")[:100],
        "favicon": abs_http(final_url, parser.icon) or abs_http(final_url, "/favicon.ico"),
        "large": (m.get("twitter:card") == "summary_large_image") or bool(m.get("og:image") and m.get("og:type") in ("video.other", "article"))
    }


@embeds_bp.get("/")
@validate_querystring(EmbedQueryArgs)
async def get_embed(query_args: EmbedQueryArgs):
    if not request.user:
        abort(401)
    if security.ratelimited(f"embed:{request.user}"):
        abort(429)
    security.ratelimit(f"embed:{request.user}", 30, 60)

    url = query_args.url.strip()
    key = "embed:" + hashlib.sha1(url.encode()).hexdigest()
    cached = rdb.get(key)
    if cached is not None:
        preview = json.loads(cached)
    else:
        preview = await asyncio.to_thread(build_preview, url)
        rdb.setex(key, CACHE_OK if preview else CACHE_FAIL, json.dumps(preview))

    if not preview:
        return {"error": True, "type": "noPreview"}, 404
    return {"error": False, **preview}, 200
