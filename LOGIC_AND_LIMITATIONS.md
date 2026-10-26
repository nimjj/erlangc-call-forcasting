# Logic, simplified maths, and limitations

How the forecast and the staffing numbers are produced, in plain language, what accuracy to expect, and where the method is weak. For the architecture, code layout and endpoints, see [ARCHITECTURE.md](ARCHITECTURE.md). For setup, see [README.md](README.md).

Contents: [1. The short version](#1-the-short-version) · [2. From call records to a forecast](#2-from-call-records-to-a-forecast) · [3. From calls to agents](#3-from-calls-to-agents-erlang-c-simplified) · [4. From half-hours to shifts](#4-from-half-hours-to-shifts) · [5. How accurate is it](#5-how-accurate-is-it) · [6. Limitations](#6-limitations) · [7. Checking it on your own data](#7-checking-it-on-your-own-data) · [8. Glossary](#8-glossary)

## 1. The short version

1. The service reads past call records, counts answered calls per half-hour, and predicts how many calls will arrive in each half-hour of a future period.
2. For each half-hour it works out how many agents must be on the phones to answer calls fast enough (by default, 80% within 20 seconds), then adds a margin for breaks and absence (30%).
3. It groups half-hours into three 8-hour shifts. A shift must have enough agents for its **busiest** half-hour, because the agents stay for the whole shift.
4. Because the busiest half-hour is almost always busier than the forecast average, shifts are sized with a simulation, so that about 80% of shifts end up fully covered. Night shifts and weekend day shifts get a minimum of 3 agents.

What to expect, measured on a test with the true answer known (section 5):

| | Result |
|---|---|
| Daily call total | typically off by about 21% per day; a month's total by about 5-6% |
| Half-hour call counts | off by about 78% per half-hour (counts are tiny, so this is mostly chance) |
| Shifts fully covered | 93%, up from 55% when staffing from the plain forecast |
| Price of that cover | about 42% more agent-shifts than the real calls strictly needed |

The call forecast is already close to the best any forecast can do on a queue this small (section 5.2), so the staffing method, not the forecast, is what moves the result.

## 2. From call records to a forecast

### 2.1 What goes in

Only **answered** calls with a talk time between 1 second and 4 hours are used. Abandoned, unanswered and timed-out calls are dropped. Calls are counted per interval (15, 30 or 60 minutes; the dashboard and tests use 30). Average handling time (AHT) is learned per time-of-day slot from the same history. Missing dates are treated as "no calls", not "unknown", so uploads should cover the whole period. Details of the accepted file formats are in [ARCHITECTURE.md](ARCHITECTURE.md#6-data-formats).

### 2.2 Which model runs

| History available | Model | `decomposition_summary.model_variant` |
|---|---|---|
| Less than 14 days | Rolling profile: average the matching earlier days | (method is `ROLLING_PROFILE`) |
| 14 to 364 days | Weekly STL on every half-hour: trend plus a repeating weekly pattern | `interval_stl_weekly` |
| 365 days or more (default) | Daily model with a yearly curve and US holidays | `daily_stl_annual` |

The response always says which one ran. `annual_seasonality=off` forces the weekly model.

### 2.3 The yearly model in plain English

It works on **daily** totals and multiplies a few effects together:

```text
expected calls on a day  =  level  ×  weekday factor  ×  time-of-year factor  ×  holiday factor
```

- **Weekday factor.** Mondays to Fridays are busy, weekends are very quiet. It is learned with STL (a robust method that splits a series into trend, repeating pattern and leftovers), after replacing holidays and unusual days with typical values so they do not distort the pattern.
- **Time-of-year factor.** A smooth yearly curve (three waves with two or more years of history, two with less), fitted together with a trend that may bend once per year. Fitting both together stops a quiet season from being mistaken for a decline.
- **Level and trend.** The recent level, with a slope that fades quickly (it loses 40% of its strength each week, so it flattens within about a month instead of running away over a year). The level is then scaled so the model reproduces the call total of the last 28 days.
- **Holiday factor.** US federal holidays, plus Thanksgiving eve and the day after, Christmas Eve and New Year's Eve. Each holiday's effect is learned from earlier years and blended with a default for its type (closed days about 0.15, reduced days 0.6, the day before Thanksgiving 1.6).
- **Splitting the day.** The daily total is divided across half-hours using that weekday's typical shape over the last 26 weeks, and rounded to whole calls that add up to the daily total.

One-off events (an outage, a storm) are ignored when learning the pattern and are not predicted.

The weekly model (14 to 364 days) is simpler: fit trend plus weekly pattern to every half-hour, draw a straight line through the recent trend, and repeat the last week's pattern forward. The rolling-profile fallback averages earlier days chosen by weekday and position in the month.

### 2.4 Which period gets forecast

With the default `forecast_days=365`, the period depends on how much history there is:

| History | Forecast period |
|---|---|
| 1-6 days | next day |
| 7-27 days | next 7 days |
| 28 days to under 12 months | next full calendar month |
| 12 months or more | next full calendar year (starting 1 January) |

Any other `forecast_days` forecasts that many days starting right after the last record.

## 3. From calls to agents (Erlang C, simplified)

Erlang C is the standard way to size a call queue. It answers: "if calls arrive at random at this average rate and each takes about this long, how many agents do I need so that most callers are answered quickly?"

**Step 1: how much work arrives (traffic).**

```text
traffic (Erlangs) = calls in the interval × average handling time ÷ interval length
```

Example: 5 calls in 30 minutes, each about 11 minutes (670 s): 5 × 670 ÷ 1800 = 1.86 Erlangs. In plain words, that is 1.86 agents' worth of talking packed into the half-hour.

**Step 2: agents on the phones.** Find the smallest number of agents for which the target is met (default: 80% of calls answered within 20 seconds). You always need more agents than the traffic, because calls arrive at random and sometimes queue. For 1.86 Erlangs, 4 agents give about 87%, 3 would not meet the target.

**Step 3: shrinkage.** Agents on the roster are not all available (breaks, training, absence). With 30% shrinkage, divide by 0.7 and round up: 4 ÷ 0.7 = 5.7, so **6 agents rostered**.

With a 670 s call and the default targets, the staircase from calls in a half-hour to agents rostered is:

| Calls in the half-hour | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|
| Traffic (Erlangs) | 0.37 | 0.74 | 1.12 | 1.49 | 1.86 | 2.23 | 2.61 |
| Agents on the phones | 2 | 2 | 3 | 4 | 4 | 5 | 5 |
| Agents rostered | 3 | 3 | 5 | 6 | 6 | 8 | 8 |

**Why one call needs 3 agents.** "1 call" is treated as an *average* of 1 call per half-hour arriving at a random moment, not as exactly one call. With calls of 11 minutes there is a 37% chance a second call arrives while the first is still being handled. One agent would answer only about 63% of callers within 20 seconds, short of 80%. Two agents reach about 94%. Shrinkage then turns 2 into 3 (2 ÷ 0.7 = 2.9). This is the formula applied to a number too small for it to be a good fit; see limitation L4.

A half-hour with no calls needs no agents.

## 4. From half-hours to shifts

The roster has three 8-hour shifts per day: night 00:00-08:00, morning 08:00-16:00 and evening 16:00-24:00 (the start times can be changed). Agents work a whole shift, so a shift needs **the agents for its busiest half-hour**.

**Why the plain forecast under-covers.** The forecast gives the *average* number of calls per half-hour, often 1-2 in a small queue. Real calls are lumpy: a half-hour that averages 2 might see 0, 0, 4, 1, 5. The busiest half-hour of a shift is the maximum of several random counts, which is usually well above the maximum of their averages. In the test, the busiest half-hour of a morning averaged about twice the forecast average. Staffing to the plain forecast covered only 55% of shifts.

**What the service does instead.** For every shift of every day:

1. Simulate the shift 300 times. Call counts are random (Poisson) around the forecast, with a small day-level variation (about 15%) for forecast error.
2. Convert every simulated half-hour to agents with the Erlang C steps above.
3. Take the busiest half-hour of each simulated shift.
4. Use the value that 80% of simulated shifts do not exceed (`coverage_percent`, default 80).

The random seed is fixed, so the same forecast always gives the same schedule.

**Minimums.** Night shifts and weekend day shifts are raised to at least 3 agents (`night_minimum_agents`, `weekend_minimum_agents`). These shifts often have no calls, but when one arrives it still needs 3 agents, and no forecast can say which nights those will be.

**Headcount and roster.** The number of people needed is estimated from the weekly total of agent-shifts (each person works at most 5 days a week) and the busiest day. The roster is then built greedily: one shift per person per day, at least 8 hours between shifts, and the rest as days off. The greedy step does not guarantee every shift is filled, so always check `coverage_ok` and `coverage_shortage` in the response.

## 5. How accurate is it

### 5.1 How it was tested

Real data covered one year. To test a forecast for a full year, two earlier years were *synthesised* from that real year (`tools/synthesize_queue_years.py`: same shape, different level, own holidays, random noise). The forecast was trained only on the two synthetic years and then scored against the real year. Two further checks were used: 12 rolling monthly tests (each month predicted from data before it, which is closest to real use), and a second test on the synthetic years where the true answer is known. All figures below are percentages, to protect the underlying data.

### 5.2 Call forecast

| Measure | Result | How to read it |
|---|---|---|
| Daily error (WAPE) | 20.7% | Total of the daily misses divided by total calls. The usual miss on a day is about a fifth of the day's calls. |
| Total bias | -1.5% | Slightly low overall. Negative means too few calls forecast. |
| Weekly error | 9.1% | Misses shrink as days are added together. |
| Monthly error | 5.5% average, 8.6% worst | All 12 months within 10% of the real total. |
| Weekdays / weekends | 18.9% / 65.6% | Weekends are only about 4% of calls, so their large percentage matters little. |
| Normal / holiday / one-off event days | 20.6% / 16.4% / 31.8% | Holidays are handled well. One-off events are not predictable (the four event days were 32% too low). |
| Half-hour error | 77.5% | At 1-3 calls per half-hour, one call more or less is a 30-100% miss. This is mostly chance, not a modelling fault. |
| Rolling-month tests | 21.9% daily | Predicting each month from earlier data only. |
| Second synthetic test | 24.5% daily | A different year pair with a known true answer. |

**The noise floor.** Even a forecast that knew the true average exactly would be wrong on any given day, because the number of calls is random. In this queue that alone gives a daily error of about 16% if calls were perfectly random, and about 21% with the extra day-to-day variation actually seen. The model's 20.7% is therefore near the best achievable, so further tuning of the call forecast cannot cut the error by anything like 10%.

Tuning tried and rejected (no gain or worse on the rolling tests): per-weekday level correction, a day-of-month effect, blending in last year's same day, and longer trend windows.

### 5.3 Shift staffing

This is what the staffing decision is judged on. A shift is *covered* when the agents scheduled are at least the agents the real calls required in that shift (same Erlang C rule). Shifts with no real calls are left out of the coverage percentage.

| | Plain forecast (old rule) | Current default (80% + minimums of 3) |
|---|---|---|
| Shifts covered | 55% | 93% |
| Rolling-month tests | 51% | 94% |
| Agent-shifts short (share of agent-shifts needed) | 24% | 3.6% |
| Agent-shifts scheduled vs needed | 12% fewer | 42% more |

Coverage by type, current default: night 98%, morning 95%, evening 88%; weekdays 92%, weekends 97%. By month it ranged from 86% to 96%.

**What the extra 42% is.** About 28% of shifts had no calls at all, and about 20% of all scheduled agent-shifts sit on those empty shifts, almost all of them the minimums of 3 on quiet nights and weekends. The rest is the safety margin on busy shifts. Lowering `coverage_percent`, or setting a minimum to 0, reduces cost and cover together. Roughly: dropping the night minimum leaves about 88% covered and removes extra agent-shifts equal to about one fifth of the total needed; 70% coverage instead of 80% cuts the margin on weekday day shifts from about 19% to about 11% (cover falls from about 91% to about 84%).

### 5.4 What this means in practice

- Treat the monthly call total as reliable to within roughly 5-10%, a single day to within roughly 20-30%, and a single half-hour as a guess.
- Treat the shift numbers as a planning level that is exceeded on roughly 1 shift in 14, not a guarantee. Where a shift is short, it is short by about two agents on average.
- Test results come from data shaped like the training data (section 6, L1). Expect real accuracy on new data to be somewhat worse until it is measured (section 7).

## 6. Limitations

### Evidence

| # | Limitation | What it means | What to do |
|---|---|---|---|
| L1 | The test years were synthesised from the same real year they are scored against | The accuracy figures are probably optimistic for a genuinely new year | Re-run the checks on a second real year as soon as there is one |
| L2 | Only one year of real data exists | The yearly pattern and holiday effects are learned from a single year of real behaviour | Add real history as it accumulates; accuracy should improve |
| L3 | Staffing "needed" is computed with the same Erlang C rule used to plan | Coverage says the plan matches the rule, not that real service levels will be met | Compare with real service-level reports once live |

### Staffing model

| # | Limitation | What it means | What to do |
|---|---|---|---|
| L4 | Erlang C is built for steady, larger call flows; this queue has 1-3 calls per half-hour | 1 call needs 3 agents, 6 need 8; results step sharply and can overstate need on quiet shifts | Consider looser targets or smaller shrinkage if the client allows, then re-measure |
| L5 | The 80% / 20 s target, 30% shrinkage and shift minimums are defaults, not client-confirmed | They drive cost heavily | Confirm the real service agreement and absence rate |
| L6 | Only answered calls count as demand | About 9% of calls in the test data were abandoned or unanswered; those callers needed agents too, so need is understated | Decide whether to count them (not implemented) |
| L7 | Arrivals are assumed random (Poisson) with a fixed 15% day-level variation | Busy days that cluster (a campaign, an outage) are under-covered | Raise `coverage_percent` for known risky periods |
| L8 | No prediction intervals are returned | The response gives one number per half-hour, not a range | Use `coverage_percent` as the safety knob |
| L9 | AHT is a historical average per time of day | A change in call length (new product, new process) is not anticipated | Refresh with recent data |

### Forecast model

| # | Limitation | What it means | What to do |
|---|---|---|---|
| L10 | One-off events are not predicted | Event days were about 32% too low in the test | Provide known events by raising coverage manually |
| L11 | Trend fades quickly (flat within about a month) | Strong, lasting growth or decline is missed until the next refit | Refit regularly with fresh data |
| L12 | Holiday effects for rarely seen holidays use defaults | A holiday seen once is learned only partly | Review holiday days by hand |
| L13 | Weekends are poorly forecast (65% daily error) | Few calls, but the percentage is large | The weekend minimum covers this |
| L14 | Timestamps carry no time zone | Daylight-saving changes and exports from different zones are not handled | Export in one local time zone |
| L15 | Missing dates are read as zero calls | A skipped export looks like a day without demand | Check uploads cover the full period |

### Scheduling

| # | Limitation | What it means | What to do |
|---|---|---|---|
| L16 | The roster is built greedily | Not guaranteed to fill every shift even when the headcount estimate is met | Check `coverage_ok`; add agents or adjust shift times |
| L17 | Months are generated independently | Weekly limits and rest rules do not carry across a month boundary | Review the first and last days of each month |
| L18 | Shifts are three 8-hour blocks | No part-time, split or skill-based shifts, no single-queue-only model | Out of scope |

### Running the service

| # | Limitation | What it means | What to do |
|---|---|---|---|
| L19 | Background jobs live in memory | A restart loses unfinished and finished jobs | Fetch results promptly; keep one worker |
| L20 | Deliberately one worker process | It does not scale across containers | Move job state to shared storage before scaling |
| L21 | No built-in authentication | Anyone who can reach the port can use it | Run behind an authenticated HTTPS proxy; the container publishes on `127.0.0.1` only |
| L22 | Upload limits | 10 files of 25 MiB each; at most 3,650 forecast days; 40,000 forecast rows per schedule request | Split larger jobs |

## 7. Checking it on your own data

| Goal | How |
|---|---|
| Is a running deployment working? | `python tools/smoke_test.py` |
| How good is the call forecast on my data? | Backtest page (`backtest-ui/`), or `POST /api/v1/cdr/backtest` |
| Do the shifts get covered? | `python tools/shift_coverage_check.py --data <combined CDR file>` (needs a file with earlier history and a test period) |
| Where does it miss, month by month, day by day, shift by shift? | `python tools/build_comparison_page.py`, then open the page it writes |
| Which settings to change? | Raise `coverage_percent` for cover, lower it or a minimum for cost, then rerun the shift check |

## 8. Glossary

| Term | Meaning |
|---|---|
| AHT | Average handling time: how long an average call takes from answer to end |
| Agent-shift | One agent working one 8-hour shift |
| Bias | Total forecast minus total real, as a percentage. Negative means too few calls forecast |
| Covered shift | A shift where the agents scheduled are at least the agents the real calls needed |
| Erlang | A unit of workload: 1 Erlang is one agent talking for the whole interval |
| Erlang C | The formula that turns call volume and AHT into the agents needed to meet a service target |
| Service level | Share of calls answered within the target time (default 80% within 20 seconds) |
| Shrinkage | Share of rostered time agents are not available (breaks, training, absence); default 30% |
| STL | Seasonal-trend decomposition using LOESS: splits a series into trend, repeating pattern and leftovers |
| WAPE | Weighted absolute percentage error: total of the misses divided by the total real calls |
