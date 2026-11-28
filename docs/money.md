# Money

The **💰 Money** sidebar page tracks money you set aside, pay down and spend. LIFE OS **tracks** money; it never
moves it, so record what you actually did. All amounts are exact decimals and live only in your local database.

## Separate tabs, so nothing gets mixed
| Tab | Holds | The bar shows |
|---|---|---|
| 👦 Family | Spending on your son (with an optional monthly budget), or savings for him | This month vs budget, or saved vs target |
| 🎄 Holidays & gifts | Christmas and gift funds | Saved vs target, by the deadline |
| 🎁 Wants | Your wish list | Saved vs price, then a **14-day cool-off** |
| 🛡️ Reserves | Opening cushion, tax reserve, emergency fund | Saved vs target |
| 💳 Money I owe | Parents, a former partner, credit cards | Paid down vs owed (new charges add to what's owed) |

Debts can only live in **Money I owe**, so they never sit next to your son's fund.

## Each fund
- **Quick buttons:**
  - savings: *Set aside* / *Take out* (taking out needs a reason)
  - debts: *Paid* / *New charge* (needs a note)
  - spending: *Log spending*
- **Pace:** with a deadline, it shows "$X/week gets you there", on track or behind pace, and an estimate at your recent pace.
- **More:** history, edit (moving a deadline needs a reason), *Bought it* (records the real price), and *Drop*. A dropped want counts its price as **kept**.
- **Debts close themselves** when paid off.

## Frugality tools
- **🧠 Before you buy:** enter an item and its price to see the **cost in hours of work** (set your take-home hourly rate under ⚙️ Settings). Then choose:
  - **Skip it:** the money counts toward *Kept by not buying*.
  - **Wish list:** it becomes a Wants fund with a 14-day cool-off once it's saved.
  - **Buying it:** logged.
- **🧊 Cool-off:** a want unlocks *Bought it* 14 days after it's fully saved. Dipping below the target restarts the clock. Buying earlier is allowed, but the reason is recorded.
- **💵 Got paid? Split it:** suggests how to divide money as it comes in, in this order: debts → reserves → family → holidays → wants.
  - Funds with a deadline are offered their weekly pace; the rest are left for you to fill in.
  - You adjust the amounts, then *Record this split*.
- **⏸️ Before wants:** the Wants tab notes any debt or reserve that's behind pace.
- **Cash check:** report your actual cash under ⚙️ Settings, and the page warns if your funds add up to more than you have.
- **Weekly review** adds one line: set aside, paid down, tracked spending, kept by not buying.

Setting: `config/settings.yaml → money.cooloff_days` (14).
Tests: `tests/unit/test_money.py` (MNY-01..11), `test_int20`.

## 🎯 Debt playbook (Money → Debt playbook tab)
A payoff plan built on your **💳 Money I owe** debts, plus eight plays that hold you to it.

**Each debt's terms:** APR, minimum, due day and autopay. Unknown terms stay blank, and that debt stays out of the plan until you fill them in, so the plan is never built on guesses.

**The plan:**
- **How it works:** every minimum gets paid. Your committed **extra per month** plus your **micro-payments** go to one **target** debt. When that debt is paid off, its minimum rolls into the next target.
- **Strategies:** **🏔️ Avalanche** (highest APR first, least interest) or **⛄ Snowball** (smallest balance first, quickest first win). Both are shown side by side against **minimums only**: debt-free month, total interest and first payoff.
- **🎯 Make it a goal:** creates a FIN goal, "Debt-free by …", which counts down on Plan and Timeline.
- **Assumptions:** no new charges; you keep paying at least today's minimum; interest charged monthly at APR/12. All amounts are exact decimals.

**The honest micro-payment math:** for your target debt, it compares minimums only, the same extra money paid once a month, and that money paid as small amounts every N days. Paying **more** is most of the saving; paying **more often** adds a little, from a lower average daily balance.

**The eight plays** (each shows ✅ done / ⬜ to do / ⚠️ warning, worked out from your data):
1. 📋 **Know every number:** APR, minimum and due day for every debt.
2. 🛡️ **Never miss a minimum:** autopay on, or a reminder 3 days before the due date. A missed last cycle shows on Today.
3. 🎯 **One target at a time:** avalanche or snowball, chosen on purpose.
4. ☕ **Micro-payments:** a small amount every N days, with one tap on **Today** to log it to the target.
5. 💵 **Payday rule:** a set % of every paycheck or windfall. **Got paid? Split it** suggests it for your target debt automatically.
6. 🧊 **Stop adding to it:** flags new charges in the last 30 days. Statement interest doesn't count as a new charge.
7. 📞 **Ask for a lower rate:** for any debt at 20%+ APR, creates a FIN task with a call script (NEG101).
8. 🧾 **Monthly statement check:** enter the statement balance. The difference is logged as interest and fees (or as a credit), so balances stay real.

**Today shows:** minimums due soon or missed, and the ☕ micro-payment button on micro days.
Research behind the strategies: Amar et al. (2011) and Gathergood et al. (2019), both in the University library (OWN101, unit 2).
Tests: `tests/unit/test_debt_plan.py` (DEBT-01..10), `test_int27`.
