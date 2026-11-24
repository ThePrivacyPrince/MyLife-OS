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
