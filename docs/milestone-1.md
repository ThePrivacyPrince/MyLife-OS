# Milestone 1 — Foundation & Daily Execution

Status: **implemented, 155 automated tests passing.** Awaiting owner review before Milestone 2.

## 1. Implementation plan (as built)

| Step | Scope | Where |
|---|---|---|
| 1 | Five Pillars as permanent config, synced into the DB | `config/pillars.yaml`, `lifeos/db.py` |
| 2 | Schema: goals, tasks, recurring templates, dependencies, evidence, audit events, day reviews | `lifeos/models.py` |
| 3 | Pure engines (no DB): state machine, PAF gate, recurrence, dependency graph | `lifeos/engines/` |
| 4 | Services: every write goes through one audited code path | `lifeos/services/` |
| 5 | Daily planning + end-of-day review + pattern detection | `lifeos/services/planner.py` |
| 6 | Starter data (goals, real first tasks, routines; **no money figures in Git**) | `seed/starter.yaml` |
| 7 | CLI (`lifeos init/seed/today/review/dashboard`) | `lifeos/cli.py` |
| 8 | Streamlit Daily Command Center | `lifeos/dashboard/app.py` |
| 9 | Unit, integration, adversarial and UI smoke tests + CI | `tests/`, `.github/workflows/ci.yml` |

**Deliberately not in M1:** FastAPI (Streamlit calls the services directly; add an API when a phone app or bot needs one), Alembic (schema still moving; add before real data matters in M2), AI of any kind, finances.

## 2. Database schema

```
pillars(code PK, name, purpose, subdomains JSON, sort_order)
goals(id, code UNIQUE, title, description, pillar_code FK, secondary_pillars JSON,
      target_metric, baseline, target_value, current_value NULL=unknown, progress_evidence,
      target_date, status, created_at, updated_at)
recurring_templates(id, title, description, pillar_code FK, priority CHECK 1..3, goal_id FK,
      rule RRULE, frequency_label, start_date, end_date, exceptions JSON, estimated_minutes,
      missed_policy {mark_missed|carry_over}, protected, evidence_required, active,
      generated_through, created_at)
tasks(id, title, description, pillar_code FK, priority CHECK 1..3, kind {one_time|occurrence},
      template_id FK NULL, occurrence_date, scheduled_date, due_date, estimated_minutes,
      status, status_reason, follow_up_date, goal_id FK, evidence_required, protected,
      created_at, updated_at, completed_at,
      UNIQUE(template_id, occurrence_date))
task_dependencies(id, task_id FK, depends_on_id FK, UNIQUE(task_id, depends_on_id))
task_events(id, task_id FK, event_type, from_value, to_value, reason, actor, at)   -- append-only
evidence(id, task_id FK, description, uri, at)
day_reviews(id, date UNIQUE, note, summary JSON, created_at, updated_at)
```

Design choices:
- **One `tasks` table** for one-time tasks *and* recurring occurrences → one unified priority queue, while the
  Recurring view filters by `kind`. Each occurrence is its own row with its own status/evidence/timestamps.
- `scheduled_date` = "don't start before"; `due_date` = the hard deadline. Rescheduling never moves the due date.
- Foreign keys and CHECK constraints are enforced in SQLite too — the database backstops the services.
- Timestamps are UTC; calendar dates are local (`config/settings.yaml: timezone`).

## 3. Priority engine (PAF) design

`lifeos/engines/priority.py` — pure functions.

- **Actionable** = status `pending`/`in_progress` AND all prerequisites completed AND (no date OR scheduled/due date ≤ today).
- **Gate:** a task is gated if any *other* actionable, *unprotected* task has a higher priority.
  P1 gates P2 and P3; P2 gates P3.
- **Resolved for gating** = completed, blocked (reason **and** follow-up date), waiting (reason),
  deferred (reason **and** return date), cancelled (reason), missed.
- **Protected** tasks (sleep, shifts, caregiving) are never gated and never gate anything.
- Gate applies to **starting** and **completing**. Doing work out of order is allowed only with an
  **override reason**, written to the audit log as `gate_override`. Blank/whitespace reasons are rejected.
- Exception: completing a *missed* routine late isn't gated — that records something that already happened.
- Every gate decision carries a human-readable explanation naming the blocking tasks.
- **Priority changes** require a reason and record previous/new/reason/actor/timestamp.
- **Control weakness detection:** same task downgraded from P1 ≥2× in 14 days, or ≥3 P1 downgrades total → flagged.

## 4. Recurring-task model

- A `RecurringTemplate` holds an RFC 5545 RRULE built from friendly options:
  daily / selected weekdays / weekly / biweekly / monthly / custom.
- `generate_occurrences(through=today)` creates rows up to today only, idempotently
  (watermark `generated_through` + DB unique constraint). First-run backfill is capped at 7 days.
