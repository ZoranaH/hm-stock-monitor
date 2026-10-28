#!/usr/bin/env python3
"""Personal H&M size monitor. Python 3.10+; optional Playwright browser mode."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


LOG = logging.getLogger("hm-monitor")
STOP = threading.Event()
BASE = Path(__file__).resolve().parent
MAX_RESPONSE_BYTES = 8_000_000


class MonitorError(Exception):
    """An unknown result, never an out-of-stock result."""

    def __init__(
        self,
        message: str,
        retry_after: float = 0,
        blocked: bool = False,
        rate_limited: bool = False,
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.blocked = blocked
        self.rate_limited = rate_limited


class NextDataParser(HTMLParser):
    """Read H&M's embedded JSON without executing page scripts."""

    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


@dataclass(frozen=True)
class Watch:
    article: str
    label: str
    sizes: tuple[str, ...]
    country: str
    locale: str

    @property
    def key(self):
        return f"{self.locale}:{self.article}"

    @property
    def url(self):
        return f"https://www2.hm.com/{self.locale}/productpage.{self.article}.html"

    @property
    def stock_url(self):
        market = self.locale.split("_")[1]
        return (
            "https://ofg.hm.com/pdh-availability/v1/product/"
            f"{market}/availability/{self.article[:7]}"
        )


def normalize_size(value):
    return str(value).strip().upper()


def parse_metadata(html: str, watch: Watch) -> dict:
    parser = NextDataParser()
    parser.feed(html)
    try:
        payload = json.loads("".join(parser.parts))
        page = payload["props"]["pageProps"]["productPageProps"]
        details = page["aemData"]["productArticleDetails"]
        if page["articleCode"] != watch.article or page["locale"] != watch.locale:
            raise MonitorError("Product or country changed; refusing to infer stock.")
        variant = details["variations"][watch.article]
        sizes = {}
        for item in variant["sizes"]:
            label = normalize_size(item["name"])
            sku = item["sizeCode"]
            if not label or not isinstance(sku, str):
                raise MonitorError("Invalid size metadata.")
            if not re.fullmatch(re.escape(watch.article) + r"\d{3}", sku):
                raise MonitorError("Size belongs to another colour or article.")
            if label in sizes:
                raise MonitorError("Ambiguous size labels; mapping needs review.")
            sizes[label] = sku
        if not sizes:
            raise MonitorError("No size mapping; product may have been removed.")
        requested = set(watch.sizes) - {"*"}
        if not requested.issubset(sizes):
            raise MonitorError(f"Requested size missing. Page lists: {', '.join(sizes)}")
        coming_soon = variant["comingSoon"]
        if not isinstance(coming_soon, bool):
            raise MonitorError("Unrecognised coming-soon status.")
        return {
            "sizes": sizes,
            "name": details["productName"],
            "colour": variant["name"],
            "coming_soon": coming_soon,
            "fetched_at": time.time(),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise MonitorError("Product JSON is missing or changed; stock is unknown.") from error


def parse_stock(payload: dict, watch: Watch) -> tuple[set[str], set[str]]:
    """Validate the response before treating an empty list as out of stock."""
    if not isinstance(payload, dict) or "availability" not in payload:
        raise MonitorError("Availability response has an unexpected schema.")
    result = []
    for key in ("availability", "fewPieceLeft"):
        values = payload.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(sku, str)
            or not re.fullmatch(re.escape(watch.article[:7]) + r"\d{6}", sku)
            for sku in values
        ):
            raise MonitorError(f"Invalid {key} list; stock is unknown.")
        result.append(set(values))
    if not result[1].issubset(result[0]):
        raise MonitorError("Conflicting stock and low-stock lists.")
    return result[0], result[1]


