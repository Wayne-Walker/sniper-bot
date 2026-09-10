# Strategic Trading Edge: Analyzing Sentiment Divergence

The table you provided is a "Positioning & Sentiment" report. In institutional trading, this is often called **CoT (Commitment of Traders) Analysis**. It gives you an edge by showing you where the "forced moves" are likely to happen.

---

### 1. Understanding the Core Metrics

To find your edge, you must distinguish between the two groups:

| Metric | Definition | Why it matters for your $10k Account |
| :--- | :--- | :--- |
| **Wallets %** | The number of unique accounts. | High % means a broad consensus; low % means a few whales. |
| **Notional %** | The actual dollar value of the position. | **Crucial:** If 10% of wallets hold 90% Notional, "Smart Money" is heavily concentrated. |
| **Lev (Leverage)** | The multiplier being used. | High leverage (24x) means high sensitivity to price. A small drop could trigger liquidations. |

---

### 2. Identifying the "Edge" in your BTC Data

Based on the snippet: `BTC: Smart Money 89% Notional / 24x Lev vs. Dumb Money 0%`

#### **The "Liquidity Vacuum" Edge**
"Dumb Money" is at **0%**. This means retail traders are currently sidelined or scared. Markets usually top out when retail is "all in" (90%+). Since they aren't in yet, there is a massive amount of "buying fuel" left to enter the market, which could push BTC much higher.

#### **The "Whale Conviction" Edge**
Smart Money isn't just "long"; they are **conviction long**. 
* **89% Notional** means the biggest players have committed almost all their available capital to this trade. 
* **24x Leverage** indicates they expect the move to happen **imminently**. Professionals rarely pay the high funding costs of 24x leverage unless they expect a breakout within 24–48 hours.



---

### 3. Actionable Trading Rules (The "SOP")

Add these logic gates to your bot or manual checklist:

* **Rule 1: Don't Fight the Whale.** Never open a Short position when Smart Money Notional is >70% and Dumb Money is <20%. You will likely get run over by the "Liquidity Train."
* **Rule 2: The "Dumb Money" Signal.** If you see Dumb Money jump from 0% to 50% in one hour, that is your signal that the "top" is forming. Retail is FOMO-ing in, and Smart Money will likely start selling their 89% Notional to them.
* **Rule 3: Watch for the "Long Squeeze".** Because Smart Money is at **24x leverage**, a 4% drop in BTC price could force them to liquidate. 
    * *Strategy:* Don't buy the "Green" (the pump). Set limit orders at the "Liquidation Zones" (roughly 3-5% below current price) to catch the dip if those 24x positions get flushed.

---

### 4. Implementation in your Python Bot
Since you just fixed your `uv` environment, you can now use the Hyperliquid SDK to pull this data automatically.

> **Tip:** Look for the `frontend_liquidations` or `global_positioning` endpoints in the SDK. If you can programmatically calculate the gap between **Smart Notional** and **Dumb Notional**, you can automate your "Edge" by only allowing the bot to trade when the gap is >50%.

**Does the concept of "Dumb Money as Fuel" make sense for your current BTC strategy?**