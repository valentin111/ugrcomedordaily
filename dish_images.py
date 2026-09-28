"""Find illustrative dish photos on Commons without a catalogue or extra packages."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import json
import logging
import os
import re
import time
import unicodedata
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


API = "https://commons.wikimedia.org/w/api.php"
LOG = logging.getLogger("ugr-menu")
STOP_WORDS = set("a al la las el los de del en con y un una para exclusivo personas celiaquia".split())
FOOD_CONTEXT = re.compile(
    r"\b(?:dish(?:es)?|cuisine|cooked|cooking|fried|grilled|roasted|stew|soup|salad|meal|"
    r"paella|fideua|tortilla|hamburguesa|burger|croqueta|adobo|escabeche|plato|platos|"
    r"receta|cocina|gastronomia|estofado|guiso|asado|frito|parrilla|gratinado|ensalada)\b"
)


@dataclass(frozen=True)
class DishImage:
    """Keep image and attribution together so credits cannot get lost in rendering."""

    url: str
    source_url: str
    title: str
    artist: str
    credit: str
    license_name: str
    license_url: str


class PlainText(HTMLParser):
    """Extract text from Commons metadata without trusting its embedded HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        """Collect text only; newsletter rendering escapes it again."""
        self.parts.append(data)


def plain_text(value: str) -> str:
    """Strip markup and normalize spacing in author and description fields."""
    parser = PlainText()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


def words(value: str) -> list:
    """Match Spanish words without depending on accents or capitalization."""
    value = unicodedata.normalize("NFKD", value.lower())
    return re.findall(r"[a-z]+", "".join(char for char in value if not unicodedata.combining(char)))


def search_terms(dish: str) -> list:
    """Retain ingredients and dietary labels while dropping connectors and celiac notes."""
    dish = re.sub(r"\([^)]*celiaqu[ií]a[^)]*\)", "", dish, flags=re.I)
    return list(dict.fromkeys(word for word in words(dish) if word not in STOP_WORDS))


def enabled_from_env() -> bool:
    """Let operators disable image lookups without changing the sending schedule."""
    value = os.getenv("IMAGE_SEARCH_ENABLED", "true").strip().lower()
    if value not in ("true", "false"):
        raise ValueError("IMAGE_SEARCH_ENABLED must be true or false")
    return value == "true"


def safe_url(value: str, hosts: set) -> str:
    """Accept only HTTPS links on the provider's expected hosts."""
    if not isinstance(value, str):
        return ""
    url = urlsplit(value)
    if url.scheme == "https" and url.hostname in hosts and not url.username and not url.password and url.port in (None, 443):
        return value
    return ""


def search_commons(terms: list, timeout: float) -> list:
    """Ask for a few bitmap results with thumbnails and attribution in one request."""
    params = {
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": " ".join(terms) + " filetype:bitmap",
        "gsrnamespace": "6", "gsrlimit": "5", "prop": "imageinfo",
        "iiprop": "url|mime|extmetadata", "iiurlwidth": "288", "iiurlheight": "192",
        "iiextmetadatafilter": "Artist|Credit|Attribution|LicenseShortName|LicenseUrl|ImageDescription|Categories|Restrictions",
        "maxlag": "5",
    }
    request = Request(API + "?" + urlencode(params), headers={
        "User-Agent": os.getenv("IMAGE_USER_AGENT", "UGRComedorDaily/1.0 (daily university menu illustrations)"),
        "Accept": "application/json",
    })
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(1_000_001)
        if len(payload) > 1_000_000:
            raise ValueError("Commons response exceeds the expected size")
        result = json.loads(payload)
    if "error" in result:
        raise ValueError("Commons could not serve the search")
    return result.get("query", {}).get("pages", [])


def select_image(pages: list, terms: list) -> DishImage | None:
    """Prefer relevant filenames and require usable thumbnails and clear CC licensing."""
    candidates = []
    for page in pages:
        try:
            info = page.get("imageinfo", [])[0]
            metadata = info.get("extmetadata", {})
            field = lambda key: plain_text(metadata.get(key, {}).get("value", ""))
            title = page.get("title", "").removeprefix("File:")
            description = set(words(title + " " + field("ImageDescription") + " " + field("Categories")))
            if not FOOD_CONTEXT.search(" ".join(description)):
                continue
            # Keep every searched word, particularly vegetarian and vegan qualifiers.
            if not set(terms).issubset(description):
                continue
            if info.get("mime") not in ("image/jpeg", "image/png") or field("Restrictions"):
                continue
            thumbnail = safe_url(info.get("thumburl", ""), {"upload.wikimedia.org", "thumb.wikimedia.org"})
            source = safe_url(info.get("descriptionurl", ""), {"commons.wikimedia.org"})
            license_url = field("LicenseUrl").replace("http://creativecommons.org/", "https://creativecommons.org/", 1)
            license_url = safe_url(license_url, {"creativecommons.org"})
            license_path = urlsplit(license_url).path
            allowed = re.fullmatch(r"/licenses/by(?:-sa)?/(?:1\.0|2\.0|2\.5|3\.0|4\.0)/?(?:deed\.[a-z-]+)?", license_path)
            allowed = allowed or re.fullmatch(r"/publicdomain/zero/1\.0/?(?:deed\.[a-z-]+)?", license_path)
            artist = field("Attribution") or field("Artist")
            if not all((thumbnail, source, artist, field("LicenseShortName"), allowed)):
                continue
            photo = DishImage(thumbnail, source, title, artist, field("Credit"), field("LicenseShortName"), license_url)
            score = len(set(terms) & set(words(title)))
            candidates.append((score, -page.get("index", 0), photo))
        except (AttributeError, IndexError, TypeError, ValueError):
            # One malformed result must not prevent another candidate from being used.
            continue
    return max(candidates, key=lambda item: item[:2])[2] if candidates else None


def find_images(menus: dict) -> dict:
    """Search afresh per newsletter batch, with bounded work and text-only fallback."""
    dishes = list(dict.fromkeys(dish for rows in menus.values() for course, dish in rows
                               if course in ("primero", "segundo")))
    found = {}
    deadline = time.monotonic() + 30
    # Normal menus have four dishes; allow extra PTS alternatives without unbounded calls.
    for dish in dishes[:8]:
        terms = search_terms(dish)
        if not terms:
            continue
        queries = [terms]
        # A shorter search helps named house recipes, while retaining dietary qualifiers.
        if len(terms) > 2:
            dietary = [word for word in terms if word.startswith(("vegetari", "vegan"))]
            simpler = list(dict.fromkeys(terms[:2] + dietary))
            if simpler != terms:
                queries.append(simpler)
        for query in queries:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                LOG.info("Image search time budget reached; keeping remaining dishes text-only")
                return found
            try:
                photo = select_image(search_commons(query, min(5, remaining)), query)
            except Exception as error:
                # Images are optional. Stop this batch on service failures or rate limiting.
                LOG.warning("Image search unavailable (%s); continuing newsletter without remaining photos", type(error).__name__)
                return found
            if photo:
                found[dish] = photo
                break
    LOG.info("Illustrative images found for %s/%s distinct starter and main dishes", len(found), len(dishes))
    return found