def parse_page_stock(html: str, watch: Watch) -> tuple[set[str], set[str]]:
    """Read the stock snapshot supplied with this exact product page."""
    parser = NextDataParser()
    parser.feed(html)
    try:
        payload = json.loads("".join(parser.parts))
        page = payload["props"]["pageProps"]["productPageProps"]
        if page["articleCode"] != watch.article or page["locale"] != watch.locale:
            raise MonitorError("Product/country mismatch in page stock snapshot.")
        return parse_stock(page["ssrAvailability"], watch)
    except (KeyError, TypeError, ValueError):
        raise MonitorError("Page has no readable size-stock snapshot; stock is unknown.") from None


def matching_sizes(metadata: dict, available: set[str], watch: Watch) -> set[str]:
    if metadata["coming_soon"]:
        return set()
    requested = set(metadata["sizes"]) if "*" in watch.sizes else set(watch.sizes)
    return {size for size in requested if metadata["sizes"][size] in available}


def retry_delay(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


class Client:
    def __init__(self, timeout=30, spacing=2):
        self.timeout = timeout
        self.spacing = spacing
        self.last_hm_request = 0.0

    def request(self, url, payload=None, expected_url=None):
        is_hm = urlsplit(url).hostname in {"www2.hm.com", "ofg.hm.com"}
        target = "Telegram API"
        if is_hm:
            target = "H&M stock endpoint" if "/availability/" in url else "H&M product page"
        if is_hm:
            STOP.wait(max(0, self.spacing - (time.monotonic() - self.last_hm_request)))
            self.last_hm_request = time.monotonic()
        headers = {"User-Agent": "HMStockMonitor/1.0 (personal availability monitor)"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            headers["Cache-Control"] = "no-cache"
        try:
            with urlopen(Request(url, data=body, headers=headers), timeout=self.timeout) as response:
                if expected_url:
                    actual = urlsplit(response.url)
                    expected = urlsplit(expected_url)
                    if (actual.scheme, actual.hostname, actual.path) != (
                        expected.scheme, expected.hostname, expected.path
                    ):
                        raise MonitorError("Unexpected redirect; stock is unknown.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise MonitorError("Response exceeds the size limit.")
                return raw.decode("utf-8")
        except HTTPError as error:
            # Do not log exception URLs: Telegram URLs contain the secret token.
            raise MonitorError(
                f"{target}: HTTP {error.code}; request refused or failed.",
                retry_delay(error.headers.get("Retry-After")),
                blocked=is_hm and error.code in {403, 429},
                rate_limited=is_hm and error.code == 429,
            ) from None
        except (URLError, TimeoutError, OSError, UnicodeError):
            raise MonitorError(f"{target}: network, TLS or response decoding error.") from None

    def metadata(self, watch):
        return parse_metadata(self.request(watch.url, expected_url=watch.url), watch)

    def stock(self, watch):
        try:
            payload = json.loads(self.request(watch.stock_url, expected_url=watch.stock_url))
        except ValueError:
            raise MonitorError("Stock endpoint returned non-JSON content.") from None
        return parse_stock(payload, watch)


class BrowserClient:
    """Read ordinary browser page data; no stealth or challenge bypasses."""

    always_refresh_metadata = True

    def __init__(self, profile: Path, channel="chrome", headed=False, cdp_url=None):
        try:
            from playwright.sync_api import Error, TimeoutError, sync_playwright
        except ImportError:
            raise MonitorError("Browser mode needs: py -m pip install playwright") from None
        self.browser_error = Error
        self.timeout_error = TimeoutError
        self.pending = {}
        self.last_navigation = 0.0
        self.attached = cdp_url is not None
        self.context = None
        self.page = None
        self.runtime = sync_playwright().start()
        try:
            if self.attached:
                # Reuse only the dedicated Chrome session started by the user.
                # Keep its existing tabs and browser preferences intact.
                self.browser = self.runtime.chromium.connect_over_cdp(
                    cdp_url, timeout=15000, no_defaults=True
                )
                if not self.browser.contexts:
                    raise MonitorError("Connected Chrome has no browser context.")
                self.context = self.browser.contexts[0]
                self.page = self.context.new_page()
            else:
                self.context = self.runtime.chromium.launch_persistent_context(
                    user_data_dir=str(profile.resolve()),
                    channel=channel,
                    headless=not headed,
                    viewport={"width": 1280, "height": 900},
                )
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(30000)
            self.page.set_default_navigation_timeout(45000)
        except (Error, MonitorError, TypeError):
            self.close()
            if self.attached:
                raise MonitorError(
                    "Could not connect to the monitor's Chrome window. Run start_chrome.cmd "
                    "first and leave that window open. If Playwright is older than 1.60, "
                    "run 'py -m pip install -r requirements-browser.txt'."
                ) from None
            raise MonitorError(
                f"Could not start {channel}. Check that it is installed and this monitor's "
                "profile is not already in use. For bundled Chromium, run "
                "'py -m playwright install chromium' and add '--browser-channel chromium'."
            ) from None

    def close(self):
        try:
            if self.attached:
                # Close only our tab. Stopping Playwright disconnects without
                # closing the externally started browser or the user's tabs.
                if self.page is not None:
                    self.page.close()
            elif self.context is not None:
                self.context.close()
        except self.browser_error:
            # Chrome may already have been closed manually.
            pass
        finally:
            self.runtime.stop()

    def metadata(self, watch):
        """Load a fresh page and remember its matching stock for stock()."""
        self.pending.pop(watch.key, None)
        STOP.wait(max(0, 2 - (time.monotonic() - self.last_navigation)))
        self.last_navigation = time.monotonic()
        market = watch.locale.split("_")[1]
        stock_paths = {
            ("ofg.hm.com", f"/pdh-availability/v1/product/{market}/availability/{watch.article[:7]}"),
            ("www2.hm.com", f"/hmwebservices/service/product/{market}/availability/{watch.article[:7]}.json"),
        }
        stock_responses = []

        def collect_stock(response):
            parts = urlsplit(response.url)
            if (parts.hostname, parts.path) in stock_paths and response.status not in {301, 302, 307, 308}:
                stock_responses.append(response)

        self.page.on("response", collect_stock)
        response = None
        stage = "navigation"
        try:
            response = self.page.goto(watch.url, wait_until="domcontentloaded")
            if response is None:
                raise MonitorError("Browser navigation returned no product response.")
            if response.status >= 400:
                self.log_refusal(response, watch)
                raise MonitorError(
                    f"Browser product page: HTTP {response.status}. H&M refused this browser request.",
                    retry_delay(response.headers.get("retry-after")),
                    blocked=response.status in {403, 429},
                    rate_limited=response.status == 429,
                )
            expected = urlsplit(watch.url)
            actual = urlsplit(self.page.url)
            if (actual.scheme, actual.hostname, actual.path) != (expected.scheme, expected.hostname, expected.path):
                raise MonitorError("Browser product page redirected; item may not be listed in this country.")
            stage = "product data script"
            self.page.locator("script#__NEXT_DATA__").wait_for(state="attached")
            stage = "stock data"
            # Allow stock requests initiated by the page to complete. If none are
            # observed, use the validated stock snapshot in this page's HTML.
            self.page.wait_for_timeout(1500)
            html = self.page.content()
            metadata = parse_metadata(html, watch)
            if stock_responses:
                stock_response = stock_responses[-1]
                if stock_response.status != 200:
                    self.log_refusal(stock_response, watch)
                    raise MonitorError(
                        f"Browser stock request: HTTP {stock_response.status}; stock is unknown.",
                        retry_delay(stock_response.headers.get("retry-after")),
                        blocked=stock_response.status in {403, 429},
                        rate_limited=stock_response.status == 429,
                    )
                try:
                    stock = parse_stock(stock_response.json(), watch)
                except ValueError:
                    raise MonitorError("Browser stock response was not valid JSON.") from None
            else:
                stock = parse_page_stock(html, watch)
            self.pending[watch.key] = stock
            return metadata
        except self.timeout_error:
            self.log_timeout(watch, stage, response)
            raise MonitorError(
                "Browser timed out loading product data. Check whether the site opens normally "
                "or shows a consent/access page."
            ) from None
        except self.browser_error:
            raise MonitorError("Browser could not read the page or stock response.") from None
        finally:
            self.page.remove_listener("response", collect_stock)

    def log_timeout(self, watch, stage, response):
        """Inspect the existing DOM only; never expose page text, URLs or cookies."""
        hint = "unclassified"
        has_data = "unknown"
        try:
            html = self.page.content().lower()
            has_data = "yes" if "__next_data__" in html else "no"
            if "verify you are human" in html or "captcha" in html:
                hint = "verification marker"
            elif "access denied" in html:
                hint = "access denied marker"
            elif "service unavailable" in html:
                hint = "service unavailable marker"
        except self.browser_error:
            hint = "DOM unavailable"
        LOG.warning(
            "%s | %s | timeout details: stage=%s; HTTP=%s; "
            "product-data marker=%s; page hint=%s",
            watch.country, watch.label, stage,
            response.status if response is not None else "unavailable",
            has_data, hint,
        )

    def log_refusal(self, response, watch):
        """Classify the response already received without logging cookies or HTML."""
        if response.status not in {403, 429} and not 500 <= response.status <= 599:
            return
        page_type = "unclassified refusal"
        try:
            body = response.text()[:20000].lower()
            if "captcha" in body or "verify you are human" in body:
                page_type = "verification page"
            elif "access denied" in body:
                page_type = "access denied page"
            elif "too many requests" in body:
                page_type = "rate limit page"
            elif "service unavailable" in body:
                page_type = "service unavailable page"
        except self.browser_error:
            page_type = "response body unavailable"
        server = re.sub(r"[^A-Za-z0-9 ._/-]", "?", response.headers.get("server", "unspecified"))[:80]
        LOG.warning(
            "%s | %s | refusal details: HTTP %s; server=%s; type=%s; Retry-After=%.0fs",
            watch.country, watch.label, response.status, server, page_type,
            retry_delay(response.headers.get("retry-after")),
        )

    def stock(self, watch):
        if watch.key not in self.pending:
            self.metadata(watch)
        return self.pending.pop(watch.key)


@contextmanager
def stock_client(args):
    if not args.browser and not args.connect_chrome:
        yield Client()
        return
    cdp_url = "http://127.0.0.1:9222" if args.connect_chrome else None
    browser = BrowserClient(args.browser_profile, args.browser_channel, args.headed, cdp_url)
    try:
        yield browser
    finally:
        browser.close()


class Telegram:
    def __init__(self, client, require_chat=True):
        self.client = client
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", self.token):
            raise MonitorError("Set TELEGRAM_BOT_TOKEN in .env first.")
        if require_chat and not re.fullmatch(r"-?\d+", self.chat):
            raise MonitorError("Set TELEGRAM_CHAT_ID in .env first.")

    def call(self, method, payload):
        response = self.client.request(
            f"https://api.telegram.org/bot{self.token}/{method}", payload
        )
        try:
            result = json.loads(response)
            if result.get("ok") is not True:
                raise MonitorError("Telegram did not confirm success.")
            return result["result"]
        except (ValueError, TypeError, KeyError):
            raise MonitorError("Unexpected Telegram response.") from None

    def send(self, text):
        self.call("sendMessage", {
            "chat_id": self.chat,
            "text": text[:4000],
            "link_preview_options": {"is_disabled": True},
        })


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, separator, value = line.partition("=")
            if separator and key.strip() in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}:
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def state_lock(path: Path):
    """Avoid duplicate monitors using the same state on Linux or Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MonitorError("Another monitor is using this state file.") from None
        yield


def plan_transition(record: dict, current: set[str], notify_first: bool):
    """Require two successful absent checks before rearming a size alert."""
    confirmed = set(record.get("announced", []))
    misses = dict(record.get("misses", {}))
    if not record.get("initialized") and not notify_first:
        return set(), current, {}
    for size in confirmed - current:
        misses[size] = misses.get(size, 0) + 1
        if misses[size] >= 2:
            confirmed.discard(size)
            misses.pop(size, None)
    for size in current:
        misses.pop(size, None)
    return current - confirmed, confirmed, misses


def check_watch(client, notifier, watch, record, settings, dry_run):
    now = time.time()
    metadata = record.get("metadata")
    requested = set(watch.sizes) - {"*"}
    if (
        getattr(client, "always_refresh_metadata", False)
        or not metadata
        or not requested.issubset(metadata["sizes"])
        or now - metadata["fetched_at"] >= settings["metadata_refresh_hours"] * 3600
    ):
        metadata = client.metadata(watch)
    available, low = client.stock(watch)
    current = matching_sizes(metadata, available, watch)

    # Verify product metadata again before a new alert, including release status.
    potential = current - set(record.get("announced", []))
    article_has_stock = any(sku.startswith(watch.article) for sku in available)
    if potential or (metadata["coming_soon"] and article_has_stock):
        metadata = client.metadata(watch)
        available, low = client.stock(watch)
        current = matching_sizes(metadata, available, watch)

    new, confirmed, misses = plan_transition(record, current, settings["notify_on_first_check"])
    LOG.info("%s | %s | matching sizes: %s", watch.country, watch.label, ", ".join(sorted(current)) or "none")
    if dry_run:
        return
    if new:
        size_text = ", ".join(
            size + (" (low stock)" if metadata["sizes"][size] in low else "")
            for size in sorted(new)
        )
        notifier.send(
            f"H&M size available — {watch.country}\n{watch.label}\n"
            f"{metadata['name']} — {metadata['colour']}\n"
            f"Size(s): {size_text}\n{watch.url}\n"
            "Availability can change before checkout."
        )
        confirmed.update(new)

    # A failed send leaves the previous state untouched, so it can be retried.
    record.update({
        "metadata": metadata,
        "announced": sorted(confirmed),
        "misses": misses,
        "initialized": True,
        "last_success": now,
        "last_available": sorted(current),
        "failures": 0,
        "retry_at": 0,
        "last_error": None,
    })


def migrate_retry_state(state):
    """Retire the old blanket 403 pause without discarding stock or alert history."""
    legacy_pause = state.pop("pause_until", 0)
    if not legacy_pause:
        return

    # An old 429 may represent a server-wide limit. Keep that wait, while a
    # legacy 403 now keeps only the affected item's existing retry deadline.
    had_rate_limit = any(
        "HTTP 429" in (record.get("last_error") or "")
        for record in state.get("watches", {}).values()
    )
    if had_rate_limit:
        state["hm_retry_at"] = max(state.get("hm_retry_at", 0), legacy_pause)
    LOG.info("Migrated old retry state; saved stock and notification history were preserved.")


def cool_down_watch(notifier, watch, record, settings, error, attempts):
    """Apply an item-only cooldown after its attempts have been exhausted."""
    failures = record.get("failures", 0) + 1
    # Repeated failed cycles back off further; a successful check resets failures.
    # Bound the exponent so a long-running failure cannot create a huge integer.
    local_delay = min(
        settings["max_failure_cooldown_seconds"],
        settings["failure_cooldown_seconds"] * 2 ** min(failures - 1, 10),
    )
    delay = max(local_delay, error.retry_after)
    record.update({
        "failures": failures,
        "retry_at": time.time() + delay,
        "last_error": str(error),
        "last_attempt_count": attempts,
    })
    LOG.warning(
        "%s | %s | %s attempts failed; this item will retry in at least %.0f seconds.",
        watch.country, watch.label, attempts, delay,
    )
    if failures >= 3 and time.time() - record.get("error_alert_at", 0) >= 86400:
        try:
            notifier.send(
                f"H&M monitor needs attention — {watch.country}\n"
                f"{watch.label}\n{error}\n"
                f"Stock is unknown after {attempts} attempts. "
                f"This item will retry in at least {delay / 60:.0f} minutes.\n"
                + watch.url
            )
            record["error_alert_at"] = time.time()
        except MonitorError:
            LOG.error("Could not send the monitor error notification.")


def run_cycle(client, notifier, watches, state, settings, state_path, dry_run):
    if not dry_run:
        migrate_retry_state(state)
        save_state(state_path, state)
    hm_retry_at = 0 if dry_run else state.get("hm_retry_at", 0)
    if time.time() < hm_retry_at:
        LOG.warning(
            "H&M requested a rate-limit pause; retry in %.0f seconds. Stock is unknown.",
            hm_retry_at - time.time(),
        )
        return len(watches)

    # Resume unfinished items first, so a time-limited run cannot continually
    # favour products at the beginning of the configuration file.
    by_key = {watch.key: watch for watch in watches}
    previous_order = state.get("pending_checks", [])
    ordered_keys = list(dict.fromkeys(previous_order + list(by_key)))
    pending = []
    cooling_down = set()
    records = state.setdefault("watches", {})
    for key in ordered_keys:
        if key not in by_key:
            continue
        record = records.get(key, {})
        if not dry_run and time.time() < record.get("retry_at", 0):
            cooling_down.add(key)
            LOG.info(
                "%s | %s | waiting %.0f seconds before retry; stock is unknown.",
                by_key[key].country, by_key[key].label, record["retry_at"] - time.time(),
            )
            continue
        pending.append(by_key[key])

    attempts = {watch.key: 0 for watch in pending}
    retry_at = {watch.key: 0 for watch in pending}
    succeeded = set()
    failed = set()
    deadline = time.monotonic() + settings["cycle_budget_seconds"]
    max_attempts = 1 if dry_run else settings["attempts_per_item"]

    def save_progress():
        if not dry_run:
            # Unattempted items take priority over retries on the next run.
            unfinished = sorted(pending, key=lambda watch: attempts[watch.key] > 0)
            state["pending_checks"] = [watch.key for watch in unfinished]
            save_state(state_path, state)

    save_progress()
    while pending and not STOP.is_set() and time.monotonic() < deadline:
        # Each pass visits every eligible item once before retrying failures.
        # A single 403 does not prevent the other product/country checks.
        for watch in list(pending):
            if STOP.is_set() or time.monotonic() >= deadline or time.time() < hm_retry_at:
                break
            if time.time() < retry_at[watch.key]:
                continue

            record = records.get(watch.key, {}) if dry_run else records.setdefault(watch.key, {})
            attempts[watch.key] += 1
            LOG.info(
                "%s | %s | attempt %s/%s",
                watch.country, watch.label, attempts[watch.key], max_attempts,
            )
            try:
                check_watch(client, notifier, watch, record, settings, dry_run)
            except MonitorError as error:
                LOG.warning("%s | %s | UNKNOWN: %s", watch.country, watch.label, error)
                delay = max(settings["retry_delay_seconds"], error.retry_after)

                # A 429 is an explicit rate limit. Wait before any further H&M
                # requests, including other products, and preserve long waits
                # across workflow runs instead of sleeping past the job limit.
                if error.rate_limited:
                    delay = max(delay, settings["rate_limit_delay_seconds"])
                    hm_retry_at = time.time() + delay
                    if not dry_run:
                        state["hm_retry_at"] = hm_retry_at
                    LOG.warning("H&M rate limit: waiting at least %.0f seconds.", delay)

                retry_at[watch.key] = time.time() + delay
                if not dry_run:
                    record.update({
                        "last_error": str(error),
                        "last_attempt_count": attempts[watch.key],
                        "retry_at": retry_at[watch.key],
                    })
                if attempts[watch.key] >= max_attempts:
                    pending.remove(watch)
                    failed.add(watch.key)
                    if not dry_run:
                        cool_down_watch(
                            notifier, watch, record, settings, error, attempts[watch.key]
                        )
            else:
                pending.remove(watch)
                succeeded.add(watch.key)
                if not dry_run:
                    record["last_attempt_count"] = attempts[watch.key]
            save_progress()

        if not pending or STOP.is_set() or time.monotonic() >= deadline:
            break

        # Wait only until the next eligible retry. Long Retry-After values are
        # stored in state; other eligible items have already had their chance.
        delay = max(
            0,
            min(retry_at[watch.key] for watch in pending) - time.time(),
            hm_retry_at - time.time(),
        )
        if delay >= deadline - time.monotonic():
            break
        if STOP.wait(delay):
            break

    save_progress()
    errors = len(watches) - len(succeeded)
    attempted = sum(count > 0 for count in attempts.values())
    LOG.info(
        "Cycle summary: %s/%s items attempted; %s succeeded; "
        "%s failed after all attempts; %s skipped for cooldown; "
        "%s unfinished checks queued for a later run.",
        attempted, len(watches), len(succeeded), len(failed), len(cooling_down), len(pending),
    )
    heartbeat_hours = settings["heartbeat_hours"]
    if not dry_run and heartbeat_hours and time.time() - state.get("heartbeat_at", 0) >= heartbeat_hours * 3600:
        try:
            notifier.send(
                f"H&M monitor status: {len(succeeded)}/{len(watches)} checks succeeded "
                f"in this cycle; {len(failed)} failed after all attempts; "
                f"{len(cooling_down)} skipped for cooldown; {len(pending)} waiting to finish.\n"
                "Countries: " + ", ".join(dict.fromkeys(watch.country for watch in watches))
            )
            state["heartbeat_at"] = time.time()
            save_state(state_path, state)
        except MonitorError:
            LOG.error("Could not send the periodic status notification.")
    return errors


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    settings = {
        "interval_seconds": 900,
        "metadata_refresh_hours": 6,
        "notify_on_first_check": True,
        "heartbeat_hours": 24,
        "attempts_per_item": 3,
        "retry_delay_seconds": 60,
        "failure_cooldown_seconds": 1800,
        "max_failure_cooldown_seconds": 3600,
        "rate_limit_delay_seconds": 60,
        "cycle_budget_seconds": 300,
    }
    settings.update(config.get("settings", {}))
    # Existing configurations with a longer base cooldown keep their setting.
    if "max_failure_cooldown_seconds" not in config.get("settings", {}):
        base_cooldown = settings["failure_cooldown_seconds"]
        if type(base_cooldown) is int:
            settings["max_failure_cooldown_seconds"] = max(3600, base_cooldown)
    if settings["interval_seconds"] < 300 or settings["metadata_refresh_hours"] <= 0:
        raise MonitorError("Use an interval of at least 300 seconds and a positive metadata refresh interval.")
    retry_settings = {
        "attempts_per_item": (1, 5),
        "retry_delay_seconds": (5, 300),
        "failure_cooldown_seconds": (300, 86400),
        "max_failure_cooldown_seconds": (300, 86400),
        "rate_limit_delay_seconds": (30, 86400),
        "cycle_budget_seconds": (30, 300),
    }
    for name, (minimum, maximum) in retry_settings.items():
        value = settings[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise MonitorError(f"{name} must be an integer from {minimum} to {maximum}.")
    if settings["max_failure_cooldown_seconds"] < settings["failure_cooldown_seconds"]:
        raise MonitorError("max_failure_cooldown_seconds must be at least failure_cooldown_seconds.")
    watches = []
    seen = set()
    known_locales = {country["locale"] for country in config["countries"]}
    for item in config["items"]:
        selected_countries = item.get("countries")
        if selected_countries is not None and (
            not isinstance(selected_countries, list)
            or not selected_countries
            or any(locale not in known_locales for locale in selected_countries)
        ):
            raise MonitorError("An item's countries must be a nonempty list of configured locales.")
    for country in config["countries"]:
        if not re.fullmatch(r"[a-z]{2}_[a-z]{2}", country["locale"]):
            raise MonitorError("Invalid country locale.")
        for item in config["items"]:
            if "countries" in item and country["locale"] not in item["countries"]:
                continue
            if not re.fullmatch(r"\d{10}", item["article"]):
                raise MonitorError("Article numbers must be strings of exactly 10 digits.")
            sizes = tuple(normalize_size(value) for value in item["sizes"])
            if not sizes or any(not size for size in sizes) or ("*" in sizes and len(sizes) != 1):
                raise MonitorError("Use a nonempty sizes list, or [\"*\"] for any size.")
            watch = Watch(item["article"], item["name"], sizes, country["name"], country["locale"])
            if watch.key in seen:
                raise MonitorError("Duplicate product/country in configuration.")
            seen.add(watch.key)
            watches.append(watch)
    if not watches:
        raise MonitorError("The watch list is empty.")
    return settings, watches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=BASE / "config.json")
    parser.add_argument("--state", type=Path, default=BASE / "data/state.json")
    parser.add_argument("--env", type=Path, default=BASE / ".env")
    parser.add_argument("--once", action="store_true", help="Check once, then exit.")
    parser.add_argument("--dry-run", action="store_true", help="One check; no messages or state writes.")
    parser.add_argument("--telegram-chats", action="store_true", help="List private chat IDs after you message your bot.")
    parser.add_argument("--test-notification", action="store_true", help="Send one test message.")
    parser.add_argument("--browser", action="store_true", help="Read H&M through an ordinary browser using Playwright.")
    parser.add_argument("--connect-chrome", action="store_true", help="Connect to local Chrome opened with start_chrome.cmd.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (requires --browser).")
    parser.add_argument("--browser-channel", choices=["chrome", "msedge", "chromium"], default="chrome")
    parser.add_argument("--browser-profile", type=Path, default=BASE / "data/browser-profile")
    parser.add_argument("--limit", type=int, help="Check only the first N product/country entries, for diagnosis.")
    args = parser.parse_args()
    if args.connect_chrome and (args.browser or args.headed):
        parser.error("Use --connect-chrome on its own, without --browser or --headed")
    if args.headed and not args.browser:
        parser.error("--headed requires --browser")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    load_env(args.env)
    client = Client()
    if args.telegram_chats:
        telegram = Telegram(client, require_chat=False)
        chats = {}
        for update in telegram.call("getUpdates", {"timeout": 0}):
            chat = update.get("message", {}).get("chat", {})
            if chat.get("type") == "private":
                chats[chat["id"]] = chat.get("first_name", "Private chat")
        print(json.dumps(chats, ensure_ascii=False, indent=2))
        if not chats:
            print("Open your bot in Telegram, send /start, then run this command again.")
        return 0
    notifier = None if args.dry_run else Telegram(client)
    if args.test_notification:
        if notifier is None:
            raise MonitorError("Do not combine --test-notification with --dry-run.")
        notifier.send("H&M monitor test: notifications are working.")
        return 0
    settings, watches = load_config(args.config)
    if args.limit:
        watches = watches[:args.limit]
    mode = "connected Chrome" if args.connect_chrome else "browser" if args.browser else "direct HTTP"
    LOG.info("H&M monitor 1.4 | mode: %s", mode)
    with stock_client(args) as hm_client:
        if args.dry_run:
            return 1 if run_cycle(hm_client, None, watches, {}, settings, args.state, True) else 0
        with state_lock(args.state):
            state = json.loads(args.state.read_text()) if args.state.exists() else {"watches": {}}
            while not STOP.is_set():
                started = time.monotonic()
                errors = run_cycle(hm_client, notifier, watches, state, settings, args.state, False)
                if args.once:
                    return 1 if errors else 0
                delay = max(
                    1,
                    settings["interval_seconds"] - (time.monotonic() - started),
                    state.get("hm_retry_at", 0) - time.time(),
                )
                STOP.wait(delay)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MonitorError, OSError, ValueError, KeyError, TypeError) as error:
        LOG.error("Stopped: %s", error)
        raise SystemExit(1)
