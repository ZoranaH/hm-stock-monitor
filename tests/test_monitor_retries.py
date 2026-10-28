"""Offline regression tests: no H&M requests and no real Telegram messages."""

import copy
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import monitor


class Clock:
    def __init__(self):
        self.now = 2_000_000_000.0
        self.waits = []
        self.stopped = False

    def time(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return self.stopped

    def is_set(self):
        return self.stopped


class Notifier:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_path = Path(self.temporary.name) / "state.json"
        self.clock = Clock()
        self.notifier = Notifier()
        self.settings, self.watches = monitor.load_config(monitor.BASE / "config.json")
        self.settings["heartbeat_hours"] = 0
        # Keep the original configurable timings covered alongside the new defaults.
        self.settings.update({"retry_delay_seconds": 15, "failure_cooldown_seconds": 600})
        self.state = {"watches": {}}
        self.calls = []
        self.counts = Counter()
        self.behaviour = lambda watch, attempt: None
        for target, replacement in [
            ("time.time", self.clock.time),
            ("time.monotonic", self.clock.time),
            ("STOP", self.clock),
            ("check_watch", self.check),
        ]:
            patcher = patch.object(monitor, target, replacement) if "." not in target else patch(
                "monitor." + target, replacement
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def check(self, client, notifier, watch, record, settings, dry_run):
        self.calls.append((watch.key, self.clock.now))
        self.counts[watch.key] += 1
        error = self.behaviour(watch, self.counts[watch.key])
        if error:
            raise error
        if not dry_run:
            record.update({"retry_at": 0, "failures": 0, "last_error": None})

    def run_cycle(self, dry_run=False):
        return monitor.run_cycle(
            None, self.notifier, self.watches, self.state,
            self.settings, self.state_path, dry_run,
        )

    def test_all_eight_items_are_checked_before_retrying_first_403(self):
        first = self.watches[0].key
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("HTTP 403", blocked=True)
            if watch.key == first and attempt < 3 else None
        )
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual([key for key, _ in self.calls[:8]], [w.key for w in self.watches])
        self.assertEqual(self.counts[first], 3)
        self.assertTrue(all(self.counts[w.key] == 1 for w in self.watches[1:]))
        self.assertEqual(self.state["watches"][first]["retry_at"], 0)

    def test_slow_checks_retain_attempts_across_budget_limited_restarts(self):
        self.settings.update({"cycle_budget_seconds": 90, "retry_delay_seconds": 60,
                              "failure_cooldown_seconds": 3600})
        def slow_failure(watch, attempt):
            self.clock.now += 31
            return monitor.MonitorError("timeout")
        self.behaviour = slow_failure
        for _ in range(10):
            self.run_cycle()
            self.state = json.loads(self.state_path.read_text())
            if all(self.state["watches"].get(w.key, {}).get("failures") == 1 for w in self.watches):
                break
            self.clock.now += 60
        self.assertEqual([self.counts[w.key] for w in self.watches], [3] * 8)
        self.assertEqual(self.state["pending_checks"], [])
        for record in self.state["watches"].values():
            self.assertEqual(record["failures"], 1)
            self.assertNotIn("pending_attempts", record)

    def test_pending_attempt_count_survives_a_skipped_retry_run(self):
        self.watches = self.watches[:1]
        self.settings.update({"cycle_budget_seconds": 1, "retry_delay_seconds": 60})
        self.behaviour = lambda watch, attempt: monitor.MonitorError("timeout")
        self.run_cycle()
        record = self.state["watches"][self.watches[0].key]
        self.assertEqual(record["pending_attempts"], 1)
        self.state = json.loads(self.state_path.read_text())
        self.run_cycle()  # Short saved retry delay is still active.
        self.state = json.loads(self.state_path.read_text())
        self.clock.now += 60
        self.run_cycle()
        self.clock.now += 60
        self.run_cycle()
        self.assertEqual(self.counts[self.watches[0].key], 3)
        self.assertEqual(self.state["watches"][self.watches[0].key]["failures"], 1)

    def test_legacy_pending_attempts_resume_without_erasing_alert_history(self):
        watch = self.watches[0]
        self.state = {"pending_checks": [watch.key], "watches": {watch.key: {
            "last_attempt_count": 2, "last_error": "timeout", "announced": ["XS"],
        }}}
        self.behaviour = lambda w, attempt: monitor.MonitorError("timeout") if w == watch else None
        self.run_cycle()
        record = self.state["watches"][watch.key]
        self.assertEqual(self.counts[watch.key], 1)
        self.assertEqual(record["last_attempt_count"], 3)
        self.assertEqual(record["failures"], 1)
        self.assertEqual(record["announced"], ["XS"])

    def test_success_clears_resumed_attempt_count(self):
        watch = self.watches[0]
        self.state = {"watches": {watch.key: {
            "pending_attempts": 2, "last_error": "timeout", "retry_at": 0,
        }}}
        self.run_cycle()
        self.assertNotIn("pending_attempts", self.state["watches"][watch.key])
        self.behaviour = lambda w, attempt: monitor.MonitorError("timeout") if w == watch else None
        self.run_cycle()
        self.assertEqual(self.counts[watch.key], 4)  # One success, then three fresh attempts.

    def test_persistent_403_cools_only_failed_item_after_three_attempts(self):
        first = self.watches[0].key
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("HTTP 403", blocked=True) if watch.key == first else None
        )
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.counts[first], 3)
        self.assertEqual(self.state["watches"][first]["retry_at"], self.clock.now + 600)
        self.assertNotIn("hm_retry_at", self.state)
        self.assertNotIn("pause_until", self.state)
        self.assertEqual(json.loads(self.state_path.read_text()), self.state)

    def test_all_items_can_fail_without_starving_later_items(self):
        self.behaviour = lambda watch, attempt: monitor.MonitorError("HTTP 403", blocked=True)
        self.assertEqual(self.run_cycle(), 8)
        self.assertEqual(list(self.counts.values()), [3] * 8)
        expected_order = [w.key for w in self.watches] * 3
        self.assertEqual([key for key, _ in self.calls], expected_order)

    def test_retry_spacing_is_at_least_fifteen_seconds(self):
        first = self.watches[0].key
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("timeout") if watch.key == first and attempt < 3 else None
        )
        self.run_cycle()
        times = [when for key, when in self.calls if key == first]
        self.assertEqual(times, [2_000_000_000, 2_000_000_015, 2_000_000_030])

    def test_cooldown_survives_restart_and_does_not_skip_healthy_items(self):
        self.test_persistent_403_cools_only_failed_item_after_three_attempts()
        self.state = json.loads(self.state_path.read_text())
        self.calls.clear()
        self.counts.clear()
        self.clock.now += 300
        self.assertEqual(self.run_cycle(), 1)
        self.assertNotIn(self.watches[0].key, self.counts)
        self.assertEqual(len(self.counts), 7)
        self.clock.now += 300
        self.behaviour = lambda watch, attempt: None
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.counts[self.watches[0].key], 1)

    def test_long_item_retry_after_defers_only_that_item(self):
        first = self.watches[0].key
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("HTTP 403", retry_after=3600, blocked=True)
            if watch.key == first else None
        )
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(self.state["watches"][first]["retry_at"], self.clock.now + 3600)
        self.assertEqual(self.state["pending_checks"], [first])
        self.assertEqual(self.clock.waits, [])

    def test_429_waits_before_other_items_and_then_completes_all_checks(self):
        first = self.watches[0].key
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("HTTP 429", retry_after=75, blocked=True, rate_limited=True)
            if watch.key == first and attempt == 1 else None
        )
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(len(self.counts), 8)
        self.assertTrue(all(when >= 2_000_000_075 for _, when in self.calls[1:]))

    def test_long_429_is_persisted_and_unchecked_items_resume_first(self):
        first = self.watches[0].key
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("HTTP 429", retry_after=900, blocked=True, rate_limited=True)
            if watch.key == first and attempt == 1 else None
        )
        self.assertEqual(self.run_cycle(), 8)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.state["hm_retry_at"], self.clock.now + 900)
        self.assertEqual(self.state["pending_checks"][0], self.watches[1].key)
        self.state = json.loads(self.state_path.read_text())
        self.calls.clear()
        self.assertEqual(self.run_cycle(), 8)
        self.assertEqual(self.calls, [])
        self.clock.now += 900
        self.behaviour = lambda watch, attempt: None
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.calls[0][0], self.watches[1].key)

    def test_429_without_header_uses_sixty_second_wait(self):
        first = self.watches[0].key
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("HTTP 429", blocked=True, rate_limited=True)
            if watch.key == first and attempt == 1 else None
        )
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.clock.waits[0], 60)

    def test_old_403_pause_is_removed_without_erasing_notification_history(self):
        first = self.watches[0].key
        self.state = {
            "pause_until": self.clock.now + 3600,
            "watches": {first: {"announced": ["XS"], "last_error": "HTTP 403", "retry_at": 0}},
        }
        self.assertEqual(self.run_cycle(), 0)
        self.assertNotIn("pause_until", self.state)
        self.assertEqual(self.state["watches"][first]["announced"], ["XS"])

    def test_old_429_pause_is_preserved(self):
        self.state = {
            "pause_until": self.clock.now + 3600,
            "watches": {self.watches[0].key: {"last_error": "HTTP 429"}},
        }
        self.assertEqual(self.run_cycle(), 8)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state["hm_retry_at"], self.clock.now + 3600)

    def test_budget_preserves_unchecked_items_for_next_run(self):
        self.settings["cycle_budget_seconds"] = 30
        def slow_failure(watch, attempt):
            self.clock.now += 31
            return monitor.MonitorError("timeout")
        self.behaviour = slow_failure
        self.assertEqual(self.run_cycle(), 8)
        self.assertEqual(len(self.calls), 1)
        self.state = json.loads(self.state_path.read_text())
        self.calls.clear()
        self.clock.now += 300
        self.behaviour = lambda watch, attempt: None
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.calls[0][0], self.watches[1].key)

    def test_dry_run_checks_all_items_once_without_saving_or_sending(self):
        self.behaviour = lambda watch, attempt: monitor.MonitorError("HTTP 403", blocked=True)
        original = copy.deepcopy(self.state)
        self.assertEqual(self.run_cycle(dry_run=True), 8)
        self.assertEqual(list(self.counts.values()), [1] * 8)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.state, original)
        self.assertEqual(self.notifier.messages, [])

    def test_stop_does_not_claim_unchecked_items_succeeded(self):
        self.clock.stopped = True
        self.assertEqual(self.run_cycle(), 8)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.state["pending_checks"]), 8)

    def test_repeated_failures_back_off_across_restarts_and_success_resets_delay(self):
        self.settings.update({"retry_delay_seconds": 60, "failure_cooldown_seconds": 1800})
        first = self.watches[0].key
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("HTTP 403", blocked=True) if watch.key == first else None
        )
        for cycle, expected_delay in enumerate([1800, 3600, 3600], start=1):
            self.assertEqual(self.run_cycle(), 1)
            record = self.state["watches"][first]
            self.assertEqual(record["failures"], cycle)
            self.assertEqual(record["retry_at"], self.clock.now + expected_delay)
            self.assertEqual(self.counts[first], cycle * 3)
            self.assertTrue(all(self.counts[watch.key] == cycle for watch in self.watches[1:]))
            self.state = json.loads(self.state_path.read_text())
            self.clock.now = record["retry_at"]

        self.behaviour = lambda watch, attempt: None
        self.assertEqual(self.run_cycle(), 0)
        self.assertEqual(self.state["watches"][first]["failures"], 0)
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("HTTP 403", blocked=True) if watch.key == first else None
        )
        self.assertEqual(self.run_cycle(), 1)
        self.assertEqual(self.state["watches"][first]["retry_at"], self.clock.now + 1800)

    def test_new_retry_delay_spaces_attempts_one_minute_apart(self):
        self.settings["retry_delay_seconds"] = 60
        first = self.watches[0].key
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("HTTP 403", blocked=True)
            if watch.key == first and attempt < 3 else None
        )
        self.assertEqual(self.run_cycle(), 0)
        times = [when for key, when in self.calls if key == first]
        self.assertEqual(times, [2_000_000_000, 2_000_000_060, 2_000_000_120])

    def test_server_retry_after_can_exceed_local_cooldown_cap(self):
        record = {"failures": 10000}
        monitor.cool_down_watch(
            self.notifier, self.watches[0], record, self.settings,
            monitor.MonitorError("HTTP 403", retry_after=7200, blocked=True), 3,
        )
        self.assertEqual(record["retry_at"], self.clock.now + 7200)

    def test_summary_and_heartbeat_distinguish_skipped_items_from_new_failures(self):
        self.settings["heartbeat_hours"] = 24
        first, second = self.watches[:2]
        self.state["watches"][first.key] = {"retry_at": self.clock.now + 300}
        self.behaviour = lambda watch, attempt: (
            monitor.MonitorError("HTTP 403", blocked=True) if watch.key == second.key else None
        )
        with self.assertLogs(monitor.LOG, level="INFO") as logs:
            self.assertEqual(self.run_cycle(), 2)
        self.assertIn(
            "7/8 items attempted; 6 succeeded; 1 failed after all attempts; "
            "1 skipped for cooldown; 0 unfinished checks", "\n".join(logs.output),
        )
        self.assertIn("1 failed after all attempts; 1 skipped for cooldown", self.notifier.messages[0])


