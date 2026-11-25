"""Job prep rules (pure): pipeline statuses, the strategy template, requirement extraction, readiness and
matching a posting to University units. No database access."""

from __future__ import annotations

import re
from dataclasses import dataclass

STATUSES = ["researching", "preparing", "applied", "interviewing", "offer", "accepted", "closed"]
STATUS_ICON = {"researching": "🔎", "preparing": "🛠️", "applied": "📨", "interviewing": "🎤", "offer": "🎉",
               "accepted": "✅", "closed": "🗄️"}
OPEN_STATUSES = {"researching", "preparing", "applied", "interviewing", "offer"}
STRENGTHS = ["strong", "partial", "gap", "unknown"]
STRENGTH_ICON = {"strong": "🟢", "partial": "🟡", "gap": "🔴", "unknown": "⚪"}
LINK_KINDS = ["course", "unit", "task", "story", "repo"]

# Big picture first: understand the seat before selling yourself for it.
BIG_PICTURE = [
    ("where", "Where the role sits", "Team, who it reports to, who it serves day to day (e.g. Legal, Sales, Finance), "
     "and what the organization does and sells."),
    ("problem", "The problem it solves", "Why does this seat exist? What breaks, slows down or costs money when it's "
     "empty? Read between the lines of the posting: volume, speed, risk, attorney time."),
    ("success", "What success looks like", "How they'll judge the hire after 90 days: metrics (cycle time, backlog, "
     "errors), relationships, and what the manager stops having to do."),
    ("bigger", "How it connects to the bigger picture", "How this role helps the company's goals (revenue closes "
     "faster, less risk, audit-ready) and where it could grow."),
]
BIG_PICTURE_KEYS = [k for k, _, _ in BIG_PICTURE]

# The strategy is written once per role, section by section. Prompts say what a good answer looks like.
STRATEGY = [
    ("needs", "Their top 3 needs", "What problem is this hire solving? Read the posting twice; name the 3 things that matter most."),
    ("pitch", "My one-line pitch", "Who you are for THIS role, in one sentence they'd repeat to the hiring manager."),
    ("proof", "Proof points", "Your strongest evidence, with numbers where you have them. Link repos and portfolio pieces below."),
    ("gaps", "Gaps and how I'm closing them", "Be honest. For each gap: what you're doing now (a course unit, a project) and how you'll say it."),
    ("stories", "Stories to tell", "Which stories from your bank answer their likely questions? Link them below."),
    ("questions", "Questions to ask them", "Questions that show you understand the job: workflow, volume, tools, what success looks like."),
    ("plan", "30-60-90 day plan", "What you'd learn, fix and own in your first three months."),
    ("company", "Company research", "What they sell, who buys it, recent news, the team, the tools they use."),
    ("logistics", "Logistics", "Pay range, location, schedule, start date, anything that affects your other commitments."),
]
STRATEGY_KEYS = [k for k, _, _ in STRATEGY]
SECTION_KEYS = BIG_PICTURE_KEYS + STRATEGY_KEYS  # everything stored in JobRole.strategy

# ---------------------------------------------------------------- automation ideas
EFFORTS = ["small", "medium", "large"]
IMPACTS = ["high", "medium", "low"]
IDEA_STATUSES = ["idea", "prototype", "demo-ready", "parked"]
IDEA_ICON = {"idea": "💡", "prototype": "🧪", "demo-ready": "🚀", "parked": "💤"}
# Questions that surface automation in almost any role.
AUTOMATION_PROMPTS = [
    "What gets copied by hand from one system to another (email → tracker, PDF → spreadsheet)?",
    "Where do requests arrive unstructured, so someone has to chase missing information?",
    "What is checked by hand against a list or playbook every time?",
    "Which reminders depend on someone remembering (renewals, deadlines, follow-ups)?",
    "Which reports or status updates are assembled manually each week?",
    "Where do approvals stall because nobody knows whose turn it is?",
    "What questions does the team answer over and over (a template or FAQ would do)?",
]

_BULLET = re.compile(r"^\s*(?:[-*•·▪◦●]|\d{1,2}[.)])\s+(.*\S)\s*$")
_TAG = re.compile(r"(?<![\w&])#([A-Za-z][\w-]{1,40})")


def extract_requirements(posting: str, limit: int = 25) -> list[str]:
    """Bullet lines from a pasted job posting - the usual shape of 'responsibilities' and 'qualifications'."""
    out: list[str] = []
    for line in (posting or "").splitlines():
        m = _BULLET.match(line)
        if m:
            text = m.group(1).strip().rstrip(";.")
            if 8 <= len(text) <= 300 and text.lower() not in {o.lower() for o in out}:
                out.append(text)
    return out[:limit]


def tags_in(text: str) -> list[str]:
    """#hashtags written in a note become its tags (lowercased, unique, in order)."""
    seen: list[str] = []
    for t in _TAG.findall(text or ""):
        t = t.lower()
        if t not in seen:
            seen.append(t)
    return seen


@dataclass(frozen=True)
class Readiness:
    requirements: float | None   # 0..1, None when no requirements are listed
    study: float | None          # 0..1 of linked units applied or mastered, None when nothing is linked
    counts: dict

    @property
    def overall(self) -> float | None:
        parts = [p for p in (self.requirements, self.study) if p is not None]
        return sum(parts) / len(parts) if parts else None


def readiness(strengths: list[str], unit_levels: list[str]) -> Readiness:
    """How ready you are, from evidence only: proven requirements and applied study. Unknown stays unknown."""
    weight = {"strong": 1.0, "partial": 0.5, "gap": 0.0, "unknown": 0.0}
    counts = {k: strengths.count(k) for k in STRENGTHS}
    req = sum(weight[x] for x in strengths) / len(strengths) if strengths else None
    study = (sum(lv in ("applied", "mastered") for lv in unit_levels) / len(unit_levels)) if unit_levels else None
    return Readiness(req, study, counts)


def _term_in(term: str, text: str) -> bool:
    t = term.strip()
    if not t or len(t) < 3:
        return False
    flags = 0 if (t.isupper() or any(ch.isupper() for ch in t[1:])) else re.IGNORECASE
    return re.search(rf"(?<![\w]){re.escape(t)}(?:s|es)?(?![\w])", text, flags) is not None  # MSA -> MSAs


def match_units(posting: str, cards: list[dict]) -> list[tuple[str, list[str]]]:
    """University units whose flashcard terms appear in the posting, best match first: [(unit_key, [terms])].
    Acronyms match case-sensitively (so 'MSA' doesn't match 'msa' inside another word)."""
    hits: dict[str, list[str]] = {}
    for c in cards:
        variants = [p.strip() for p in re.split(r"\s+(?:vs|/)\s+|\s*/\s*", c["term"]) if p.strip()] + [c["term"]]
        for v in dict.fromkeys(variants):
            if _term_in(v, posting or ""):
                hits.setdefault(c["unit"], [])
                if c["term"] not in hits[c["unit"]]:
                    hits[c["unit"]].append(c["term"])
                break
    return sorted(hits.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def idea_score(impact: str, effort: str) -> int:
    """Higher = do first. High impact for small effort is a quick win (9); low impact, large effort is last (1)."""
    return {"high": 3, "medium": 2, "low": 1}[impact] * {"small": 3, "medium": 2, "large": 1}[effort]


def is_quick_win(impact: str, effort: str) -> bool:
    return impact == "high" and effort == "small"
