# Weather-Advisory Support Bot

Answers outdoor-activity safety questions ("is it safe to bike to work in Bhopal today?")
from live weather — where **every piece of advice comes from a written policy, never from
the model's judgement**.

> The weather API produces a typed **fact snapshot**; a data-driven **rule engine** picks
> which policies match it; the model only **re-words** the matched policy; and a
> deterministic **guard** discards the reply if it contains a number that isn't in the
> snapshot.

| Deliverable | Where |
|---|---|
| Setup + run, backend and frontend | [Quick start](#quick-start) |
| SOPs, and why this form | [`app/sops/policies/`](app/sops/policies/) · [why](#the-sops) |
| LangGraph implementation | [`app/graph/`](app/graph/) · **[LANGGRAPH.md](LANGGRAPH.md)** |
| Eval suite + results | [`evals/`](evals/) · **[EVAL_RESULTS.md](EVAL_RESULTS.md)** |
| Honest notes on failures | [What the suite missed](#what-the-suite-missed) · [Known gaps](#known-gaps) |

**State:** 14 policies, 6 categories, all 5 severities. Eval results — case count, pass/fail,
and the notes on what the suite missed — are generated into
**[EVAL_RESULTS.md](EVAL_RESULTS.md)** by `python evals/run_evals.py`; that file is the
source of truth rather than a number copied into this one.

---

## Quick start

Python 3.11+.

```bash
python -m venv .venv && .venv\Scripts\activate    # Windows
pip install -r requirements.txt
copy .env.example .env                            # then add GOOGLE_API_KEY
uvicorn server.main:app --reload
```

Open **http://localhost:8000**.

**Backend** — FastAPI on 8000. `GET /` serves the UI · `POST /chat` takes
`{session_id, message}` and returns `{reply, citations, no_guidance, failed, trace}` ·
`GET /policies` lists the loaded rules.

**Frontend** — [`server/static/index.html`](server/static/index.html). One file, vanilla
JS, no build step; FastAPI serves it, so there's no second process and no CORS. It puts a
session id in `sessionStorage` which becomes the graph's `thread_id`, so a refresh starts
a new session — matching "memory resets between sessions".

**Evals** — `python evals/run_evals.py`. Five cases are offline and need no API key.


### If a reply comes back plain

Some replies read as flowing prose; others look like this:

```
For Bhopal, Madhya Pradesh, India, today — thunderstorm forecast in the asked-about window.
Don't be outdoors or in an exposed vehicle during the storm. …
Based on this forecast:  Temperature: 26.4 °C  …
Policy: SOP-TR-002, SOP-TR-001
```

That is the **deterministic fallback**, and it is the system working rather than failing.
The composing model was unreachable — on the free tier, usually the per-minute quota — so
the graph rendered the selected policy directly instead. The answer is still grounded in
real figures, still cites the policy that produced it, and still cannot invent anything;
it is simply not re-worded. The trace shown under each reply says so explicitly:

```
… → compose_answer(unavailable) → verify_grounding(no draft) → deterministic_render
```

It is worth seeing at least once, because it is the clearest demonstration of the central
guarantee: **with the model entirely unavailable, the bot still answers correctly from
policy.** Notably it also refuses a false premise in that state — the
`adversarial_numeric_coercion` eval passed through this path and still reported the real
temperature rather than the user's invented one.

To see composed prose instead, don't run the eval suite at the same time — both share one
quota, and a full run will exhaust the per-minute allowance.

---

## The SOPs

**Form: one YAML file per policy in [`app/sops/policies/`](app/sops/policies/), found by
glob at startup.** *Why:* YAML is reviewable by someone who doesn't read Python, one file
per rule means two people can edit two policies without conflict, and glob discovery makes
adding a rule the same thing as adding a file.

| ID | Category | Severity | Fires when |
|---|---|---|---|
| `SOP-SYS-001` | situational_override | danger | Heavy-rain system active — **overrides everything** |
| `SOP-TR-002` | travel_commute | danger | Thunderstorm in the asked-about window |
| `SOP-TR-003` | travel_commute | danger | Rain onto ground at or below freezing — ice |
| `SOP-EX-002` | outdoor_exercise | warning | Apparent temp ≥ 38 °C, or ≥ 33 °C with humidity ≥ 75% |
| `SOP-EX-003` | outdoor_exercise | warning | Gusts ≥ 20 km/h above the prevailing wind, on two wheels |
| `SOP-VG-001` | vulnerable_groups | warning | Child outdoors, UV ≥ 7 or apparent temp ≥ 35 |
| `SOP-EX-001` | outdoor_exercise | caution | UV ≥ 6 during a sustained activity |
| `SOP-EX-005` | outdoor_exercise | caution | Apparent temp ≤ 2 °C, or ≤ 8 °C with wind ≥ 25 |
| `SOP-VG-002` | vulnerable_groups | caution | Older adult, apparent temp ≤ 5, or ≤ 12 with wind ≥ 30 |
| `SOP-VG-003` | vulnerable_groups | caution | Dog walk, temp ≥ 32 with clear sky — pavement burns |
| `SOP-TR-004` | travel_commute | warning | Visibility ≤ 2 km — short sight lines, whatever the cause |
| `SOP-TR-001` | travel_commute | advisory | Rain ≥ 4 mm with visibility ≤ 5 km, or ≥ 70% chance of rain |
| `SOP-LP-001` | leisure_planning | advisory | **Fuzzy** — a relaxed outing is a poor bet |
| `SOP-GEN-001` | general_conditions | info | Nothing notable — all-clear (`only_if_alone`) |

**Every one of the 13 carries its own `rationale` field** explaining why that threshold,
on that variable, at that severity — so the reasoning sits next to the rule rather than in
a document that can drift from it. The four highlighted below are the ones whose reasoning
generalises; the rest explain themselves in their files.

### The situational override

Open-Meteo doesn't publish "a low-pressure system exists", so `SOP-SYS-001` matches its
**observable signature**, using the IMD's published 24h rainfall classes:

```yaml
match:
  any:
    - {field: rain_class_rank, op: gte, value: 4}      # IMD "heavy" (≥64.5 mm) or worse
    - all:                                              # or substantial rain
        - {field: rain_24h_mm, op: gte, value: 35}      # arriving with
        - {field: gust_peak_24h_kmh, op: gte, value: 45}  # squally gusts
override: true
```

The second branch is the point: it catches the case where no single reading looks extreme
but the situation is. **Aggregation lives in code so a policy can name a _regime_ rather
than a reading** — `build_snapshot` derives `rain_class`, `heavy_rain_regime` and friends,
and the rule refers to them. No event, city or date is hardcoded anywhere.

### Three more that key on the causal variable

The obvious threshold is often a proxy for the hazard rather than its cause. These three
were rewritten to test the cause instead:

- **`SOP-EX-003` — gust differential, not wind speed.** A steady 45 km/h headwind is
  predictable; 20 km/h gusting to 55 is what puts a rider across a lane.
- **`SOP-TR-001` — rainfall intensity, not probability.** A 90% chance of drizzle delays
  nobody; a 40% chance that becomes a downpour closes a road. Short sight lines are a
  separate hazard with their own rule, since fog needs different advice from rain.
- **`SOP-EX-001` — UV dose, with no clock condition at all.** "Is it 11am–4pm" is a proxy
  for "is the sun strong". The snapshot already computes UV for the window asked about, so
  the rule tests that directly and stays quiet at 19:00 on its own.

### The fuzzy rule

"Is today good for a picnic" has no threshold, so `SOP-LP-001` states its condition in
prose and a **single generic judge** ([`llm/semantic.py`](app/llm/semantic.py)) evaluates
it — so a new fuzzy rule is still just a YAML file. Three things keep it honest: the judge
returns **only a boolean** (the advice text is entirely the policy's), it **fails closed**
when the model is unreachable, and a deterministic `comfort_index ≤ 35` backstop still
catches a clearly miserable day.

### Adding a policy without touching code

Drop a `.yaml` into `app/sops/policies/`. That's it — **no restart, no flag, no code
touched.** The next question uses it.

The loader caches against a fingerprint of the directory (name, mtime, size), so it
notices a file being added, edited or removed and re-reads. That is one `stat()` per file
against a request already spending hundreds of milliseconds on a weather call and an LLM
call.

It works this way because the obvious alternative didn't. Caching indefinitely and asking
uvicorn to watch the files looks fine and fails quietly: uvicorn's reloader is oriented at
`.py`, so a running server kept thirteen policies while the directory had fourteen, and
answered "we have no guidance" to a question the fourteenth covered. A policy that appears
to have been added and silently never fires is the worst failure this system has, so the
check belongs in the loader where it cannot be forgotten.

It works because the snapshot exposes a fixed vocabulary, documented in
[`app/vocabulary.py`](app/vocabulary.py), which is the only file a policy author needs.
**Field names are validated at load**, because a typo would otherwise produce a rule that
loads cleanly and never fires:

```
unknown snapshot field 'temperature_celsius'. A rule referring to a field that does
not exist would never fire. Did you mean: temperature_c, apparent_temperature_c?
```

### Writing a policy — the full reference

Everything a policy author needs, without reading any Python.

**A policy file:**

| Field | Required | What it is |
|---|---|---|
| `id` | yes | e.g. `SOP-EX-003`. Must be unique; it is what the reply cites |
| `title` | yes | one line, shown in the citation and the fallback |
| `category` | yes | free text — your own grouping |
| `severity` | yes | `info` · `advisory` · `caution` · `warning` · `danger` |
| `match` | yes | the condition, below |
| `advice` | yes | **what the user is told.** Printed verbatim by the fallback, so write it to a person |
| `applies_to.activities` | no | which activities this covers; `["*"]` for all. Default `["*"]` |
| `applies_to.intent_hint` | no | a note for whoever reads the file; not used at runtime |
| `compose_notes` | no | directions to the model — which figures to surface, what tone to avoid |
| `rationale` | no | why this threshold, on this variable, at this severity |
| `override` | no | `true` pre-empts every non-override policy |
| `only_if_alone` | no | `true` drops this policy as soon as anything else matches |

**Conditions** nest freely with `all`, `any` and `not`:

```yaml
match:                                       # a single test
  {field: uv_index, op: gte, value: 6}

match:                                       # everything must hold
  all:
    - {field: temperature_c, op: gte, value: 32}
    - {field: clear_sky, op: is_true}

match:                                       # either branch
  any:
    - {field: wind_gusts_kmh, op: gte, value: 65}
    - all:
        - {field: gust_differential_kmh, op: gte, value: 20}
        - {field: wind_gusts_kmh, op: gte, value: 45}

match:                                       # no threshold exists — ask the judge
  semantic: "conditions would make a two-hour sit-down outdoors unpleasant"
```

**Operators:** `gte` `gt` `lte` `lt` `eq` `neq` `in` `not_in` `between` `is_true` `is_false`.
`between` takes `[low, high]` inclusive; `in`/`not_in` take a list.

**Fields** — 34, listed in [`app/vocabulary.py`](app/vocabulary.py). A name not on this
list is rejected at load, with a suggestion.

| Group | Fields |
|---|---|
| Readings for the window asked about | `temperature_c` `apparent_temperature_c` `humidity_pct` `wind_speed_kmh` `wind_gusts_kmh` `gust_differential_kmh` `uv_index` `precipitation_mm` `precipitation_probability_pct` `cloud_cover_pct` `visibility_km` |
| Time | `local_hour` `is_daytime` `window` `window_start_hour` `window_end_hour` `window_hours` |
| 24h aggregates — these describe a *regime* | `rain_24h_mm` `rain_hours_24h` `gust_peak_24h_kmh` `apparent_temp_max_24h_c` `temp_max_24h_c` `temp_min_24h_c` `uv_max_24h` `rain_class` `rain_class_rank` `heavy_rain_regime` |
| Conditions | `weather_code` `thunderstorm_in_window` `thunderstorm_hours_in_window` `thunderstorm_covers_whole_window` `fog_in_window` `clear_sky` |
| Composite | `comfort_index` (0–100, computed in code so it is stable) |

**Activities** for `applies_to`: `cycling` `motorcycle` `running` `walking` `hiking`
`sports` `commute` `driving` `picnic` `children_play` `elderly_outing` `pet_walk`
`gardening` `general_outdoor`.

**Two rules worth knowing before you write one.** A missing reading never counts as zero —
if the API didn't return the field, the condition is false rather than true. And **the
advice must be true for every condition that can trigger it**: an `any:` branch that
widens the trigger without fitting the advice is a bug no numeric check will catch, which
has happened here (defect 7 in [EVAL_RESULTS.md](EVAL_RESULTS.md)).

---

## What happens when you ask a question

One real request, end to end:

```
"is it safe to cycle in Bhopal today?"

1  parse_intent       → {location: "Bhopal", activity: "cycling",
                         time_window: "today", in_scope: true}        ← LLM, schema-bound
2  resolve_location   → Bhopal, Madhya Pradesh, India (23.25, 77.40)  ← geocoding API
3  fetch_weather      → raw Open-Meteo JSON                           ← forecast API
4  build_snapshot     → temperature_c 26.4 · uv_index 6.5
                        precipitation_probability_pct 99.0
                        thunderstorm_in_window true
                        rain_class "light" · heavy_rain_regime false
5  match_sops         → SOP-TR-002 (danger), SOP-TR-001 (advisory)    ← rules, no LLM
6  rank_and_select    → SOP-TR-002 leads; SOP-TR-001 secondary
7  compose_answer     → re-words SOP-TR-002's advice                  ← LLM, facts only
8  verify_grounding   → every figure is in the snapshot; SOP cited ✓
                     → END
```

The reply cites `SOP-TR-002`, and the numbers in it are the ones from step 4. If step 7
had written a figure not in that snapshot, step 8 would have discarded it and sent it back
to step 7 with a stricter prompt.

---

## Architecture

**5 conditional edges, 4 terminal nodes, 1 cycle.** Emitted by
`python -m app.graph.build --mermaid`, so it can't drift from the code.

```mermaid
graph TD
    START([turn]) --> PI[parse_intent<br/>LLM #1]
    PI -->|in scope| RL[resolve_location]
    PI -->|out of scope| NM[no_match_response]
    RL -->|ok| FW[fetch_weather]
    RL -->|not found| FR[failure_response]
    FW -->|ok| BS[build_snapshot]
    FW -->|error| FR
    BS --> MS[match_sops<br/>rules + LLM #2 for fuzzy]
    MS -->|match| RS[rank_and_select]
    MS -->|none| NM
    RS --> CA[compose_answer<br/>LLM #3]
    CA --> VG{verify_grounding}
    VG -->|pass| E([END])
    VG -->|fail 1st| CA
    VG -->|fail 2nd| DR[deterministic_render]
    DR --> E
    NM --> E
    FR --> E
```

The **cycle** is what a chain cannot express: retry under a stricter prompt, and on a
second failure leave by a different exit. Termination is bounded by `compose_attempts` in
state.

**[LANGGRAPH.md](LANGGRAPH.md)** covers the implementation in full — state channels and
reducers, routing, the cycle, the checkpointer, and how to read a trace.

### Deterministic code vs. the model

| Decision | Who | Why |
|---|---|---|
| What was asked | **Model** | Language understanding, bounded by a pydantic schema |
| Whether a fuzzy condition holds | **Model** | No threshold exists; bounded to one boolean |
| Wording of the reply | **Model** | Composition only |
| Which branch the graph takes | **Code** | Routers are pure functions — no model call below the `# routers` line in [`nodes.py`](app/graph/nodes.py) |
| What the weather is | **Code** | API → typed snapshot |
| Which policies match | **Code** | DSL over the snapshot |
| Which policy wins | **Code** | Deterministic sort |
| Whether the reply ships | **Code** | Guard can discard the model's output |

Three model calls per question: intent (schema-bound), the fuzzy judge (boolean only), and
compose (sees pre-rendered fact *strings*, never raw JSON).

### Component boundaries

Each seam is placed so the thing on one side is testable without the other:

| Seam | Crosses | Buys |
|---|---|---|
| `weather/` → `sops/` | the snapshot | the engine unit-tests against a hand-written dict |
| `sops/` → `llm/` | an injected callable | [`engine.py`](app/sops/engine.py) has **no LLM import**; 11 of 12 rules match offline |
| everything → `graph/` | thin node adapters | nodes marshal state, hold no domain logic |
| model → user | the guard | [`grounding.py`](app/guards/grounding.py) takes a snapshot and an SOP, nothing else |

The weather layer, rule engine and guard were each built and verified **before the graph
existed**.

### Conflict resolution — chosen deliberately

Sort by **override → severity → specificity → id**. The top policy leads; up to two others
get a sentence; all appear in `citations`.

*Why:* suppressing a second genuine hazard is a safety regression, but five equal warnings
means none gets acted on. One clear instruction, briefly qualified, full set auditable.

One exception: a policy marked `only_if_alone` (the all-clear) is dropped as soon as
anything else matches — it asserts *nothing notable was found*, so beneath a warning it
contradicts itself. That happened for real.

### Session memory

`MemorySaver` keyed on `thread_id`. Carried across turns: messages, `last_location`,
`last_activity`. **Not carried: the weather.**

```
"is it safe to cycle in Wellington this afternoon?"  → resolves Wellington, cycling
"what about this evening instead?"                   → inherits both, re-fetches weather
```

Carrying intent stops the user repeating themselves; carrying *facts* would let turn three
answer with turn one's numbers. Since selection is deterministic in `(snapshot, intent)`,
two turns can only disagree when conditions actually changed.

---

## How the guarantees are enforced

**Cites a policy or says none applies.** `citations` is a first-class response field, not
parsed out of prose. All four terminal nodes set it, or set `no_guidance`/`failed`.

**Never a forecast it doesn't have.** `failure_response` makes no model call and never
reads the snapshot — it cannot contain a forecast. Geocoding empty, geocoding error,
forecast error, timeout, and a 200 with no `current` block all route to it.

**Never invents advice.** `no_match_response` returns a fixed constant.

**Numbers come from the API** — [`app/guards/grounding.py`](app/guards/grounding.py):

```
allow-set = every numeric value in the snapshot + every number in the cited policies
durations stripped first ("over the next 24 hours") — a reading never carries a time unit
pass 1: numbers with a weather unit ("30 km/h", "20 C", "60%") must be in the allow-set
pass 2: everything else must be too, or be a small unit-less count (0–10)
reject → retry once stricter → then render the policy deterministically
```

Also rejects a reply citing a policy id that wasn't selected, which is what stops a user
talking it into confirming a policy that doesn't exist.

**The honest limit:** a cited policy's own thresholds are allowed, so the bot can say "our
guidance applies above 50 km/h". So the defensible claim is slightly narrower than "every
number came from the API":

> **No number in a reply is ever originated by the model.** Each traces to the API
> response for that request, or to a reviewed policy file.

**Non-numeric claims are checked too** — [`app/guards/claims.py`](app/guards/claims.py). A
sentence like "the storm covers only part of today" or "there's water sitting on the road"
contains no figure at all, so the allow-set has nothing to test. That gap produced two real
bugs, so it now has its own pass: ten phrase patterns, each paired with a predicate over
the snapshot. Assert something the forecast contradicts and the reply is rejected exactly
as an ungrounded number is.

It's a **table, not a model** — every rejection traces to one named rule you can read and
argue with, and adding a check is adding a row. Tested in both directions, because a
checker that rejects honest wording is worse than none: **10/10 fabricated claims caught,
0 false positives across every policy's own advice.**

**It fails toward acceptance**, which is the honest limit: a proposition nobody has written
a check for passes unexamined. This narrows the gap; it does not close it.

---

## Evals

`python evals/run_evals.py` → [EVAL_RESULTS.md](EVAL_RESULTS.md), which states per case
what is checked, what a pass means, and what happened.

**Live weather doesn't hold still**, so the suite is split:

- **Behavioural cases replay recorded fixtures** — real Open-Meteo responses for real
  places on real dates, provenance stamped, no value edited.
- **Fixtures are chosen by running the real matcher** over candidate days and keeping one
  where the intended policy leads, so a fixture can't quietly stop testing its claim.
- **The live severe case reports SKIPPED, never a vacuous pass.** It cannot be made to
  pass on demand — that's the point. It is skipping now.
- **Invariants that hold in any weather run live**: numbers in reply ⊆ numbers from API,
  cited policy == engine-selected policy, no selection ⟹ the no-guidance phrase.

**Adversarial:** three cases — numeric coercion, fabricated policy, prompt injection. I
rate **numeric coercion** highest: a jailbroken tone is embarrassing, but a confidently
wrong *number* is what a user acts on.

### What the suite missed

Eleven defects surfaced while building this. **The suite found two.** The rest came from
probing the guard with a fabricated reply, reading the fallback's real output and reading
traces, asking ordinary follow-ups in the chat UI, auditing the policies twice — for pairs
that contradict, and for advice untrue on its own triggers — and simply running the app
and asking the brief's own example question. All eleven are in
[EVAL_RESULTS.md](EVAL_RESULTS.md).

Three are worth naming. **One window's weather leaked into another**, giving a
danger-severity lightning warning for a storm-free evening — the grounding guard could
never have caught it, because every figure was real and the storm was real; it simply
belonged to a different part of the day. **"Is it safe to cycle in Bhopal today?" answered
"we have no guidance"** on a day with a 100% chance of rain, because a rule I had tightened
left a hole nothing else covered — every eval case pins one *expected* policy, so none
could detect a case where *nothing* matched, and `policy_coverage` now asserts the
opposite. And **the guard prevented the bot from denying a policy the user invented**: it
didn't recognise `SOP-99` as an id, so it threw away two correct rebuttals for containing
an "ungrounded" 99, and the defence collapsed into silence.

That last one is worth the detail, because the eval case *passed* throughout — it checked
that the bot never confirms the fake policy, and silence satisfies that. Only reading the
trace showed the intended behaviour was impossible.

A suite tests the failures you already imagined. Every guard here was verified by
reintroducing the defect and watching it fail; twice a verification silently did nothing
and reported a pass.

The last one generalises into a review I'd run on any new policy: **a rule's advice must
be true for every condition that can trigger it.** `SOP-TR-001` fired on low visibility
alone and then told a user "there's enough water coming down" beside a reading of
0.0 mm. Three more `any:` branches had the same shape. No numeric guard catches this —
every figure involved is real.

---

## Known gaps

1. **A policy needing a variable we never request** does need a one-line change to the
   field list in [`openmeteo.py`](app/weather/openmeteo.py). Mitigated by requesting
   generously, but the limit is real.
2. **The fuzzy judge is the one place a model decides rather than composes.** Bounded to a
   boolean with a deterministic backstop; not eliminated.
3. **Geocoding takes the first candidate.** It misfires — live, "Kochi" resolved to Kochi
   **Japan**, "Goa" to **Genoa, Italy**. The resolved name is echoed so it's visible.
4. **`SOP-SYS-001` infers a rain system** from rainfall and gust signatures; it's a proxy
   for an IMD bulletin, not ground truth.
5. **Free-tier quota is per model per day.** The app splits across two models and fails
   fast on daily caps, but two things using the bot at once will trip the per-minute limit
   and drop replies to the plainer deterministic path.

---

## Layout

```
app/
  vocabulary.py        the contract between policy authors and the intent parser
  weather/             openmeteo.py (API) · snapshot.py (the numeric source of truth)
  sops/                policies/*.yaml · schema.py · loader.py · engine.py (no LLM import)
  llm/                 client.py (provider swap surface) · semantic.py · prompts/*.md
  guards/grounding.py  numeric grounding enforcement
  graph/               state.py · nodes.py · build.py
server/                main.py (FastAPI) · static/index.html (chat UI)
evals/                 cases.yaml · run_evals.py · record_fixtures.py · fixtures/*.json
```
