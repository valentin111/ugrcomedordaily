"""Fetch today's UGR menu and send a small daily newsletter using only Python."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from html import escape
from html.parser import HTMLParser
import logging
import os
from pathlib import Path
import re
import signal
import smtplib
import sqlite3
import ssl
import threading
import time as clock
import unicodedata
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from dish_images import enabled_from_env, find_images


SOURCE = "https://scu.ugr.es/"
LOCATIONS = {"main": "Fuentenueva · Cartuja · Aynadamar", "pts": "PTS"}
MONTHS = dict(zip(
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(),
    range(1, 13),
))
COURSES = {"primero": "First course", "segundo": "Second course", "acompanamiento": "Side", "postre": "Dessert"}
LOG = logging.getLogger("ugr-menu")


def normalized(value: str) -> str:
    """Ignore accents, casing and extra whitespace when matching page labels."""
    value = unicodedata.normalize("NFKD", value)
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).lower().split())


class PageParser(HTMLParser):
    """Keep headings and table rows in document order, including nested text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
        self.heading = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        """Track only the structural elements used by UGR's menu tables."""
        if tag == "h1":
            self.heading = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "br":
            self.handle_data(" ")

    def handle_data(self, data):
        """Preserve dish text and accents without including HTML markup."""
        if self.heading is not None:
            self.heading.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        """Finish text units without expanding colspan into imaginary cells."""
        if tag == "h1" and self.heading is not None:
            self.events.append(("heading", " ".join("".join(self.heading).split())))
            self.heading = None
        elif tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.events.append(("row", self.row))
            self.row = None


def extract_menu(html: str, target: date, location: str = "main") -> dict:
    """Select an exact date and campus; reject incomplete or changed layouts."""
    parser = PageParser()
    parser.feed(html)
    active = False
    seen_section = False
    seen_date = False
    seen_target = False
    day = None
    option = None
    menus = {}
    for kind, value in parser.events:
        if kind == "heading":
            heading = normalized(value)
            active = "menu semanal" in heading and (
                all(name in heading for name in ("fuentenueva", "cartuja", "aynadamar"))
                if location == "main" else "comedor pts" in heading
            )
            seen_section |= active
            day, option = None, None
            continue
        if not active or not value:
            continue
        label = normalized(value[0])
        match = re.search(r"\b(\d{1,2}) de ([a-z]+) de (\d{4})\b", label)
        if match:
            day = date(int(match[3]), MONTHS[match[2]], int(match[1]))
            seen_date = True
            seen_target |= day == target
            option = None
        elif re.fullmatch(r"menu [12]", label):
            option = label[-1]
            if day == target:
                if option in menus:
                    raise ValueError("Duplicate menu for this date; refusing an ambiguous newsletter")
                menus[option] = []
        elif day == target and option and label in COURSES:
            if len(value) < 2 or not value[1]:
                raise ValueError("A dish is missing from today's menu")
            menus[option].append((label, value[1]))
    if not seen_section or not seen_date:
        raise ValueError("No recognizable dated menu section; UGR's page may have changed")
    if seen_target and (set(menus) != {"1", "2"} or any(
        not set(COURSES).issubset({course for course, _ in dishes}) for dishes in menus.values()
    )):
        raise ValueError("Today's menu is incomplete; waiting for a complete publication")
    return menus


def fetch_page() -> str:
    """Fetch the public page with TLS validation and a bounded response size."""
    request = Request(SOURCE, headers={"User-Agent": "UGRComedorDaily/1.0", "Cache-Control": "no-cache"})
    with urlopen(request, timeout=30) as response:
        payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("UGR response exceeds the expected page size")
        return payload.decode(response.headers.get_content_charset() or "utf-8")