class TransportTests(unittest.TestCase):
    def test_http_200_denial_does_not_wait_for_missing_product_data(self):
        _, watches = monitor.load_config(monitor.BASE / "config.json")
        client = monitor.BrowserClient.__new__(monitor.BrowserClient)
        client.pending = {}
        client.last_navigation = 0
        client.page = Mock()
        client.page.url = watches[0].url
        client.page.goto.return_value = SimpleNamespace(status=200, headers={"retry-after": "120"})
        client.page.content.return_value = "<title>Access Denied</title><h1>Access Denied</h1>private-body"
        client.timeout_error = TimeoutError
        client.browser_error = OSError
        with self.assertRaises(monitor.MonitorError) as caught:
            client.metadata(watches[0])
        self.assertTrue(caught.exception.blocked)
        self.assertFalse(caught.exception.rate_limited)
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertNotIn("private", str(caught.exception))
        client.page.locator.assert_not_called()
        client.page.goto.assert_called_once()
        client.page.remove_listener.assert_called_once()
        self.assertEqual(client.pending, {})

    def test_denial_parser_ignores_script_text_comments_and_product_pages(self):
        cases = [
            ('<title>Product</title><script>"<h1>Access Denied</h1>"</script>', False),
            ('<!-- <h1>Access Denied</h1> -->', False),
            ('<h1> Access <span>Denied</span> </h1>', True),
            ('<h1>Access Denied</h1><script id="__NEXT_DATA__">{}</script>', False),
            ('<title>Loading...</title>', False),
        ]
        for html, expected in cases:
            with self.subTest(html=html):
                parser = monitor.AccessPageParser()
                parser.feed(html)
                self.assertEqual(parser.denied and not parser.has_product_data, expected)

    def test_product_script_timeout_reports_safe_page_markers(self):
        _, watches = monitor.load_config(monitor.BASE / "config.json")
        watch = watches[0]
        client = monitor.BrowserClient.__new__(monitor.BrowserClient)
        client.pending = {}
        client.last_navigation = 0
        client.page = Mock()
        client.page.url = watch.url
        client.page.goto.return_value = SimpleNamespace(status=200)
        client.page.locator.return_value.wait_for.side_effect = TimeoutError()
        client.page.content.return_value = "Verify you are human. private-body-and-cookie"
        client.timeout_error = TimeoutError
        client.browser_error = OSError
        with self.assertLogs(monitor.LOG, level="WARNING") as logs:
            with self.assertRaises(monitor.MonitorError):
                client.metadata(watch)
        output = "\n".join(logs.output)
        self.assertIn("stage=product data script; HTTP=200", output)
        self.assertIn("product-data marker=no; page hint=verification marker", output)
        self.assertNotIn("private", output)
        self.assertEqual(client.pending, {})
        client.page.goto.assert_called_once()
        client.page.remove_listener.assert_called_once()

    def test_navigation_timeout_survives_unreadable_dom(self):
        _, watches = monitor.load_config(monitor.BASE / "config.json")
        client = monitor.BrowserClient.__new__(monitor.BrowserClient)
        client.pending = {}
        client.last_navigation = 0
        client.page = Mock()
        client.page.goto.side_effect = TimeoutError()
        client.page.content.side_effect = OSError("private diagnostic")
        client.timeout_error = TimeoutError
        client.browser_error = OSError
        with self.assertLogs(monitor.LOG, level="WARNING") as logs:
            with self.assertRaises(monitor.MonitorError):
                client.metadata(watches[0])
        output = "\n".join(logs.output)
        self.assertIn("stage=navigation; HTTP=unavailable", output)
        self.assertIn("page hint=DOM unavailable", output)
        self.assertNotIn("private", output)

    def test_browser_product_403_and_429_have_different_retry_scope(self):
        _, watches = monitor.load_config(monitor.BASE / "config.json")
        for status in [403, 429, 503]:
            with self.subTest(status=status):
                client = monitor.BrowserClient.__new__(monitor.BrowserClient)
                client.pending = {}
                client.last_navigation = 0
                client.page = Mock()
                client.page.goto.return_value = SimpleNamespace(
                    status=status, headers={"retry-after": "120"}, text=lambda: "Access Denied"
                )
                client.timeout_error = TimeoutError
                client.browser_error = OSError
                with self.assertRaises(monitor.MonitorError) as caught:
                    client.metadata(watches[0])
                self.assertEqual(caught.exception.rate_limited, status == 429)
                self.assertEqual(caught.exception.retry_after, 120)
                client.page.remove_listener.assert_called_once()

    def test_browser_stock_429_also_sets_shared_rate_limit(self):
        _, watches = monitor.load_config(monitor.BASE / "config.json")
        watch = watches[0]
        client = monitor.BrowserClient.__new__(monitor.BrowserClient)
        client.pending = {}
        client.last_navigation = 0
        client.page = Mock()
        client.page.content.return_value = ""
        client.page.url = watch.url
        client.timeout_error = TimeoutError
        client.browser_error = OSError
        def navigate(*args, **kwargs):
            callback = client.page.on.call_args.args[1]
            callback(SimpleNamespace(
                url=watch.stock_url, status=429, headers={"retry-after": "120"},
                text=lambda: "Too many requests",
            ))
            return SimpleNamespace(status=200)
        client.page.goto.side_effect = navigate
        with patch.object(monitor, "parse_metadata", return_value={}):
            with self.assertRaises(monitor.MonitorError) as caught:
                client.metadata(watch)
        self.assertTrue(caught.exception.rate_limited)
        self.assertEqual(caught.exception.retry_after, 120)

    def test_only_hm_429_is_classified_as_a_shared_rate_limit(self):
        for url, status, expected in [
            ("https://www2.hm.com/product", 429, True),
            ("https://www2.hm.com/product", 403, False),
            ("https://api.telegram.org/test", 429, False),
        ]:
            with self.subTest(url=url, status=status):
                error = HTTPError(url, status, "refused", {"Retry-After": "90"}, io.BytesIO())
                with patch.object(monitor, "urlopen", side_effect=error):
                    with self.assertRaises(monitor.MonitorError) as caught:
                        monitor.Client(spacing=0).request(url)
                self.assertEqual(caught.exception.rate_limited, expected)
                self.assertEqual(caught.exception.retry_after, 90)

    def test_error_preserves_previous_stock_and_does_not_rearm_an_alert(self):
        settings, watches = monitor.load_config(monitor.BASE / "config.json")
        watch = watches[0]
        record = {"announced": ["XS"], "misses": {}, "last_available": ["XS"], "initialized": True}
        original = copy.deepcopy(record)
        client = Mock()
        client.metadata.side_effect = monitor.MonitorError("HTTP 403", blocked=True)
        notifier = Notifier()
        with self.assertRaises(monitor.MonitorError):
            monitor.check_watch(client, notifier, watch, record, settings, False)
        self.assertEqual(record, original)
        self.assertEqual(notifier.messages, [])

    def test_successful_retry_sends_one_stock_alert_and_preserves_deduplication(self):
        settings, watches = monitor.load_config(monitor.BASE / "config.json")
        watch = watches[0]
        sku = watch.article + "002"
        metadata = {"sizes": {"XS": sku}, "coming_soon": False, "fetched_at": 2_000_000_000,
                    "name": "Belt", "colour": "Black"}
        client = Mock()
        client.metadata.side_effect = [monitor.MonitorError("timeout"), metadata, metadata, metadata]
        client.stock.return_value = ({sku}, set())
        notifier = Notifier()
        record = {}
        with self.assertRaises(monitor.MonitorError):
            monitor.check_watch(client, notifier, watch, record, settings, False)
        monitor.check_watch(client, notifier, watch, record, settings, False)
        monitor.check_watch(client, notifier, watch, record, settings, False)
        self.assertEqual(len(notifier.messages), 1)
        self.assertEqual(record["announced"], ["XS"])
        self.assertEqual(record["failures"], 0)

    def test_refusal_diagnostics_do_not_log_response_html_or_cookies(self):
        _, watches = monitor.load_config(monitor.BASE / "config.json")
        client = monitor.BrowserClient.__new__(monitor.BrowserClient)
        client.browser_error = OSError
        response = SimpleNamespace(
            status=403,
            headers={"server": "Example-Edge", "set-cookie": "secret-cookie", "retry-after": "90"},
            text=lambda: "<h1>Access Denied</h1> secret-body",
        )
        with self.assertLogs(monitor.LOG, level="WARNING") as logs:
            client.log_refusal(response, watches[0])
        output = "\n".join(logs.output)
        self.assertIn("server=Example-Edge; type=access denied page; Retry-After=90s", output)
        self.assertNotIn("secret", output)
        response.text = Mock(side_effect=OSError("body unavailable"))
        with self.assertLogs(monitor.LOG, level="WARNING") as logs:
            client.log_refusal(response, watches[0])
        self.assertIn("type=response body unavailable", "\n".join(logs.output))

    def test_503_diagnostics_classify_without_exposing_response_content(self):
        _, watches = monitor.load_config(monitor.BASE / "config.json")
        client = monitor.BrowserClient.__new__(monitor.BrowserClient)
        client.browser_error = OSError
        response = SimpleNamespace(
            status=503,
            headers={"server": "Example-Edge", "set-cookie": "private-cookie", "retry-after": "120"},
            text=lambda: "<h1>Service Unavailable</h1> private-body",
        )
        with self.assertLogs(monitor.LOG, level="WARNING") as logs:
            client.log_refusal(response, watches[0])
        output = "\n".join(logs.output)
        self.assertIn("HTTP 503", output)
        self.assertIn("type=service unavailable page; Retry-After=120s", output)
        self.assertNotIn("private", output)

    def test_original_config_gets_retry_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads((monitor.BASE / "config.json").read_text())
            config["settings"] = {"interval_seconds": 300}
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config))
            settings, watches = monitor.load_config(path)
        self.assertEqual(settings["attempts_per_item"], 3)
        self.assertEqual(settings["retry_delay_seconds"], 60)
        self.assertEqual(settings["failure_cooldown_seconds"], 1800)
        self.assertEqual(settings["max_failure_cooldown_seconds"], 3600)
        self.assertEqual(len(watches), 8)

    def test_existing_long_cooldown_is_preserved_and_inconsistent_cap_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads((monitor.BASE / "config.json").read_text())
            config["settings"] = {"failure_cooldown_seconds": 7200}
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config))
            settings, _ = monitor.load_config(path)
            self.assertEqual(settings["max_failure_cooldown_seconds"], 7200)
            config["settings"]["max_failure_cooldown_seconds"] = 3600
            path.write_text(json.dumps(config))
            with self.assertRaises(monitor.MonitorError):
                monitor.load_config(path)

    def test_invalid_retry_settings_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = json.loads((monitor.BASE / "config.json").read_text())
            for value in [0, 6, True, 2.5, "3"]:
                with self.subTest(value=value):
                    config["settings"]["attempts_per_item"] = value
                    path.write_text(json.dumps(config))
                    with self.assertRaises(monitor.MonitorError):
                        monitor.load_config(path)


if __name__ == "__main__":
    unittest.main()
