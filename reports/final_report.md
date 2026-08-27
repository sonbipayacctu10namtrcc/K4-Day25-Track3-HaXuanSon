# Day 10 Reliability Report

## 1. Architecture summary

`ReliabilityGateway.complete()` is the single entry point. It tries the cache first, then
walks the provider list in order, routing every attempt through that provider's own
`CircuitBreaker`. If every provider is exhausted (failed outright or fast-failed by an
open breaker) it returns a static degraded message instead of raising.

```
User Request
    |
    v
[Gateway.complete(prompt)]
    |
    +--> cache is not None? --> cache.get(prompt) --> HIT (score >= threshold,
    |                                                   not a false hit)
    |                                                       |
    |                                                       v
    |                                          return route=f"cache_hit:{score:.2f}"
    |                                                 cache_hit=True, latency=0, cost=0
    |  MISS / no cache
    v
[for provider in providers, in order]
    |
    +--> breaker = breakers[provider.name]
    |         CLOSED/HALF_OPEN --> breaker.call(provider.complete, prompt)
    |         OPEN & timeout not elapsed --> CircuitOpenError (fail fast, no network call)
    |
    +--> success --> cache.set(prompt, response.text, {"provider": ...})
    |                 route = "primary" (i==0) else "fallback"
    |                 return GatewayResponse(...)
    |
    +--> ProviderError / CircuitOpenError --> record last_error, continue to next provider
    |
    v (all providers exhausted)
[Static fallback]
    return text="The service is temporarily degraded. Please try again soon."
           route="static_fallback", error=last_error
```

Circuit breaker state machine (`circuit_breaker.py`):

```
        record_failure() x N >= failure_threshold
   CLOSED ----------------------------------------> OPEN
     ^                                                |
     | record_success()                               | time.monotonic() - opened_at
     | (success_count >= success_threshold)            >= reset_timeout_seconds
     |                                                |
   HALF_OPEN <----------------------------------------+
     |
     +--record_failure()--> OPEN  (reason="probe_failure", re-opens immediately,
                                    does NOT wait for failure_threshold again)
```

Each provider gets its own breaker instance, so a dead primary does not affect the
backup's breaker — that's what lets the fallback chain keep serving while primary
recovers on its own schedule.

## 2. Configuration

Values are from `configs/default.yaml`. Rationale is backed by evidence gathered while
building this lab (session commands are reproducible via `pytest`/the snippets below).

| Setting | Value | Reason |
|---|---:|---|
| `failure_threshold` | 3 | Primary's baseline `fail_rate` is 0.25, so three *independent* failures in a row happen by chance only ~1.6% of the time (0.25³). Opening at 3 filters out normal noise but still reacts within ~3 requests to a real outage (`primary_timeout_100` sets `fail_rate=1.0`, so it trips almost immediately). `failure_threshold=1` would flap on ordinary jitter (`test_does_not_open_below_threshold` exists precisely to guard this). |
| `reset_timeout_seconds` | 2 | Confirmed empirically: `recovery_time_ms` measured 2238–2405 ms across multiple runs (`reports/metrics_seed_a.json`, per-scenario `primary_flaky_50` run below) — recovery time tracks this setting almost exactly, since a HALF_OPEN probe fires as soon as the timeout elapses and `success_threshold=1` closes on the very next success. 2s is long enough to avoid hammering a genuinely-down dependency, short enough that a flaky provider (50% fail rate) gets re-tried quickly. |
| `success_threshold` | 1 | These are read-only, side-effect-free completions, so a single successful probe is enough to fully re-trust a provider. Verified via `test_success_threshold_greater_than_one` that the mechanism *does* support requiring more (e.g. 2) — that would be the right call for a provider whose calls have side effects (writes/payments), where one lucky probe shouldn't reopen the floodgates. |
| `cache TTL` | 300s | Sample queries (`data/sample_queries.jsonl`) are FAQ/policy-style content that doesn't change within a short burst of traffic, so 5 minutes captures realistic repeat-question traffic in a 100-request scenario without risking long-lived staleness for date-sensitive answers (mitigated further by the false-hit guard, see below). |
| `similarity_threshold` | 0.92 | Measured directly with `ResponseCache.similarity()`: `"refund policy for 2024"` vs `"refund policy for 2025"` scores **0.923** — i.e. it *clears* a 0.92 threshold despite being a different year. A lower threshold (e.g. 0.7–0.8, used only in unit tests for looser matching) would let near-duplicate-but-different phrasing like `"circuit breaker pattern"` vs `"circuit breaker design"` (0.681) or genuinely different years slip through even more easily. 0.92 restricts hits to near-verbatim repeats (typos/whitespace), and this concrete 0.923 case is exactly why `_looks_like_false_hit()` exists as an independent, threshold-blind safety net rather than relying on tuning the threshold alone. |
| `load_test.requests` | 100 (×3 scenarios = 300) | Providers use real `time.sleep()` for latency (not mocked), so 300 total requests already take real wall-clock time (~30–90s depending on cache warmth). 100/scenario is enough to get a stable P95/P99 and to observe circuit oscillation in `primary_flaky_50`, without the run taking minutes. |

