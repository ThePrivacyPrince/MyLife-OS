# Studio (content creation)

The **🎬 Studio** sidebar page runs content creation like a job. It's deliberately small: shoot, post, work with more brands.

- **Content:** each video or post moves 💡 idea → 📋 planned → 🎬 shot → ✂️ edited → ✅ posted.
  - Each piece holds a brand (or none, for your own content), shoot and post dates, platform, concept and shot list, and the post link.
  - **Brand content can't be marked posted until you confirm the disclosure** (#gifted / #ad or the platform's paid-partnership label).
- **Brands:** status 🎯 prospect / 🎁 gifted / 💬 talking / 💰 paid / 💤 paused, a contact (person, email or PR agency), notes and a next follow-up date.
  - **Posting brand content sets a follow-up 3 days later** (if none is set), so every post leads to the next conversation: ask about launches and paid work.
  - **Done +14d:** you've followed up, so the next check-in is set for 14 days out.
- **Needs you today:** shoots due, posts due and follow-ups due. **Today** shows the same counts.
- **This month:** posts published, pieces in production, active brands and paying brands. Only real counts, never estimates.
- **History:** status and date changes are logged (append-only).

Brands and content live only in your local database. Tests: `tests/unit/test_studio.py` (STU-01..03), `test_int26`.
