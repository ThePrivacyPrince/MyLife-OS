# LIFE OS — notes for coding agents

- Python 3.12+, SQLAlchemy 2, Pydantic 2, Streamlit. Install: `pip install -e ".[dev]"`. Test: `pytest`.
- Business rules live in `lifeos/engines/` (pure) and `lifeos/services/` (DB + audit). The dashboard must not
  contain business logic or write to models directly — call services.
- Every state, priority, date or evidence change must produce a `TaskEvent`. Never update or delete events.
- Keep unknown values unknown (None), never invent them. No financial figures or personal data in Git:
  `data/` is ignored; `seed/` must stay free of amounts (a test enforces no `$`).
- Follow the milestone order in `docs/`. Stop for owner approval between milestones.
- Use decimal arithmetic for any money (Milestone 2+). Never floats.