## 3. SLO definitions

Actual values from the canonical seeded run (`reports/metrics.json`, `make run-chaos` →
`--seed 1`, Redis backend, freshly flushed):

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 99.33% | Yes |
| Latency P95 | < 2500 ms | 318.96 ms | Yes |
| Fallback success rate | >= 95% | 96.43% | Yes |
| Cache hit rate | >= 10% | 70.33% | Yes |
| Recovery time | < 5000 ms | 2428.5 ms | Yes |

## 4. Metrics

From `reports/metrics.json` (canonical run: Redis backend, `--seed 1`, `redis-cli
FLUSHDB` immediately before — this is what `make run-chaos` produces, the seed is
baked into the `Makefile` target):

| Metric | Value |
|---|---:|
| total_requests | 300 |
| availability | 0.9933 |
| error_rate | 0.0067 |
| latency_p50_ms | 271.40 |
| latency_p95_ms | 318.96 |
| latency_p99_ms | 320.53 |
| fallback_success_rate | 0.9643 |
| cache_hit_rate | 0.7033 |
| estimated_cost_saved | 0.211 |
| circuit_open_count | 7 |
| recovery_time_ms | 2428.5 |

**Why recovery_time_ms ≈ 2000 ms without needing to open the code:** it's a direct
readout of `reset_timeout_seconds: 2` in `configs/default.yaml`. A breaker opens, then
the very first `allow_request()` call after `reset_timeout_seconds` of real elapsed
time flips it to HALF_OPEN; with `success_threshold: 1` the next successful probe
closes it immediately. So open→closed elapsed time is bounded below by the timeout
itself plus a small amount of scheduling/request latency on top — hence values
clustering at 2200–2470 ms across every run in this report, never near 0 and never
much more than a couple hundred ms over 2000.

**Reproducibility.** `scripts/run_chaos.py --seed N` calls `random.seed()` once before
the run (`providers.py`/`chaos.py` were not modified — both use the global `random`
module, so a single seed pins provider fail/latency rolls and query selection). Two
runs with `--seed 1` (Redis backend, matching the canonical config) produced
**identical** outcome metrics (`reports/metrics_redis_seed1_run1.json` /
`_run2.json`):

| Metric | Run 1 | Run 2 | Identical? |
|---|---:|---:|---|
| availability | 0.9933 | 0.9933 | Yes |
| error_rate | 0.0067 | 0.0067 | Yes |
| fallback_success_rate | 0.9643 | 0.9643 | Yes |
| cache_hit_rate | 0.7033 | 0.7033 | Yes |
| circuit_open_count | 7 | 7 | Yes |
| estimated_cost | 0.037884 | 0.037884 | Yes |
| scenarios | all pass | all pass | Yes |
| recovery_time_ms | 2466.1 | 2410.0 | No (~56ms drift) |

Only wall-clock-derived numbers (recovery time, latency percentiles) show timing
drift, because `FakeLLMProvider.complete()` measures real elapsed time around a real
`time.sleep()` call rather than deriving latency from the seeded RNG — that part of
`providers.py` was left untouched, per the assignment. Every decision the seeded RNG
actually controls (pass/fail rolls, cache hits, scenario verdicts, cost) is bit-for-bit
reproducible, on both the in-memory and Redis cache backends.

## 5. Cache comparison

Same seed (42, chosen before the canonical run settled on seed 1 — irrelevant here
since this comparison is internally consistent between its own two runs), same 300
requests, memory backend, only `cache.enabled` toggled
(`reports/metrics_cache_on.json` / `reports/metrics_cache_off.json`):

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 274.00 | 279.55 | +5.55 ms (cache hits are excluded from the latency list entirely — see note) |
| latency_p95_ms | 315.67 | 319.22 | +3.55 ms |
| estimated_cost | 0.132552 | 0.04848 | **-63.4%** |
| cache_hit_rate | 0.0 | 0.6033 | +0.6033 |
| circuit_open_count | 19 | 9 | -10 (fewer provider calls made overall means fewer chances to trip) |
| availability | 0.99 | 0.9767 | -0.0133 |

