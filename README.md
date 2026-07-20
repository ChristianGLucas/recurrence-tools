# recurrence-tools

Composable **RFC 5545 recurrence** nodes for the [Axiom](https://axiomide.com)
marketplace, published as `christiangeorgelucas/recurrence-tools`. Expand a
recurrence rule into real dates, query a window, step to the next occurrence,
test membership, count — and validate, parse, or build the rule itself.

Recurrence is the part of calendaring that looks easy and is not: "the
second-to-last weekday of every month, at 09:00 New York time, except the ones
we cancelled" is a single line of RFC 5545 and a very long afternoon of
hand-rolled date arithmetic. LLMs are notably bad at expanding these rules by
hand, and the failure mode — a meeting silently scheduled on the wrong day — is
quiet rather than loud.

Written in **Python**, wrapping a battle-tested, permissively-licensed library:

| Concern | Library | License |
|---|---|---|
| Recurrence expansion (`rrule`, `rruleset`) | [`python-dateutil`](https://github.com/dateutil/dateutil) | BSD-3-Clause (dual-licensed with Apache-2.0; its LICENSE extends BSD-3 over all code) |
| `six` (dateutil's only dependency) | [`six`](https://github.com/benjaminp/six) | MIT |
| IANA time-zone data | [`tzdata`](https://github.com/python/tzdata) | Apache-2.0 |

`python-dateutil` owns the algorithmically hard part. This package supplies the
envelope, the bounds, and a strict layer over the places dateutil is more
permissive than the RFC (see [Strictness](#strictness)).

All nodes are **stateless**, **deterministic**, and **fully offline** — no
network calls, no credentials, no persisted state.

## The `Recurrence` envelope

Every expansion node consumes the same envelope, so the nodes compose with each
other and a caller learns one shape:

```json
{
  "rrule":   "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-2",
  "dtstart": "19970929T090000",
  "tzid":    "America/New_York",
  "rdate":   [],
  "exdate":  []
}
```

Instants are exchanged as **RFC 5545 form strings**, never epoch numbers, and
every occurrence is emitted in the same form as the `dtstart` that anchored it:

| Form | Example | Emitted as |
|---|---|---|
| `DATE` | `19970902` | `19970902` |
| Floating `DATE-TIME` | `19970902T090000` | `19970902T090000` |
| UTC `DATE-TIME` | `19970902T090000Z` | `19970902T090000Z` |

Any zone `zoneinfo` knows is accepted, including short fixed ones like `UTC`,
`GMT` and `CET`. Only `localtime` and `Factory` are refused, because they
resolve through host configuration and would make the same request expand
differently on different machines.

With `tzid` set, occurrences keep **wall-clock time across DST transitions** —
a weekly 09:00 meeting in New York stays at 09:00 when the offset moves from
−05:00 to −04:00. That is what a calendar means by "every week at 9".

Because occurrences are emitted as local wall-clock strings, the zone is not
visible in the output itself; it becomes observable when the recurrence is
compared against an **absolute** instant. 09:00 in New York is `14:00Z` in
January but `13:00Z` in July.

The two ways of comparing therefore cut in *opposite* directions, which is worth
stating separately rather than together:

- A **`Between` window** matches an exact instant, so a window at `14:00Z`
  catches the January occurrence and misses the July one.
- A UTC **`UNTIL`** is an upper bound, so it cuts the *later* UTC instant:
  `UNTIL=…T130000Z` excludes January's 14:00Z occurrence while July's 13:00Z
  survives it.

That is the behaviour `tzid` buys you.

## Nodes

### Expansion — consume a `Recurrence`

| Node | Answers |
|---|---|
| `Expand` | "What are the first N occurrences?" |
| `Between` | "Which occurrences fall in `[start, end)`?" |
| `NextOccurrence` | "What is the first occurrence after this instant?" |
| `Contains` | "Is this exact instant an occurrence?" |
| `Count` | "How many occurrences are there?" |

### Rule-level — operate on the rule string alone

| Node | Answers |
|---|---|
| `Validate` | "Is this rule valid, and what is its canonical form?" |
| `Parse` | "What are this rule's individual parts?" |
| `Build` | "What rule do these parts make?" |

`Parse` and `Build` are inverses: feeding `Parse`'s output straight into `Build`
reproduces the canonical rule, and `Build` never emits a rule `Validate` rejects.
That edge needs no adapter at all — both sides are `RuleParts` — and it stays
honest on the error path: `Build` propagates `Parse`'s diagnosis rather than
re-deriving one from the empty parts it received.

**What chains onward.** `Build` → `Validate`/`Parse` (a rule string),
`Parse` → `Build` (parts), and `NextOccurrence` → any node taking an instant.
`Expand`, `Between`, and `Count` are terminal: no node in this package consumes
a list of occurrences, so they sit at the end of a graph.

## Strictness

`python-dateutil` accepts several inputs RFC 5545 forbids, and accepts them
*silently*. This package rejects them instead, because each one otherwise
changes what a rule means without telling anybody:

| Input | dateutil's behaviour | Here |
|---|---|---|
| `BYWEEKNO` on any `FREQ` but `YEARLY`; `BYYEARDAY` on `DAILY`/`WEEKLY`/`MONTHLY`; `BYMONTHDAY` on `WEEKLY` | Accepted, though RFC 5545 §3.3.10 forbids each | `INVALID_RULE` |
| `INTERVAL=0` | Yields the same instant forever — an unbounded generator that never advances | `INVALID_RULE` \* |
| `COUNT` and `UNTIL` together | Accepted; `COUNT` silently wins | `INVALID_RULE` |
| `BYMONTH=13`, `BYMONTHDAY=32`, `BYYEARDAY=400`, `BYWEEKNO=60` | Accepted; the rule simply never occurs | `INVALID_RULE` |
| A lone `BYSETPOS` | Silently ignored | `INVALID_RULE` |
| `BYDAY=8MO` with `FREQ=MONTHLY` (a month has ≤5 of any weekday) | **Crashes with `IndexError` from inside the iterator** | `INVALID_RULE` |
| `BYDAY=2MO` with `FREQ=WEEKLY` | Prefix silently dropped, widening "the 2nd Monday" to "every Monday" | `INVALID_RULE` |
| `DTSTART:`/`EXDATE:` smuggled into the rule string | Parsed, **silently overriding the caller's own anchor** | `INVALID_RULE` |

\* In `Build`, `interval: 0` is the protobuf sentinel for "part omitted", so it
drops `INTERVAL` from the assembled rule rather than rejecting it. Every node
that parses a rule *string* rejects a literal `INTERVAL=0`.

The `rrule` field must be a **bare RECUR value** — `KEY=VALUE` pairs joined by
`;`, with no `RRULE:` prefix, no `:`, and no line break. A RECUR value never
contains a colon, so this costs nothing legitimate and closes the smuggling
route entirely.

Everything else is dateutil's semantics, deferred to rather than re-derived.

## Bounds

Recurrences are lazy and frequently infinite, so every traversal runs under an
explicit budget. Exceeding one is a structured `LIMIT_EXCEEDED` error, never a
hang:

| Bound | Limit | On exceeding |
|---|---|---|
| Rule length | 2048 characters | `LIMIT_EXCEEDED` |
| `rdate` / `exdate` entries | 1000 each | `LIMIT_EXCEEDED` |
| `COUNT` inside a rule | 10000 | `LIMIT_EXCEEDED` |
| Occurrences visited | 200000 | `truncated` (or `LIMIT_EXCEEDED`, below) |
| Error codes | `INVALID_RULE`, `INVALID_DATETIME`, `INVALID_ARGUMENT`, `LIMIT_EXCEEDED`, `INTERNAL` | — |
| **Candidate instants examined** | **20,000,000** | `truncated` (or `LIMIT_EXCEEDED`, below) |
| Impossible `BYMONTH`/`BYMONTHDAY` pair | refused up front | `INVALID_RULE` |
| Impossible `BYYEARDAY`/`BYMONTH` pair | refused up front | `INVALID_RULE` |
| `BYSETPOS` beyond an interval's capacity | refused up front where the capacity is knowable | `INVALID_RULE` |
| Wall-clock backstop | 3s deadline, ~3.5s worst case for the caller | `LIMIT_EXCEEDED` |
| `limit` argument | 10000 accepted (default 100; `Count` defaults to 10000) | `INVALID_ARGUMENT` |

**Cost is not result size.** `FREQ=SECONDLY;BYHOUR=9;BYMINUTE=0;BYSECOND=0` is
one occurrence per day, but the expander steps through every second in between —
86400 candidate instants per occurrence returned. So the main budget counts
**candidates examined**, not occurrences returned. It is derived from the gaps
between the occurrences themselves, so it is a count rather than a clock: the
same request stops at the same place on every machine and every run.

`limit` is therefore a **ceiling, not a promise**. A large `limit` on a sparse
rule returns **fewer occurrences than asked for, flagged `truncated`** — real, correct occurrences, plus an honest
signal that more exist. It is never an error blaming the rule for being costly.

`NextOccurrence` and `Contains` have no partial form: a single answer cannot be
half-given. When the budget stops them before they reach an answer they return
`LIMIT_EXCEEDED`, because reporting "no next occurrence" or "not a member" from
an unfinished search would be a wrong answer stated with confidence.

`truncated` is also set when a recurrence runs past the end of the representable
calendar (year 9999): those occurrences exist in RFC 5545 terms but cannot be
expressed, so the list is short for a reason worth surfacing.

**Rules that can never occur are refused up front.** `BYMONTH=2;BYMONTHDAY=30`
has no answer to find — February has no 30th — and the expander would only
discover that by scanning to year 9999. A calendar fact settles it in
microseconds instead, with an error naming the real problem. The same applies to
a `BYYEARDAY`/`BYMONTH` pair no calendar produces (day 366 falls only in
December) and to a `BYSETPOS` position beyond what one interval can hold (a
`SECONDLY` interval contains exactly one instant, so there is no 300th).

Each of these compares against a deliberate *ceiling* rather than an exact
count, so it can refuse the impossible but never the possible: every refusal is
cross-checked in the test suite against what the expander itself can produce.

The capacity check counts the `BY*` parts that populate an interval, but it is
a ceiling, not an exact count — `BYDAY` and `BYMONTHDAY` narrow the day set
further and are deliberately not counted, so it refuses only the clearly
impossible. A rule it cannot rule out is still caught by the wall-clock
backstop below.

**The wall-clock backstop is deliberately not the primary bound.** A clock is
not deterministic; if it decided requests, identical input could return
different answers under different load. Each expansion runs in a child process
whose result is awaited for 3 seconds; a worker that overruns is killed, which
adds up to 0.5s more, so **the caller's worst case is about 3.5 seconds** — that
is the number to size a client timeout against, not the internal 3s deadline.

Measured on this machine, worst of three runs each: a scan-saturating sparse
rule ~1.2s, a `MINUTELY` sparse rule at the maximum limit ~0.9s, a dense
expansion at the maximum limit ~0.03s (~0.45s over HTTP, dominated by
serializing 10000 strings). The **ceiling check can re-walk the rule, so that
path measures ~2.4s** — about 80% of the 3s deadline. Under concurrent load the
deadline is genuinely reached, and reaching it produces a structured
`LIMIT_EXCEEDED`, never a hang or a wrong answer. Treat all of these as
indicative of one machine, not as a contract.

**Determinism, precisely.** The scan budget is a count, so for every rule whose
occurrences arrive within it the answer is determined by the input alone and is
identical on every machine and every run.

One structural gap remains, stated plainly because it is a real property of the
design rather than a hypothetical: the budget is charged from the gap *between*
occurrences, which means it is charged **after** the expander has already found
the next one. A rule whose occurrences were far enough apart could in principle
pay a cost no count had the chance to object to, leaving the wall clock to
decide — and a wall clock is not deterministic.

Inputs that reach it do exist and are not exotic: a rule whose feasibility the
capacity check cannot settle, or one that yields nothing while remaining cheap
per step, will run until the deadline stops it. Each such case that has been
identified was then refused up front instead, but the class is open — which is
exactly why the backstop is kept rather than argued away.

If a platform cannot provide an isolated worker, the request is refused rather
than run unbounded.

**The caller's rule is never rewritten.** An earlier version bounded cost by
injecting a synthesized `UNTIL`, which silently changed the answer for sparse
rules — a ceiling sized from `SECONDLY` steps landed before the rule's own first
occurrence. Cost is bounded by measuring and by isolation, never by altering
what was asked.

Hitting a bound is always either a flagged-short answer or a reported error —
never a hang, and never a silently short answer that looks complete.

## Composing on the error path

`Recurrence` and `RuleInput` each carry an optional inbound `error`. When a flow
wires an upstream node's `error` into it, the consuming node propagates that
error verbatim and does no work. Without it a downstream node sees only empty
fields and confidently blames the wrong one — reporting *"candidate is required"*
for what was actually a `BYSETPOS` mistake, while the flow reports success.

## Errors

No node raises for bad input. Each output message carries an `error` field,
unset on success, with a stable code: `INVALID_RULE`, `INVALID_DATETIME`,
`INVALID_ARGUMENT`, `LIMIT_EXCEEDED`, or `INTERNAL`. `INTERNAL` means the fault
was the package's, not the caller's rule — it is reported separately precisely
so a caller is never sent to debug input that was fine.

## Correctness

The suite includes an **independent oracle**: worked examples transcribed from
the expected output printed in **RFC 5545 section 3.8.5.3** itself — the
standards document, not this implementation and not dateutil's output. Several
span the US EDT→EST transition, where the RFC's own expected occurrences stay at
9:00 AM local, so they pin DST behaviour too.

The strictness table above is enforced by paired tests: one recording
dateutil's permissive behaviour as observed, one asserting this package rejects
it. If a future dateutil tightens up, the observation test fails loudly instead
of the guard quietly becoming dead code.

```bash
axiom test
```

## Licence

MIT — see [LICENSE](LICENSE). Built for the Axiom marketplace.
