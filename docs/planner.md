# Planner

The **Planner** page gathers everything with a date: classes, study blocks, shifts, appointments, imported
calendar items, tasks, routines, and goal/objective deadlines.

## Views (◀ Today ▶ to move)
- **Agenda:** day by day (Today, Tomorrow, Wed…), with "Nothing planned" for empty days.
  - Tick tasks done with ○.
  - Overdue work always surfaces on today.
- **Week:** seven columns, plus **Plan the week**: give each unscheduled task a day. Each one defaults to its due date if that falls this week.
  - Putting a task on a day is *planning*, not slipping.
  - Moving work that was already due today (or earlier) to later *is* a slip, and needs a reason.
- **Month:** a calendar grid; › opens any day that has something on it.
- **Weekly review:** see below.

## Adding to the calendar
- **➕ Add an event:** one-off, timed or all-day. Kinds are class, study, work, appointment, exam, deadline, personal and other.
- **🎓 Add a class schedule:** repeating days and times for a term (e.g. Contracts, Mon/Wed 9:00-10:30). You can skip a single day (e.g. a holiday) or end the series.
- **📥 Import a calendar (.ics):**
  - *Canvas:* Calendar → Calendar Feed. *Google Calendar:* Settings → Import & export.
  - Times convert to your timezone, and repeating events expand inside a window (30 days back, 240 days ahead).
  - Re-importing updates items instead of duplicating them.
  - Exams and due dates are recognised automatically.
  - **Make it a task** turns any item into a task due that day.

## Weekly review (Sunday; the Monday after if missed)
A reminder appears on Today and in the Planner when it's due. It won't appear for weeks before you started using LIFE OS.

1. **Scorecard from the records:**
   - tasks completed, P1 done %, routine adherence %
   - key results met, class + study hours from the calendar
   - reschedules and priority overrides

   Only finished days are scored, so today's unfinished work hasn't "slipped" yet. Protected commitments (sleep, shifts) are never scored.
2. **Got done / Slipped / missed routines**, listed automatically.
3. **Coming up:** deadlines and due tasks in the next two weeks.
4. **Reflect:**
   - wins
   - what slipped and *why* (design, not discipline)
   - lessons, and an energy rating from 1 to 5
5. **Next week's top 3:** shown on Today and in the Planner all next week.
6. **Past weeks:** a table to spot trends.

Settings: `config/settings.yaml → planner` (review day, agenda length, import window).

## Editing tasks and routines
- **✏️ Edit** is the first tab under **More** on every task card (Today, All tasks), and the ✏️ pop-up on every task in the Planner agenda.
- **One form covers everything:** title, notes, pillar, priority, work day, due date, estimate, goal, key result, evidence required and protected.
- **A reason is only asked for when it matters:**
  - changing priority
  - moving or removing an existing due date
  - pushing back work that was already due

  Setting a first due date, or planning a work day ahead, is free.
- **One edit, one transaction:** if any part is refused, nothing is saved. Every change lands in the task's history.
- **Routines:** **Recurring → ✏️ Edit routine** changes the name, pillar, priority, schedule, estimate, missed-day rule, end date or protection. Changes apply to today's open occurrence and all future ones; past days stay as they happened.
- **No hard delete:** a mistaken task is *cancelled* with a reason, so the record stays honest.

## All tasks: organized by pillar
**All tasks** opens in **By pillar**: five drop-down sections (Business, Relationships, Environment, Finances,
Education). Each header shows open / P1 / overdue counts, e.g. "💼 Business · 3 open · 1 P1 · 1 overdue".
Sections with overdue work, or a P1 due this week, open automatically.

Inside each pillar, tasks are grouped by when they need to happen:

🔴 Overdue → 📅 This week → 🗓️ Later → 📥 No date → ⏳ Waiting / blocked.

Within a group they're sorted P1 first, then by due date.

Every row has ○ to mark it done and the ✏️ edit pop-up. The priority gate still applies.

- **Board:** the five pillars side by side as columns, each group a drop-down.
- **Table:** the original sortable list.
- **Not listed:** protected commitments (sleep, shifts).
- **Sidebar filters** still work: pillar, and one-time vs recurring.

## Timeline: every pillar, three views
The top of the page shows four counts (next 30 days, next 12 months, goals without a date, deadlines moved),
then a **Coming up** strip with the nearest four deadlines across all pillars.

- **By pillar** (default): a drop-down per pillar. The header gives the count, the next deadline, and how many goals have no date.
  - Pillars with something due within 30 days, or past due, open automatically.
  - **Goals without a deadline** can be given one right there: pick a date, then **Set**. A first deadline needs no reason.
- **Roadmap:** deadlines grouped by quarter, then month. Only this quarter and next open; later quarters stay collapsed.
- **Chart:** one bar per goal and objective, from start to deadline, colored by pillar, with a dotted *today* line. Hover for details.
  - Visions are hidden by default (they stretch the scale to years). Tick **Include 3-5 year visions** to add them.
  - The pillar colors were checked for color-blind safety. Each bar is labeled, so color is never the only cue.

Task due dates stay in the Planner and All tasks, so the Timeline remains about the big commitments.