def newsletter(day: date, menus: dict, location: str, images: dict = None) -> tuple:
    """Create concise plain-text and HTML versions with the original dish names."""
    title = f"UGR menu · {day:%d %b %Y}"
    campus = LOCATIONS[location]
    lines = [title, campus, ""]
    blocks = []
    photo_credits = {}
    for option, dishes in menus.items():
        # Identify the vegetarian option consistently in text and HTML emails.
        menu_label = f"Menu {option}" + (" · Vegetarian" if option == "2" else "")
        lines.append(menu_label)
        rows = []
        for course, dish in dishes:
            lines.append(f"{COURSES[course]}: {dish}")
            rows.append(f"<div><strong>{COURSES[course]}:</strong> {escape(dish)}</div>")
            photo = (images or {}).get(dish) if course in ("primero", "segundo") else None
            if photo:
                # Use linked thumbnails to keep emails small; all provider text is escaped.
                rows.append(
                    "<div style='margin:8px 0 16px'>"
                    f"<a href='{escape(photo.source_url, quote=True)}'>"
                    f"<img src='{escape(photo.url, quote=True)}' alt='{escape(dish, quote=True)}' "
                    "width='288' height='192' style='display:block;width:100%;max-width:288px;"
                    "height:192px;object-fit:cover;object-position:center;border:0'></a>"
                    "</div>"
                )
                # Credit each photo once in the footer, even when both menus reuse it.
                photo_credits[photo.source_url] = (
                    f"<a href='{escape(photo.source_url, quote=True)}'>{escape(photo.title)}</a> — "
                    f"{escape(photo.artist)}, {escape(photo.credit)} "
                    f"(<a href='{escape(photo.license_url, quote=True)}'>{escape(photo.license_name)}</a>)"
                )
        lines.append("")
        # Inline-block columns wrap without CSS support; Outlook gets a fallback table.
        blocks.append(
            "<!--[if mso]><td width='320' valign='top'><![endif]-->"
            "<div class='menu-column' style='display:inline-block;vertical-align:top;"
            "width:100%;max-width:320px;box-sizing:border-box;padding:0 12px 0 0;font-size:16px'>"
            f"<h2 style='font-size:18px;margin-bottom:8px'>{escape(menu_label)}</h2>"
            + "".join(rows) + "</div><!--[if mso]></td><![endif]-->"
        )
    lines.append(f"Full menu, allergens and updates: {SOURCE}")
    # Keep credits in one compact paragraph that can wrap naturally on narrow screens.
    footer = (
        "<p style='font-size:10px;color:#666;line-height:1.4'>Imágenes orientativas (recortadas) · Wikimedia Commons · "
        + " · ".join(photo_credits.values()) + "</p>"
    ) if photo_credits else ""
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<style>@media screen and (max-width:687px){"
        ".menu-column{display:block!important;max-width:100%!important;padding:0!important;}"
        "}</style></head><body "
        "style='font-family:Arial,sans-serif;color:#222;line-height:1.6'>"
        "<main style='max-width:640px;margin:auto;padding:24px'>"
        f"<h1 style='font-size:24px;margin-bottom:0'>{escape(title)}</h1><p>{escape(campus)}</p>"
        "<div style='font-size:0;text-align:left'>"
        "<!--[if mso]><table role='presentation' width='640' cellpadding='0' cellspacing='0' border='0'><tr><![endif]-->"
        + "".join(blocks)
        + "<!--[if mso]></tr></table><![endif]--></div>"
        + f"<p><a href='{SOURCE}'>Full menu, allergens and updates</a></p>"
        + footer + "</main></body></html>"
    )
    return title, "\n".join(lines), body