- Exceptions (skip dates), end dates, and pause/resume are supported.
- **Missed-occurrence policy** per template:
  - `mark_missed` (daily resets): yesterday's open occurrence becomes `missed`, recorded by `system` with a reason. Never deleted.
  - `carry_over` (laundry, money check-in): stays open and shows as **overdue** until done.
- Completing today's occurrence cannot affect any other day — they are separate rows.

## 5. Task state machine

```
pending ──► in_progress ──► completed ──(reopen*)──► pending
   │  ╲          │
   │   ╲         ├──► blocked*  (needs follow-up date) ──► pending / in_progress / waiting / cancelled
   │    ╲        ├──► waiting*  ──► pending / in_progress / blocked / completed / cancelled
   │     ╲       ├──► deferred* (needs future return date; auto-returns to pending on that date)
   │      ╲      └──► cancelled* ──(reopen*)──► pending
   └──► missed* (recurring only, usually by system) ──► completed (late) / pending (reopen*)
* = reason required
```
`Ready` and `Overdue` are **derived**, not stored, so they can never be out of sync with the dates and dependencies they describe.
Extra guards: prerequisites must be complete to start or complete; evidence-required tasks need evidence before completion.

## 6. Acceptance test matrix

| ID | Requirement | Test(s) |
|---|---|---|
| SM-01..05 | Only allowed transitions; reasons enforced; random walks stay valid (property-based) | `unit/test_state_machine.py` |
| PAF-01 | P2 gated until P1 resolved | `test_paf01_*` |
| PAF-02 | P3 gated by P2 | `test_paf02_*` |
| PAF-03 | Protected never gated / never gates | `test_paf03_*` |
| PAF-04 | Block/defer/cancel/wait legitimately release the gate | `test_paf04_*` (4 cases) |
| PAF-05/06 | Future-dated or dependency-locked P1 doesn't gate today | `test_paf05_*`, `test_paf06_*` |
| PAF-07 | Override needs a real reason and is audited | `test_paf07_*`, `test_adv03_*` |
| PAF-08 | Priority change audited with reason | `test_paf08_*` |
| PAF-09 | Repeated P1 downgrade flagged | `test_paf09_*` |
| REC-01..04 | Rule builders, weekday/biweekly cadence, DST, window invariants (property-based) | `unit/test_recurrence.py` |
| REC-05 | Completing today doesn't complete future | `test_rec05_*` |
| REC-06 | Generation idempotent | `test_rec06_*`, `test_adv06_*` |
| REC-07..11 | Exceptions, end dates, missed vs carry-over, backfill cap, pause | `unit/test_recurrence.py` |
| DEP-01..04 | No self/circular deps; unmet prerequisites block start/complete | `unit/test_tasks_and_dependencies.py` |
| TSK-01..07 | Input validation, evidence, block/defer rules, reschedule never moves due date, edit can't bypass audit, full history | same file |
| PLN-01..05 | Plan sections, overdue never dropped, overcommit warning, dependency-locked view, unified queue, idempotent | `unit/test_planner_review.py` |
| REV-01..02 | Review summary, upsert, constructive pattern flags (no shame language) | same file |
| ADV-01..08 | SQL/markup in titles, malformed RRULEs, DB constraints, conflicting dates, unknown status | `adversarial/test_adversarial.py` |
| INT-01..04 | Seed idempotent & money-free; multi-day simulation; goal links; CLI | `integration/test_end_to_end.py` |
| INT-05..06 | Every dashboard page renders; Done button writes through services | `integration/test_dashboard.py` |

## 7. Minimum viable dashboard

Sidebar: view picker, planning date, pillar filter, one-time/recurring filter.

- **Today** — metrics (P1 open, done today, overdue, capacity), warnings and pattern flags, protected commitments,
  P1 → P2 → P3 cards (gated cards show 🔒 and *why*), blocked & waiting, waiting-on-prerequisites, coming up.
  Each card: Start / Done, plus **More** → block, wait, defer, cancel, reopen, priority, reschedule, evidence, history, override.
- **Add** — one-time task form, recurring routine form.
- **Recurring** — every routine with 14-day adherence, next dates, pause/resume, skip a date.
- **All tasks** — filterable table; open any task to act on it.
- **Goals** — by pillar; linked-task counts labeled *activity, not achievement*; unknown values shown as unknown.
- **Review** — done vs still open, P1 completion rate, missed routines, gate overrides, move-to-tomorrow with a reason, patterns, daily note.

## Known limitations

- Single user, local only. Phone access needs Tailscale (or similar) until hosting is decided.
- No migrations yet — deleting `data/lifeos.db` is the upgrade path during M1. Alembic arrives with M2.
- Goals are read-only in the UI (seeded); goal editing with audit history comes in M3 with reporting.
- Dependency editing after creation is service/CLI-level only (set at creation in the UI).
- No notifications (by design for now).
- At-rest encryption not implemented; the DB is a local file excluded from Git.

## Suggested improvements before / during M2

