# Plan files

A plan file loads a whole chain in one step: vision → goals → objectives → key results → dated tasks.

**Plan files are private.** Keep them in `data/plans/` (git-ignored). Never commit one: they usually
contain personal and financial details.

Import from the **Plan** page (📥 *Import a plan file*) or the terminal:

```bash
lifeos import-plan data/plans/my-plan.yaml
```

- **All or nothing:** if any part fails validation, nothing is written, and the error says which part.
- **Each `plan:` name can be imported once.** After that, edit on the Plan page.
- **A goal with a `code:` that already exists is updated, not duplicated.** If its deadline changes, that's logged as a deadline move with the reason "Imported plan '…'".

## Format

```yaml
plan: My 2027 plan                  # unique name

vision:
  pillar: BUS                       # BUS REL ENV FIN EDU
  target_year: 2030                 # 3-5 years out
  title: Where I want to be
  description: Optional detail

goals:                              # 6-12 months
  - key: first                      # used to link objectives below
    code: BUS-G001                  # optional: update this existing goal instead of creating one
    pillar: BUS
    title: What must be true
    target_date: 2027-05-31         # required - starts the countdown
    baseline: Where I'm starting    # optional
    target_value: The number        # optional
    evidence: How I'll know it's achieved

objectives:                         # about 3 months each (max ~100 days)
  - goal: first
    title: What this quarter delivers
    start: 2026-12-01
    end: 2027-02-28                 # optional, defaults to 91 days
    description: Optional detail
    done_when: What counts as done
    key_results:                    # daily or weekly
      - key: kr1                    # optional, for linking tasks
        title: The measurable thing
        cadence: weekly             # weekly (default) or daily
        measure: manual             # manual | level | completions
        target: 10                  # omit = track only (recorded, never judged)
        unit: sessions              # USD shows as dollars

tasks:                              # dated checklist items
  - title: Something to do
    pillar: BUS                     # default BUS
    priority: 2                     # default 2
    scheduled: 2027-03-01           # don't start before (keeps it from gating today's work)
    due: 2027-03-31
    key_result: kr1                 # optional link up the chain
    notes: Optional
```

### Key result types

| `measure` | Meaning | Example | How you record it |
|---|---|---|---|
| `manual` | Amount for the period | Bookings this week | Check in each amount; they add up per week |
| `level` | Running total; the latest reading counts | Skis ready, cash on hand | Check in the current total, not the change |
| `completions` | Linked tasks done | Paperwork sessions | Complete linked tasks or routines |

A key result in a phase that hasn't started shows "starts Jun 01". It is never "behind".
