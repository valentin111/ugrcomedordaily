# UGR Comedor Daily

A small container that emails the current day's UGR menu at **09:00 Europe/Madrid**, **Monday through Thursday only**. It uses Python's standard library, with no third-party Python packages, web server or external database.

The default newsletter includes both menus for **Fuentenueva / Cartuja / Aynadamar**, with English labels and the original Spanish dishes. The separate PTS menu is also supported. Each recipient receives an individual email with plain-text and HTML versions and a link to UGR for allergens and updates. The HTML version searches Wikimedia Commons for illustrative starter and main-course photos each day; no dish explanations or translations are added.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and technology decisions.

The second option is labelled **Menu 2 · Vegetarian** in both email formats and previews. HTML emails show the menus side by side on wide screens and stacked on mobile, with photo credits in a shared footer. Images remain at most 288 pixels wide and have a fixed height of 192 pixels, using centered cropping to preserve proportions in clients that support `object-fit`.

## Start with Docker Compose

You need an always-on machine with Docker Compose and an SMTP account that permits sending from your chosen address.

```sh
cp .env.example .env
# Edit .env with your provider's SMTP details, sender and recipients.
docker compose up -d --build
docker compose logs --tail=50 -f
```

No ports need to be opened. The service makes outbound HTTPS and SMTP connections. It runs as a non-root user with a read-only root filesystem. Delivery records live in the `delivery-data` volume.

Docker must be running before these commands can work. Keep `.env` private; it is excluded from version control and the image build.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SMTP_HOST` | Required | Your mail provider's SMTP server. |
| `SMTP_PORT` | `587` or `465` | Defaults to 587 for STARTTLS, 465 for implicit TLS. |
| `SMTP_SECURITY` | `starttls` | `starttls` or `ssl`; certificates are verified. |
| `SMTP_USERNAME` | Empty | SMTP login; omit with the password for a trusted TLS relay. |
| `SMTP_PASSWORD` | Empty | Provider SMTP password or app password, when required. |
| `SMTP_PASSWORD_FILE` | Empty | Alternative: path inside the container to a mounted secret file. |
| `MAIL_FROM` | Required | Sender email address; must be allowed by your provider. |
| `RECIPIENTS` | Required | Fixed comma-separated email addresses; duplicates are removed. |
| `TEST_RECIPIENTS` | Empty | Separate comma-separated addresses used only by `test-email`. |
| `MENU_LOCATION` | `main` | `main` for the three shared campuses, or `pts`. |
| `IMAGE_SEARCH_ENABLED` | `true` | Search Commons for starter and main photos; `false` disables image requests. |
| `IMAGE_USER_AGENT` | Application identifier | Optional Commons API identifier with your project URL or contact. |
| `TZ` | `Europe/Madrid` | Schedule timezone, including daylight-saving changes. |
| `SEND_AT` | `09:00` | First check each eligible day. |
| `RETRY_UNTIL` | `12:00` | Stop retries at this local time; must be after `SEND_AT`. |
| `RETRY_SECONDS` | `900` | Retry every 15 minutes; minimum 60 seconds. |
| `STATE_PATH` | `/data/delivery.sqlite3` in Docker | Persistent delivery database. Locally defaults to `data/delivery.sqlite3`. |

Use plain email addresses without display names. In `.env`, single-quote passwords containing special characters such as `$` or `#`. For a secret file, mount it read-only into the container, set `SMTP_PASSWORD_FILE` to that path and remove `SMTP_PASSWORD`.

After changing settings, run `docker compose up -d` to recreate the service with the updated environment. A simple container restart does not load changed environment variables.

## Schedule and retries

- The scheduler checks its clock every 15 seconds, so the first attempt starts at approximately 09:00 local time.
- Friday, Saturday and Sunday are excluded from scheduled sends and `once`. Previews and explicit `test-email` sends can still use menus from those days.
- Monday through Thursday, the service sends only when UGR publishes a complete menu for that exact date. There is no fallback to a previous date or another campus.
- Missing or incomplete menus and temporary failures are retried until noon. Days without a published menu produce no email.
- Restarting between 09:00 and noon catches up for the current day. Starting after noon waits for the next eligible day. Old dates are never backfilled.
- Every recipient's successful SMTP acceptance is stored immediately. Restarts and retries skip recipients already recorded for that date and location.