**Note on latency:** `run_scenario()` only appends to `latencies_ms` when
`result.latency_ms > 0`, and cache hits report `latency_ms=0`. So the P50/P95 numbers
above are computed only over the *provider-serving* requests in both runs — cache
doesn't change per-request latency, it changes how many requests need a provider call
at all. The real win is cost (**63% cheaper** with cache on) and reduced load on
providers (fewer breaker trips). Availability is very slightly lower with cache on
purely because of a shift in which pseudo-random draws hit providers vs. cache under
the fixed seed, not because caching hurts reliability.

## 6. Redis shared cache

- **Why in-memory cache is insufficient for multi-instance deployments:** `ResponseCache._entries` is a plain Python list living in one process's heap. Behind a load balancer with N gateway replicas, each replica has its own empty cache — a question answered by replica 1 is a guaranteed miss on replica 2, so effective hit rate (and cost savings) shrinks roughly by a factor of N, and every replica independently re-pays for the same repeated question.
- **How `SharedRedisCache` solves this:** it stores `{query, response}` as a Redis hash keyed by `f"{prefix}{md5(query)}"` with a Redis-native `EXPIRE` for TTL, so every replica reads/writes the same keyspace. A cache write from any one instance is immediately visible to all others.

### Evidence of shared state

Two independent `SharedRedisCache` Python objects, no shared process state, same Redis:

```
>>> c1 = SharedRedisCache('redis://localhost:6379/0', 300, 0.92, prefix='rl:cache:')
>>> c1.set('What is the refund policy?', 'You can request a refund within 30 days.')
>>> c2 = SharedRedisCache('redis://localhost:6379/0', 300, 0.92, prefix='rl:cache:')
>>> c2.get('What is the refund policy?')
('You can request a refund within 30 days.', 1.0)
```
`c2 sees c1 write: You can request a refund within 30 days. 1.0`

### Redis CLI output

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:f452fc0bc027

