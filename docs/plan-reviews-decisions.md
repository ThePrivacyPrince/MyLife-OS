# Plan: reviews decide what gets scheduled next

Distilled from three workbooks (*Three Sacred Questions*, *Goal Accelerator*, *10 Commandments 52-Week Challenge*).
The upsells, shame warnings and "never skip a day" rules were left out. What remains are a few tools
adapted to how LIFE OS already works.

**Principle:** LIFE OS must not become another responsibility, so this adds **no new daily ritual**. It upgrades
the routine that already exists ("Review tomorrow's schedule"), the weekly review, and the Plan page.

```
Rules (what I protect)  ->  Decisions (what must be true before a big commitment)
        |                                ^ evidence
30-day review  ->  weekly outcomes  ->  next action each day  ->  tasks and practice worksheets
```

| Phase | What | Status |
|---|---|---|
| 1 | Weekly outcomes, the evening check (3 questions), capacity from the calendar | **Built** |
| 2 | 30-day review: what changed, what kept slipping, one adjustment that becomes a scheduled item | Planned |
| 3 | Rules page (two-pass writing) and decision gates. The first gate is the Oct 1 truck/move decision, with an evidence checklist, recorded reasoning and a later review (the decision journal). | Planned |
| 4 | Practice worksheets on real tasks: negotiation prep, comparing options, 3-month operating costs, delegation trial | Planned |

Left out or deferred:
- a separate daily journal (duplicates the Planner)
- stock picking (not allowed by the money rules yet)
- real-estate analysis (becomes the M2/M5 deal analyzer)
- the 10 business subjects (become the Personal University curriculum)

## Phase 1 (built)

**Weekly outcomes:** up to 3 finish lines a week. Each has:
- a next task
- evidence, which is required to close it as *achieved*
- a follow-up date while it's *waiting* on someone
- an explicit close-out: achieved / partial / missed / dropped, or *carried*

Nothing rolls over silently. Carry streaks are shown ("carried 2×"), and two or more weeks raises "too big?".
The weekly review's "Next week's outcomes" creates them, and old text-only top 3 entries are converted automatically.

**Evening check:** shown at the bottom of Today, and expanded until tomorrow is planned.
1. **Progress:** one action that moves a weekly outcome. It becomes or schedules a real task (planning, not slipping) and sets the outcome's next task.
2. **Negotiation:** one ask to improve price, timing, terms or scope: who, what, and the exact words. "Nothing worth asking" is valid. The ask is a card on Today, not a queued task, because it's a message rather than gated work.
   - Results: got / partly / no / didn't ask, plus what you gained.
   - The weekly review shows wins and gains.
3. **Capacity:** what to protect, waking hours for that day (overnights and practice vary), and which tasks move first if the day is full.
   - If the day turns out overcommitted, **Move them to tomorrow** applies that choice with a recorded reason.

"Plan for: Today" exists for planning after waking from an overnight. Never miss twice: a gentle note appears only after two days in a row without a check.

**Capacity:** `min(cap, awake - calendar - buffer)`.
- Calendar blocks that cross midnight (overnight shifts) are split across both days.
- Settings: `planning.awake_minutes_default` (960), `buffer_minutes` (120), and `discretionary_minutes_per_day`, the most P1/P2 work in a day (180).
- New event kinds: 🏃 training and 👨‍👧 family.

Tests: `tests/unit/test_outcomes_daily.py` (OUT, DCK, CAP), `test_int15`, `test_int16`.