1. Alembic migrations before any real financial data goes in.
2. A "Pull into today" button for future tasks.
3. Protected work-shift template once the RXO schedule is known (it'll make capacity math honest).
4. Decision Journal (small, compounds over time).

---

# Milestone 1.1 — The planning cascade

Added at the owner's request: a 3-5 yr / 6-12 mo / 3 mo / daily-weekly planning ladder.

```
3-5 years      VISION        where I want to be                   FIN-V1
    ↑
6-12 months    GOALS         what must be true within a year       FIN-G003
    ↑
3 months       OBJECTIVES    what this quarter must deliver        FIN-G003-O1
    ↑
daily/weekly   KEY RESULTS   measurable things I do each day/week  FIN-G003-O1-KR1
    ↑
               tasks & routines linked to a key result
```

All arrows point up. The hierarchy is now **Pillar → Vision → Goal → Objective → Key result → Task/Routine**.

**Rules**
- Horizon checks come from `config/settings.yaml → cascade`. A vision outside 3-5 years, or a goal outside 6-12 months,
  saves with a gentle warning. An objective longer than 100 days is rejected, because that's a goal.
- Key results are **daily or weekly**, measured by either:
  - *completions*: linked tasks/routines completed in the period, or
  - *manual*: check-in amounts, exact decimals stored as text (never floats).
- Pace only counts **finished** days, so Monday morning is never "behind".
- Progress flows up: completed tasks → KR periods met → objective hit-rate (finished periods only) → goal → vision.
- Linking a routine moves its open and future occurrences; completed history keeps the link it was done under.
- Every plan change is audited in `plan_events`. Closing an objective, goal or vision requires a reason.
- **Gaps** are flagged, never auto-filled:
  - a pillar with no vision, or a vision with no goals
  - a goal with no vision, or no objective this quarter
  - an objective with no key results, or one past its end date and still open
  - a key result nothing drives
- Nothing in the tiers is invented: visions and objectives are written by you.

**Upgrade:** existing databases gain the new nullable columns in place on startup (`lifeos/db.py`, test `UPG-01`).

**UI:**
- **Plan** page (replaces Goals): the ladder, a gap summary, and each pillar's chain with live KR progress bars, plus forms for every tier, check-ins, status changes and goal→vision links.
- **Today:** this period's key results and a "↑ serves: …" line on every linked task.
- **Add:** both forms can link a new task or routine to a key result.
- **Tasks:** a "Link ↑" tab. **Recurring:** a link option on each routine.

Tests: `tests/unit/test_cascade.py` (CAS-01..20), `tests/integration/test_upgrade.py`, `test_int07` (builds a full chain through the UI).

---

# Milestone 1.2 — Countdowns and timelines ("so I'm not starting over")

- **Every tier has a deadline.**
  - A vision needs a target year, which counts down to Dec 31 of that year.
  - A goal needs a target date; older goals without one are flagged until you set it.
  - An objective has its end date: 91 days inclusive by default, about 100 days at most.
- **Countdowns** (`lifeos/engines/timeline.py`) show days left, the share of the time window already used, and a state:
  on the clock · due soon (≤14 days) · due today · past deadline · not started · no deadline.
- **Deadlines are sticky.** Setting the first deadline is free. *Moving* one requires a reason, is logged as
  `deadline_moved`, and shows "moved N×". Two or more moves on a goal raise a gentle "is it realistic?" flag.
- **Passed deadlines must be closed out:** achieved, missed, or moved with a reason. Nothing silently resets.
- **Goal timeline:** each goal's runway is split into ~3-month slots, each marked achieved / missed / now /
  planned / not planned yet / ⚠️ nothing was planned. "Plan it" pre-fills the objective form for the next empty quarter.
- **Rolling forward:** 14 days before an objective ends, if the goal has no next objective, you get a
  "Plan next quarter →" prompt that pre-fills the form to start the day after.
- **Timeline page:** every open commitment with a date, soonest first, grouped by month, with countdowns.
  **Today** shows the next three deadlines.

Tests: `tests/unit/test_timeline.py` (TML-01..13), `test_int08` (deadline form, refusal without a reason, Timeline page).

---

# Milestone 1.3 — Plan files, running totals, track-only key results

- **Plan importer:** `lifeos import-plan FILE` or the Plan page. All-or-nothing, imported once per name, and it updates existing goals by code. See [plan-files.md](plan-files.md).
- **`level` key results:** running totals where the latest reading counts (skis ready, cash on hand). No pacing, and they're excluded from weekly hit-rates.
- **Track-only key results:** no target means the number is recorded and shown but never judged.
- **Future phases aren't judged:** a key result before its objective starts reads "not started".
- **Sequenced goals:** quarters before a goal's first planned phase show ⏸️ "before this goal's first phase" rather than "not planned".
  The "no objective" gap only fires when nothing is planned now *or* later.
- **Fix:** the dashboard engine cache is keyed by database location.

Tests: `tests/integration/test_plan_import.py` (IMP-01..06), CAS-21..24, TML-14..15, `test_int09`.