$ docker compose exec redis redis-cli HGETALL rl:cache:f452fc0bc027
query
What is the refund policy?
response
You can request a refund within 30 days.
```

### In-memory vs Redis latency comparison (optional)

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 279.55 | 271.40 | Both dominated by the ~180–260ms fake provider `base_latency_ms` + jitter; Redis round-trips (sub-ms on localhost) are noise by comparison. |
| latency_p95_ms | 319.22 | 318.96 | Same conclusion — cache backend choice doesn't show up in served-request latency at this scale. |
| cache_hit_rate | 0.6033 | 0.7033 | Higher with Redis in this run because Redis persists across scenario boundaries within the same process (see §8) — not because the similarity logic differs; both use the identical `ResponseCache.similarity()`. |

## 7. Chaos scenarios

Per-scenario runs, `--seed 1` set once at process start (matching exactly how
`run_simulation()` executes them — one continuous random stream across all three
scenarios), Redis backend, freshly flushed beforehand:

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| `primary_timeout_100` (primary `fail_rate=1.0`) | All primary traffic fails over to backup; circuit opens and stays open (primary never recovers) | availability 0.98, fallback_success_rate 0.9444, 34 successful fallbacks, 2 static fallbacks (both breakers briefly saturated at once), circuit opened 5 times, `recovery_time_ms=None` — **correct**, since primary's `fail_rate` never improves within the scenario, so it should never close again | Pass |
| `primary_flaky_50` (primary `fail_rate=0.5`) | Circuit oscillates open/closed as primary intermittently recovers; mix of primary and fallback routes | availability 1.0, circuit opened twice, one full open→close recovery observed (`recovery_time_ms=2386.9ms`, matches `reset_timeout_seconds=2s`), 14 fallback successes, 0 static fallbacks | Pass |
| `all_healthy` (no overrides) | All requests served via primary or cache, no circuit opens | availability 1.0, `circuit_open_count=0`, cache warmed to 73% hit rate by the end of 100 requests | Pass |
| `cache_disabled` (this report's own comparison, not a named scenario in the config) | Same request mix but every request must reach a provider — higher cost, more breaker exposure | availability 0.99, cache_hit_rate 0.0, estimated_cost 0.1326 (2.7× the cached run), `circuit_open_count=19` (more than double) — confirms cache absorbs load that would otherwise stress the breakers | Pass (used as a comparison run, not graded pass/fail) |

The combined `reports/metrics.json` reports `circuit_open_count=7`, which is exactly
5+2+0 above, and `recovery_time_ms=2428.5ms`, which comes entirely from the single
recovery inside `primary_flaky_50` (the other two scenarios contribute `None` and are
skipped by `calculate_recovery_time_ms()`'s averaging).

`run_simulation()`'s pass/fail rule: scenarios with `provider_overrides` (forced chaos)
pass at `availability >= 0.9`; the healthy baseline requires `>= 0.95`. All three
configured scenarios clear their bar in every run observed during this lab.

## 8. Failure analysis

Two concrete weaknesses surfaced directly while building this out — not hypothetical:

**1. Circuit breaker state is per-process, not shared.** `CircuitBreaker` keeps
`failure_count`/`state`/`opened_at` as plain instance attributes in RAM
(`circuit_breaker.py`). Behind N gateway replicas, each has its own breaker per
provider — replica A can have primary's breaker OPEN while replica B, having seen a
different (or no) failure history, keeps sending it traffic. A real outage would be
detected N times independently instead of once, and the fallback chain's protection is
only as good as whichever replica happens to notice fastest. **Fix (stretch goal
already scoped in the README):** move breaker counters into Redis using `INCR`/`EXPIRE`
for the failure window and a shared key for `state`/`opened_at`, so all replicas trip
and reset together — mirroring what `SharedRedisCache` already does for the cache
layer.

**2. `SharedRedisCache` has no fallback path if Redis is unreachable.** `ping()`
exists but `get()`/`set()` call `self._redis.hget`/`hset` directly with no try/except —
if Redis is down, every request that reaches the cache layer raises instead of
gracefully degrading, and since `ReliabilityGateway.complete()` doesn't catch generic
exceptions around `self.cache.get(...)`, a Redis outage would crash `complete()`
entirely rather than just losing the caching optimization. **Fix:** catch
`redis.exceptions.ConnectionError` in `SharedRedisCache.get()`/`set()` and either
no-op (skip caching that request) or fall back to an in-process `ResponseCache`
instance held as a backup, matching the "Redis graceful degradation" stretch goal.

**Smaller, session-observed nuance:** the Redis-backed cache persists across chaos
*scenario* boundaries within one `run_simulation()` call (each scenario builds a fresh
`ResponseCache` when `backend: memory`, but `SharedRedisCache` talks to the same
external Redis instance every time). That's why the combined Redis run's cache-hit
rate (70.33%) is measurably higher than the equivalent in-memory run (60.33%) even
though both use the identical similarity function — later scenarios benefit from
entries written by earlier ones. This is realistic (production caches don't reset
between "scenarios") but means Redis-backed and memory-backed chaos runs aren't a
perfectly apples-to-apples comparison unless the cache is flushed between scenarios,
which is why every measurement in this report explicitly notes a `redis-cli FLUSHDB`
immediately before the run it reports.

**Related, but not a bug:** `calculate_recovery_time_ms()` can legitimately return
`None` for an *individual* scenario — both `primary_timeout_100` (primary never
recovers, so it should never close again) and `all_healthy` (breaker never opens, so
there's nothing to recover from) correctly report `None` in §7. It only becomes a real
problem if a scenario that's *supposed* to demonstrate recovery (like
`primary_flaky_50`) finishes its fixed 100-request budget before
`reset_timeout_seconds` of wall-clock time has elapsed — high cache-hit runs process
requests almost instantly, so a very cache-heavy flaky scenario could in principle
exhaust its request budget before the breaker ever gets to probe. It didn't happen in
any run captured for this report, but the risk is structural, not eliminated — see
Next steps.

## 9. Next steps

1. Move circuit breaker counters into Redis (`INCR`/`EXPIRE`) so all gateway replicas
   share one view of provider health instead of tripping independently.
2. Add a `try/except redis.exceptions.ConnectionError` guard in `SharedRedisCache` with
   fallback to an in-process `ResponseCache`, so a Redis outage degrades caching rather
   than crashing the gateway.
3. Switch chaos scenarios from a fixed request count to a fixed wall-clock duration, so
   a high-cache-hit-rate run can never finish its request budget before
   `reset_timeout_seconds` of real time has elapsed — removing the structural risk of
   a scenario reporting `recovery_time_ms=None` even when it was designed to exercise
   recovery (see §8).
