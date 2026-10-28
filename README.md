# H&M stock monitor

Checks configured product/country combinations and sends Telegram stock alerts.

## Scheduling and retries

The current GitHub workflow uses `workflow_dispatch`. An external scheduler
starts it roughly every five minutes; there is no GitHub `schedule` trigger.
`--once` means one complete check cycle, including its bounded retries.
`interval_seconds` controls the loop only when the monitor runs without `--once`.

Each cycle checks all eligible items before retrying failures. The defaults are:

| Setting | Default | Meaning |
| --- | --- | --- |
| `attempts_per_item` | 3 | Initial check plus up to two retries per cycle |
| `retry_delay_seconds` | 60 | Minimum delay before retrying the same item |
| `failure_cooldown_seconds` | 1800 | Initial item-only cooldown after all attempts fail |
| `max_failure_cooldown_seconds` | 3600 | Maximum local cooldown after consecutive failed cycles |
| `rate_limit_delay_seconds` | 60 | Minimum shared wait after an HTTP 429 |
| `cycle_budget_seconds` | 300 | Stop starting new checks after this much cycle time |

A single HTTP 403 no longer cancels the batch or pauses every product for an
hour. Failed checks remain **unknown**, never “out of stock.” Successful items
are not retried in the same cycle. Existing stock-alert deduplication is kept.

After three failed attempts, that item waits 30 minutes. Consecutive failed
cycles double its cooldown, capped at 60 minutes by default. A successful check
resets the failure count. These pauses reduce repeated requests; they do not
guarantee H&M will lift an access block. Existing saved deadlines remain in
effect on upgrade; the longer cooldown applies when the next batch of attempts
fails. With a five-minute scheduler, a check runs at the next dispatch after
its deadline, so the actual gap can be a few minutes longer.

Cycle summaries report successful checks, exhausted failures, skipped cooldowns
and unfinished checks separately. A run still exits unsuccessfully when some
stock is unknown; a skipped item is never reported as a successful stock check.
For HTTP 403/429, browser logs include a limited response classification and
server name to help diagnose the block. Cookies and response HTML are not logged.

Server `Retry-After` delays take precedence over shorter local delays. An HTTP
429 pauses all H&M requests because it explicitly signals a rate limit. If the
wait exceeds the remaining cycle budget, it is saved for a later run. Items in
cooldown are skipped until due. Unfinished checks are saved and prioritised on
the next run, with unattempted items first. A check already in progress may
finish after the cycle budget; the workflow retains its ten-minute job limit.

On upgrade, the old global 403 pause is removed automatically while existing
item retry deadlines, stock data and sent-alert history remain. An old 429
pause is preserved. Do not delete the `monitor-state` branch or its state file.

The defaults also work with the previous `config.json`; its item list does not
need to be replaced to install the retry fix. Explicit settings are included
in this repository to make later adjustments straightforward.

## Browser compatibility test

The GitHub runner image rollout alternated Chrome 155.0.8059.39 and 156.0.8078.47.
Recent successful checks used 155; recent blocked checks used 156. An earlier
155 run was also refused, so the browser version is a suspected factor, not a
proven cause or a guarantee of access.

The workflow temporarily pins `HM_CHROME_VERSION` to 155.0.8059.39 to test this
variable consistently. It uses the installed Chrome when its version matches;
otherwise it downloads that exact build from Google's Chrome for Testing
archive. It verifies the version before starting checks and does not silently
fall back to a different version. Existing cooldowns and notification history
remain in effect. Review this temporary pin after testing; browser security
updates should not remain deferred indefinitely.

On Ubuntu runners, the downloaded build uses the installed Chrome's root-owned,
setuid sandbox helper via `CHROME_DEVEL_SANDBOX`. This follows Chromium's
[documented setup](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md)
and fixes the observed “No usable sandbox” startup failure. The workflow stops
if the helper is missing or invalid; Chrome's sandbox remains enabled.

## Offline verification

Run from the repository root:

```sh
python -m unittest discover -s tests -v
```

Tests use simulated time, HTTP failures and notification recipients. They do
not contact H&M or send Telegram messages. A successful test run verifies the
retry and state logic; it does not guarantee that H&M will accept live requests.
