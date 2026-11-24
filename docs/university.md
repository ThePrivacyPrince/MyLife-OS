# The Personal University (Milestone 5, part B)

The **🎓 University** sidebar page holds the Education pillar: 8 departments, 12-week semesters run on the
"Rule of One", and grades that reflect demonstrated knowledge, never hours spent.

## Content vs progress
- **Course content** (units, objectives, sources, quizzes, flashcards, checklists) is general education. It lives in
  `curriculum/*.yaml`, in Git.
- **Your progress** (recall notes, answers, card schedule, evidence, reading log, inquiries, exam scores) lives only
  in your local database.

## No fabricated sources (a test enforces this)
- **Papers** are cited by authors, year, title and venue, never with a guessed link. Find them on Google Scholar.
- **Web links** may only point to an allow-listed set of official, stable domains (IRS, CFPB, NIST, Python docs, HashiCorp, OPA and similar).
- **"✔ checked":** every source has one, so you can mark it once you've opened it and confirmed it's what it says.

## Departments
🧠 Thinking & Decisions · 💵 Money & Ownership · 💼 Business & Operations · ⚖️ Law & Argument ·
🛠️ GRC Engineering & Technology · 🎙️ Leadership & Communication · 🌍 History & Civilization (no limit) · 🔧 Life Skills

## Semester 1: Ownership, Asks & First Code (Dec 7 – Feb 28)
Each semester has a hard skill, a human skill and an anchor. That's about 4 hours a week, plus 10 minutes of flashcards a day.

| Course | Role | Type | Units |
|---|---|---|---|
| OWN101 Ownership & Capital | hard | studio | cash flow · debt · credit · taxes · unit economics · investing |
| NEG101 Negotiation Lab | human | studio | preparation · anchors · tactical empathy · concessions (reps through evening-check asks and Deals) |
| SYS101 Systems Engineering for Operators | anchor | studio | Python · APIs · OPA/Rego · Terraform + policy, each mapped to an ISO 27001 / SOC 2 control |

## Every unit: Learn → Recall → Test → Apply → Cards
- **📖 Learn:** objectives, a short orientation, and research papers, books, courses and official guides.
- **🧠 Recall:** write what you remember without looking, then tick the key points you actually got.
- **✅ Test:** multiple choice is auto-graded with explanations. Short answers are checked by you against the key points; at least half must be hit.
- **🔨 Apply:** the real assignment, with evidence of what you did and where the proof is. **Studio units pass only when the thing runs.**
- **🃏 Cards:** definitions and acronyms. Starting a unit adds them to your reviews.

## Mastery is earned
⚪ not started → 🔹 introduced → 🔸 practiced → 🟡 **recalled** (correct 7+ days after you started) → 🟢 **applied** (evidence)
→ 🏅 **mastered** (applied, recalled, and correct reviews at least 21 days apart). Opening a lesson earns nothing.

## Spaced review
- **Schedule:** cards come back after 1 → 3 → 7 → 14 → 30 → 60 → 120 days. A miss sends a card back to tomorrow.
- **Daily budget:** about 10 minutes (around 20 cards). On a tight day (under 60 minutes of capacity, e.g. overnights or practice), only the 8 most overdue are shown.
- **Today** shows "🧠 N flashcards due".

## Also on the page
- **Catalog:** all departments and planned courses.
- **Library:** every source, plus a **reading log** (three takeaways and one real action per book).
- **History:** unlimited inquiries. A question, two or more independent sources, your own conclusion, and what would change your mind. Starts with Haiti (revolution, the 1825 indemnity), the Great Migration, world history, the history of money, and personal watercraft.
- **Exams:** Terraform Associate (S2), ISO/IEC 27001 Lead Implementer (S3), LSAT (its own semester, once you choose a law-school timeline), CISA (optional). Set a test date and target, then log practice scores.
- **Hands-on:** checklists with safety notes, done when needed (paint a room; WaveRunner pre-ride and after-ride flush). Every step must be ticked, plus evidence.
- **Report card:** applied and mastered units, quiz accuracy, and 30-day card recall per course.

Later: the AI faculty (dean, examiner, librarian) comes with Milestone 4. Everything here works without AI.
Tests: `tests/unit/test_university.py` (UNI-01..11), `test_int22`, `test_int23`.
