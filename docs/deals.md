# Deals (Milestone 5, part A: acquisition intelligence)

The **🛥️ Deals** sidebar page finds, checks and compares used equipment (jet skis first) before you buy.
It implements spec section 10.

**Principles**
- **Unknown facts stay unknown.** Costs with missing parts show as "at least $X", with the missing parts listed.
- **A seller saying "it runs" is never treated as proof.** Only a passed inspection counts.
- **Purchases never come from protected reserves.**

## For each listing
- **Facts:** source, link, make/model/year, asking price, your offer, agreed price, engine hours, hull, mechanical condition (as stated), title status, whether the title was verified in person, trailer, rental suitability, and the seller's own words.
- **Costs:** price + repairs + transport + prep = **ready-to-rent cost**.
- **Price history and listing age:** both are tracked automatically and flagged as **💲 leverage** when the price drops or the listing gets old.
- **Risk flags** (rule-based):
  - 🔴 scam-pattern wording (deposit, shipping, no title)
  - 🔴 missing/salvage/lien title
  - 🔴 price far below similar listings (median of 2 or more of the same model)
  - 🔴 poor hull or a failed critical inspection item
  - 🟠 unverified seller claims, unknown or high hours, no trailer
- **Inspection checklist:** ⭐ critical items (HIN match, compression, water test, hull, pump, bilge, cooling) must pass. Critical items can't be marked N/A.
- **The seven gates (spec 10.3):**
  1. budget ceiling
  2. title verified
  3. inspection passed
  4. suits rentals
  5. total cost known
  6. paid from a non-protected fund that covers the full cost
  7. your approval

  Approving with gates still open requires a recorded override reason.
- **Mark bought:** the unit joins the fleet, and the ready-to-rent cost is drawn from the chosen purchase fund.
- **Next step:** one plain instruction per listing, based on its first open gate.

## Views
- **Best candidates:** ranked listings with full details.
- **Compare:** one table of all active listings.
- **Fleet:** owned units vs. your target; add skis you already own here.
- **Passed:** listings you rejected, each with its reason.

**Ranking:** fewest red-flag risks first, then most gates passed, then fewest cautions, then lowest ready-to-rent cost.
A cheap scam can never outrank a clean listing.

**Settings** (local database only): maximum purchase price per unit, fleet target, and the "high hours" threshold.
The purchase fund lives on **💰 Money → 💼 Business**.

Tests: `tests/unit/test_acquisitions.py` (ACQ-01..12), `test_int21`.

## Pre-purchase protocol and offers (the 💲 Negotiate tab)

**🔧 Inspection protocol**, in the order you'd do it:

📄 Paperwork → 🛥️ On the trailer → ⚙️ Engine bay → 🔑 Engine test → 🌊 Water test → 🚚 Trailer

- Each item is marked ✅ pass, 🔧 fail - fixable (with a **fix-cost estimate**), 🚫 walk away, or n/a. ⭐ critical items can't be n/a.
- Fix costs add up into the listing's repairs.
- A critical item that fails with a priced fix still passes, as **"passed with fixes"**.
- A deal-breaker, or a critical failure with no estimate, fails the inspection.

**💲 Offers**, all shown step by step:
- **Starting point:** the asking price, or what similar units sell for if that's lower. That comes from your market research, or the median of 2+ clean listings of the same model; red-flag listings are never counted.
- **What it's worth to you** = starting point − fix costs.
- **🚫 Walk-away** = the lowest of: what it's worth to you, your price ceiling, and what your purchase fund covers after repairs, transport and prep. Protected reserves never count.
- **The negotiating margin** starts at 8%. Each leverage signal adds to it, up to a maximum of 20%:

  | Signal | Adds |
  |---|---|
  | Listed 30+ days | +3% |
  | Price already dropped | +2% |
  | Off-season (Oct–Mar) | +3% |
  | Cash and quick pickup | +2% |
  | Minor issues found | +2% |
  | High hours | +2% |
- **🔥 Great offer** = walk-away − the full margin (your opening number). **🤝 Fair offer** = walk-away − half the margin. Both are rounded down to $25.
- **Deal-breakers** (missing/salvage/lien title, scam wording, a poor hull, or anything you marked 🚫) mean **no prices at all: walk away**.
- **Unknowns** (asking price, or a fix cost not estimated yet) block the math. It never guesses.
- **Before the inspection is done**, numbers are labeled **preliminary**, and the talk track stays non-committal.

**What to say:** a talk track built from your findings and leverage. The walk-away is marked "for you only".
**📨 Use as tomorrow's ask** puts it in tomorrow's evening check. It only changes the ask; the rest of the plan is kept.
**Offer log:** record each offer and their answer (accepted / countered with an amount / declined / no reply).

Tests: `tests/unit/test_negotiation.py` (NEG-01..11).
