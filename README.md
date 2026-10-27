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
| `retry_delay_seconds` | 15 | Minimum delay before retrying the same item |
| `failure_cooldown_seconds` | 600 | Item-only cooldown after all attempts fail |
| `rate_limit_delay_seconds` | 60 | Minimum shared wait after an HTTP 429 |
| `cycle_budget_seconds` | 300 | Stop starting new checks after this much cycle time |

A single HTTP 403 no longer cancels the batch or pauses every product for an
hour. Failed checks remain **unknown**, never “out of stock.” Successful items
are not retried in the same cycle. Existing stock-alert deduplication is kept.

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

## Offline verification

Run from the repository root:

```sh
python -m unittest discover -s tests -v
```

Tests use simulated time, HTTP failures and notification recipients. They do
not contact H&M or send Telegram messages. A successful test run verifies the
retry and state logic; it does not guarantee that H&M will accept live requests.