@dataclass(frozen=True)
class Config:
    """Environment settings shared by one-shot and scheduled delivery."""

    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str
    recipients: tuple
    test_recipients: tuple
    location: str
    timezone: ZoneInfo
    send_at: time
    retry_until: time
    retry_seconds: int
    state_path: Path
    include_images: bool = True

    @classmethod
    def from_env(cls):
        """Fail early on invalid settings; optionally read the SMTP password from a secret file."""
        def address(value):
            if not re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+", value) or not value.isascii():
                raise ValueError("Use plain ASCII email addresses, without display names")
            return value

        security = os.getenv("SMTP_SECURITY", "starttls")
        if security not in ("starttls", "ssl"):
            raise ValueError("SMTP_SECURITY must be starttls or ssl")
        host = os.environ["SMTP_HOST"].strip()
        if not host:
            raise ValueError("SMTP_HOST is required")
        port = int(os.getenv("SMTP_PORT", "465" if security == "ssl" else "587"))
        if not 1 <= port <= 65535:
            raise ValueError("SMTP_PORT must be between 1 and 65535")
        password = os.getenv("SMTP_PASSWORD", "")
        secret = os.getenv("SMTP_PASSWORD_FILE", "")
        if secret:
            if password:
                raise ValueError("Set only one of SMTP_PASSWORD and SMTP_PASSWORD_FILE")
            password = Path(secret).read_text().rstrip("\r\n")
        username = os.getenv("SMTP_USERNAME", "")
        if bool(username) != bool(password):
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must both be set, or both omitted")
        # Normal recipients are required; the separate test list can remain empty until needed.
        recipients = tuple(dict.fromkeys(address(x.strip()) for x in os.environ["RECIPIENTS"].split(",")))
        test_recipients = tuple(dict.fromkeys(
            address(value.strip()) for value in os.getenv("TEST_RECIPIENTS", "").split(",") if value.strip()
        ))
        location = os.getenv("MENU_LOCATION", "main")
        if location not in LOCATIONS:
            raise ValueError("MENU_LOCATION must be main or pts")
        send_at = time.fromisoformat(os.getenv("SEND_AT", "09:00"))
        retry_until = time.fromisoformat(os.getenv("RETRY_UNTIL", "12:00"))
        if send_at.tzinfo or retry_until.tzinfo or send_at >= retry_until:
            raise ValueError("SEND_AT must be before RETRY_UNTIL, using local clock times")
        retry_seconds = int(os.getenv("RETRY_SECONDS", "900"))
        if retry_seconds < 60:
            raise ValueError("RETRY_SECONDS must be at least 60")
        return cls(host, port, security, username, password, address(os.environ["MAIL_FROM"]),
                   recipients, test_recipients, location, ZoneInfo(os.getenv("TZ", "Europe/Madrid")),
                   send_at, retry_until, retry_seconds, Path(os.getenv("STATE_PATH", "data/delivery.sqlite3")),
                   enabled_from_env())


def send_email(config: Config, recipient: str, content: tuple):
    """Send each recipient a private multipart email over verified TLS."""
    subject, plain, html = content
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = recipient
    message["Date"] = format_datetime(datetime.now(config.timezone))
    message["Message-ID"] = make_msgid(domain=config.sender.rsplit("@", 1)[1])
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    context = ssl.create_default_context()
    if config.security == "ssl":
        smtp = smtplib.SMTP_SSL(config.host, config.port, timeout=30, context=context)
    else:
        smtp = smtplib.SMTP(config.host, config.port, timeout=30)
    try:
        if config.security == "starttls":
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if config.username:
            smtp.login(config.username, config.password)
        smtp.send_message(message, from_addr=config.sender, to_addrs=[recipient])
    finally:
        # A failed QUIT after acceptance must not trigger another delivery.
        try:
            smtp.quit()
        except (OSError, smtplib.SMTPException):
            smtp.close()


def open_state(path: Path):
    """Store accepted deliveries in a tiny SQLite file on the persistent volume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=300)
    connection.execute("CREATE TABLE IF NOT EXISTS deliveries (day TEXT, location TEXT, recipient TEXT, "
                       "PRIMARY KEY(day, location, recipient))")
    connection.commit()
    return connection


def deliver(config: Config, day: date, connection) -> bool:
    """Retry only unsent recipients; serialize sends against concurrent one-shot runs."""
    # Keep the day rule here as well as in the scheduler so manual sends obey it.
    if day.weekday() >= 4:
        LOG.info("Friday through Sunday: newsletter disabled for %s", day)
        return True
    key = (day.isoformat(), config.location)
    sent = {row[0] for row in connection.execute(
        "SELECT recipient FROM deliveries WHERE day=? AND location=?", key)}
    pending = [recipient for recipient in config.recipients if recipient not in sent]
    if not pending:
        return True
    menus = extract_menu(fetch_page(), day, config.location)
    if not menus:
        LOG.info("No menu published for %s; no email sent", day)
        return False
    images = find_images(menus) if config.include_images else {}
    content = newsletter(day, menus, config.location, images)
    complete = True
    for index, recipient in enumerate(pending, 1):
        try:
            # Keep the lock through SMTP acceptance and the durable delivery record.
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute("SELECT 1 FROM deliveries WHERE day=? AND location=? AND recipient=?",
                                        (*key, recipient)).fetchone()
            if not exists:
                send_email(config, recipient, content)
                connection.execute("INSERT INTO deliveries VALUES (?, ?, ?)", (*key, recipient))
            connection.commit()
            if not exists:
                LOG.info("Mail accepted for recipient %s/%s on %s", index, len(pending), day)
        except (OSError, smtplib.SMTPException, sqlite3.Error) as error:
            connection.rollback()
            # Provider error messages can contain recipient addresses or credentials.
            LOG.error("Delivery failed for recipient %s/%s (%s); will retry", index, len(pending), type(error).__name__)
            complete = False
    return complete


def send_test_email(config: Config, day: date) -> bool:
    """Send today's menu only to test recipients without schedule or delivery-state checks."""
    if not config.test_recipients:
        LOG.error("TEST_RECIPIENTS is empty; no test email sent")
        return False
    menus = extract_menu(fetch_page(), day, config.location)
    if not menus:
        LOG.info("No menu published for %s; no test email sent", day)
        return False
    images = find_images(menus) if config.include_images else {}
    subject, plain, html = newsletter(day, menus, config.location, images)
    content = (
        f"[TEST] {subject}",
        f"TEST EMAIL — normal delivery records are unchanged.\n\n{plain}",
        html.replace("<main ", "<div style='background:#fff3cd;padding:12px'>TEST EMAIL — normal delivery records are unchanged.</div><main ", 1),
    )
    complete = True
    for index, recipient in enumerate(config.test_recipients, 1):
        try:
            send_email(config, recipient, content)
            LOG.info("Test mail accepted for recipient %s/%s on %s", index, len(config.test_recipients), day)
        except (OSError, smtplib.SMTPException) as error:
            # Provider messages can contain addresses or credentials, so log only the error class.
            LOG.error("Test delivery failed for recipient %s/%s (%s)",
                      index, len(config.test_recipients), type(error).__name__)
            complete = False
    return complete


