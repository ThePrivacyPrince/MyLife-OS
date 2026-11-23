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