Keep the volume: `docker compose down` preserves it; `docker compose down -v` deletes it and removes duplicate-send protection. Run one scheduled instance.

SMTP cannot guarantee exactly-once delivery: a crash after the provider accepts the email but before the database records it can cause a duplicate on retry. SMTP acceptance also does not confirm inbox placement. Normal retries and restarts use the recorded acceptance to avoid duplicates.

## Preview without sending

Preview today's live menu in the container. This needs a valid `.env` file for Compose to start, but the preview command does not use its SMTP settings.

```sh
docker compose run --rm --no-deps menu-mailer preview
```

Or use Python 3.9+ locally with timezone data installed. No package installation or SMTP settings are required:

```sh
python3 app.py preview
python3 app.py preview --date 2026-09-21 --file tests/fixtures/ugr-2026-09-19.html
python3 app.py preview --date 2026-09-21 --file tests/fixtures/ugr-2026-09-19.html --html > /tmp/ugr-menu-preview.html
```

The date above is a fixture example, not a configured sending date. Normal sending always uses today's local date. HTML previews perform live image searches even with a saved menu file. Add `--no-images` for an offline HTML preview. Plain-text previews never query the image service.

## Daily photos

Before sending a newsletter, the service searches the public Wikimedia Commons API using the Spanish dish names. It searches each distinct starter and main once per batch and shares the selected images across recipients. There is no fixed photo catalogue, account, API key or extra Python package. New daily deliveries, manual test sends and retries perform fresh searches; the same photo can naturally match on different days.

For longer names, a second search uses the first two meaningful words while preserving vegetarian or vegan labels. Candidates must match the search words and have food-preparation context in their title or metadata, a JPEG/PNG thumbnail, and usable author and Creative Commons license details. Thumbnails link to Commons. A single compact footer labelled **Imágenes orientativas** lists each unique photo's title, credit, source and license link. These are automatically matched illustrations, not photos of UGR's actual meals. Matching is approximate, and there will be dishes without photos.

Image searches have a five-second request timeout and a 30-second batch budget, checked between requests, for up to eight distinct dishes. Search errors, rate limits or missing matches leave the affected dishes text-only and do not prevent sending. The plain-text email is unchanged. Thumbnails are loaded remotely by the email client, so some recipients may need to enable external images. Set `IMAGE_SEARCH_ENABLED=false` to disable the feature.

## Send now or stop

The following command **sends real email to the configured recipients**. It uses today's date, skips Friday through Sunday and respects existing delivery records. It deliberately bypasses the usual 09:00–12:00 time window.

```sh
docker compose run --rm --no-deps menu-mailer once
```

Its exit code is 0 when every recipient has been accepted already, delivery succeeds, or Friday through Sunday is skipped; 1 means no complete menu or a delivery/configuration error. It does not retry within that one-shot invocation.

To send today's newsletter only to the separate `TEST_RECIPIENTS` list:

```sh
docker compose run --rm --no-deps menu-mailer test-email
```

This is a real email send. It works on any weekday, prefixes the subject with `[TEST]`, ignores normal delivery records, and does not update them. Every invocation sends again to every configured test recipient. It never sends to `RECIPIENTS`; an empty `TEST_RECIPIENTS` list exits with an error without fetching or sending.

```sh
docker compose down
```

## Tests and operation

```sh
python3 -m unittest discover -s tests -v
docker compose logs --tail=100
docker stats --no-stream
```

Tests use a saved public UGR page downloaded on 19 September 2026 and mocked SMTP. They cover date and campus selection, multiple dishes, incomplete menus, Friday-through-Sunday blocking, daylight-saving changes, persistence, isolated repeatable test sends, partial delivery failures, TLS modes and email formatting. They do not send real messages.

The container has a 64 MiB memory limit; actual resource usage should be checked on the deployment host. Logs are rotated and contain no recipient addresses or passwords. Failures log their exception class: for example, `SMTPAuthenticationError` indicates rejected credentials and `ValueError` during a menu check can indicate changed page markup. Use `preview` and the tests to investigate parser failures. The lightweight service has no separate monitoring or alerting server.

## Sources

- [UGR menu page](https://scu.ugr.es/): public source for dates, locations and dishes.
- [Official Python container images](https://hub.docker.com/_/python): Alpine runtime image.
- [Python SMTP documentation](https://docs.python.org/3/library/smtplib.html): TLS and email transport behavior.