def in_window(now: datetime, config: Config) -> bool:
    """Use local wall time so the 09:00 schedule follows daylight-saving changes."""
    local = now.astimezone(config.timezone)
    # Python numbers Monday as 0, so values 4 through 6 cover Friday to Sunday.
    return local.weekday() < 4 and config.send_at <= local.time() < config.retry_until


def run(config: Config, connection):
    """Sleep between checks and retry missing menus or failed sends until noon."""
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    retry_at = 0.0
    current_day = None
    complete = False
    LOG.info("Daily schedule: %s %s; retries until %s", config.send_at, config.timezone, config.retry_until)
    while not stopped.is_set():
        now = datetime.now(config.timezone)
        if now.date() != current_day:
            current_day, complete, retry_at = now.date(), False, 0.0
        if not complete and in_window(now, config) and clock.monotonic() >= retry_at:
            try:
                complete = deliver(config, current_day, connection)
            except Exception as error:
                LOG.error("Menu check failed (%s); will retry", type(error).__name__)
            retry_at = clock.monotonic() + config.retry_seconds
        stopped.wait(15)


def main():
    """Expose safe previews and an explicit one-shot sender alongside the scheduler."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Run the daily scheduler")
    commands.add_parser("once", help="Send today's menu now, honoring existing delivery records")
    commands.add_parser("test-email", help="Send today's menu to TEST_RECIPIENTS without recording delivery")
    preview = commands.add_parser("preview", help="Print a menu without sending or needing SMTP settings")
    preview.add_argument("--date", type=date.fromisoformat)
    preview.add_argument("--file", type=Path, help="Read a saved UGR HTML page instead of the live site")
    preview.add_argument("--html", action="store_true", help="Print the HTML email instead of plain text")
    preview.add_argument("--no-images", action="store_true", help="Skip live image searches in an HTML preview")
    preview.add_argument("--location", choices=LOCATIONS, default=os.getenv("MENU_LOCATION", "main"))
    args = parser.parse_args()
    if args.command == "preview":
        day = args.date or datetime.now(ZoneInfo(os.getenv("TZ", "Europe/Madrid"))).date()
        html = args.file.read_text(encoding="utf-8") if args.file else fetch_page()
        menus = extract_menu(html, day, args.location)
        images = find_images(menus) if menus and args.html and not args.no_images and enabled_from_env() else {}
        print(newsletter(day, menus, args.location, images)[2 if args.html else 1] if menus else f"No menu published for {day}.")
        return 0
    config = Config.from_env()
    if args.command == "test-email":
        return 0 if send_test_email(config, datetime.now(config.timezone).date()) else 1
    connection = open_state(config.state_path)
    try:
        if args.command == "once":
            return 0 if deliver(config, datetime.now(config.timezone).date(), connection) else 1
        run(config, connection)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        LOG.error("Application failed (%s): check configuration, network and source page", type(error).__name__)
        raise SystemExit(1)
