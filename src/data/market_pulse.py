"""
Market Pulse — Macro market data from free APIs (no keys needed).

Sources:
  • DefiLlama ......... Stablecoin market caps & inflows, DeFi TVL (chains + total)
  • CoinGecko ......... Total crypto market cap, BTC dominance, top coin prices
  • Alternative.me .... Crypto Fear & Greed Index

Signals derived:
  - Stablecoin inflows  → capital entering crypto (bullish)
  - Stablecoin outflows → capital leaving crypto (bearish)
  - TVL rising          → capital working in DeFi (bullish, esp. alts)
  - TVL falling         → capital leaving DeFi (bearish, esp. alts)
  - BTC dominance rising → risk-off / alts underperform
  - Fear & Greed extremes → contrarian signals
  - Market cap 24h change → trend confirmation
"""

import requests
from dataclasses import dataclass, field
from typing import Any, List, Optional
from src.utils.logger import get_logger

log = get_logger(__name__)

TIMEOUT = 15  # seconds per request


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class StablecoinData:
    total_mcap:      float = 0.0   # USD
    mcap_1d_change:  float = 0.0
    mcap_7d_change:  float = 0.0
    mcap_30d_change: float = 0.0
    usdt_mcap:       float = 0.0
    usdt_1d_change:  float = 0.0
    usdc_mcap:       float = 0.0
    usdc_1d_change:  float = 0.0
    dai_mcap:        float = 0.0
    dai_1d_change:   float = 0.0


@dataclass
class MarketOverview:
    total_mcap:            float = 0.0
    total_mcap_change_24h: float = 0.0
    total_volume_24h:      float = 0.0
    btc_dominance:         float = 0.0
    eth_dominance:         float = 0.0
    active_cryptos:        int   = 0


@dataclass
class CoinPrice:
    symbol:     str   = ""
    price:      float = 0.0
    mcap:       float = 0.0
    volume_24h: float = 0.0
    change_24h: float = 0.0
    change_7d:  float = 0.0


@dataclass
class FearGreed:
    value:            int = 0    # 0-100
    label:            str = ""
    yesterday_value:  int = 0
    last_week_value:  int = 0


@dataclass
class ChainTVL:
    name:   str   = ""
    tvl:    float = 0.0          # USD
    symbol: str   = ""


@dataclass
class TVLData:
    total:           float = 0.0           # USD across all chains
    change_24h_usd:  float = 0.0
    change_7d_usd:   float = 0.0
    change_30d_usd:  float = 0.0
    change_24h_pct:  float = 0.0
    change_7d_pct:   float = 0.0
    change_30d_pct:  float = 0.0
    top_chains:      List[ChainTVL] = field(default_factory=list)  # ranked desc


@dataclass
class MarketPulse:
    stablecoins: StablecoinData = field(default_factory=StablecoinData)
    market:      MarketOverview = field(default_factory=MarketOverview)
    coins:       List[CoinPrice] = field(default_factory=list)
    fear_greed:  FearGreed = field(default_factory=FearGreed)
    tvl:         TVLData = field(default_factory=TVLData)
    errors:      List[str] = field(default_factory=list)


# ── Stablecoins (DefiLlama) ──────────────────────────────────────────────────

def fetch_stablecoin_data() -> StablecoinData:
    log.info("Fetching stablecoin data (DefiLlama)...")
    resp = requests.get(
        "https://stablecoins.llama.fi/stablecoins?includePrices=true",
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    result = StablecoinData()
    tracked = {"USDT": "usdt", "USDC": "usdc", "DAI": "dai"}

    for asset in data.get("peggedAssets", []):
        if asset.get("pegType") != "peggedUSD":
            continue

        current    = _pegged(asset.get("circulating"))
        prev_day   = _pegged(asset.get("circulatingPrevDay"))
        prev_week  = _pegged(asset.get("circulatingPrevWeek"))
        prev_month = _pegged(asset.get("circulatingPrevMonth"))

        result.total_mcap      += current
        result.mcap_1d_change  += (current - prev_day)
        result.mcap_7d_change  += (current - prev_week)
        result.mcap_30d_change += (current - prev_month)

        sym = asset.get("symbol", "")
        if sym in tracked:
            prefix = tracked[sym]
            setattr(result, f"{prefix}_mcap", current)
            setattr(result, f"{prefix}_1d_change", current - prev_day)

    log.info(
        f"  Stablecoin total: ${result.total_mcap/1e9:,.1f}B  "
        f"1d: ${result.mcap_1d_change/1e6:+,.0f}M  "
        f"7d: ${result.mcap_7d_change/1e6:+,.0f}M"
    )
    return result


def _pegged(obj) -> float:
    if not isinstance(obj, dict):
        return 0.0
    return float(obj.get("peggedUSD", 0) or 0)


# ── Global market data (CoinGecko) ───────────────────────────────────────────

def fetch_market_overview() -> MarketOverview:
    log.info("Fetching global market data (CoinGecko)...")
    resp = requests.get(
        "https://api.coingecko.com/api/v3/global",
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})

    mcap = data.get("total_market_cap", {}) or {}
    vol  = data.get("total_volume", {}) or {}
    dom  = data.get("market_cap_percentage", {}) or {}

    overview = MarketOverview(
        total_mcap            = mcap.get("usd", 0) or 0,
        total_mcap_change_24h = data.get("market_cap_change_percentage_24h_usd", 0) or 0,
        total_volume_24h      = vol.get("usd", 0) or 0,
        btc_dominance         = dom.get("btc", 0) or 0,
        eth_dominance         = dom.get("eth", 0) or 0,
        active_cryptos        = data.get("active_cryptocurrencies", 0) or 0,
    )
    log.info(
        f"  Total mcap: ${overview.total_mcap/1e12:.2f}T  "
        f"24h: {overview.total_mcap_change_24h:+.2f}%  "
        f"BTC dom: {overview.btc_dominance:.1f}%"
    )
    return overview


def fetch_coin_prices() -> List[CoinPrice]:
    log.info("Fetching coin prices (CoinGecko)...")
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        "?vs_currency=usd&ids=bitcoin,ethereum,ripple,solana"
        "&order=market_cap_desc"
        "&price_change_percentage=24h,7d"
    )
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()

    coins = []
    for c in resp.json():
        coins.append(CoinPrice(
            symbol     = (c.get("symbol") or "").upper(),
            price      = c.get("current_price") or 0,
            mcap       = c.get("market_cap") or 0,
            volume_24h = c.get("total_volume") or 0,
            change_24h = c.get("price_change_percentage_24h") or 0,
            change_7d  = c.get("price_change_percentage_7d_in_currency") or 0,
        ))
    for c in coins:
        log.info(
            f"  {c.symbol}: ${c.price:,.2f}  "
            f"24h: {c.change_24h:+.1f}%  7d: {c.change_7d:+.1f}%"
        )
    return coins


# ── DeFi TVL (DefiLlama) ─────────────────────────────────────────────────────

def fetch_tvl_data() -> TVLData:
    """
    Fetch DeFi TVL: per-chain breakdown + historical for 24h/7d/30d changes.
    Two endpoints (both free, no key):
      - https://api.llama.fi/v2/chains            (current TVL per chain)
      - https://api.llama.fi/v2/historicalChainTvl (daily total TVL series)
    """
    log.info("Fetching DeFi TVL (DefiLlama)...")

    chains_resp = requests.get("https://api.llama.fi/v2/chains", timeout=TIMEOUT)
    chains_resp.raise_for_status()
    chains = chains_resp.json() or []

    hist_resp = requests.get("https://api.llama.fi/v2/historicalChainTvl", timeout=TIMEOUT)
    hist_resp.raise_for_status()
    hist = hist_resp.json() or []

    result = TVLData()

    # Current total + top chains
    result.total = sum(c.get("tvl", 0) or 0 for c in chains)
    ranked = sorted(chains, key=lambda c: c.get("tvl", 0) or 0, reverse=True)
    result.top_chains = [
        ChainTVL(
            name   = c.get("name", "?"),
            tvl    = c.get("tvl", 0) or 0,
            symbol = c.get("tokenSymbol") or "",
        )
        for c in ranked[:5]
    ]

    # Changes from historical series
    if hist and len(hist) >= 2:
        latest = hist[-1].get("tvl", 0) or 0
        # The /v2/chains aggregate can differ slightly from /historicalChainTvl
        # for "today" (intraday). Use historical for the present too, so deltas
        # are consistent.
        result.total = latest

        def delta_at(n_days_back: int):
            if len(hist) > n_days_back:
                prior = hist[-1 - n_days_back].get("tvl", 0) or 0
                return latest - prior, (latest - prior) / prior * 100 if prior else 0.0
            return 0.0, 0.0

        result.change_24h_usd, result.change_24h_pct = delta_at(1)
        result.change_7d_usd,  result.change_7d_pct  = delta_at(7)
        result.change_30d_usd, result.change_30d_pct = delta_at(30)

    log.info(
        f"  TVL total: ${result.total/1e9:,.1f}B  "
        f"24h: {result.change_24h_pct:+.2f}%  "
        f"7d: {result.change_7d_pct:+.2f}%  "
        f"30d: {result.change_30d_pct:+.2f}%"
    )
    return result


# ── Fear & Greed (Alternative.me) ────────────────────────────────────────────

def fetch_fear_greed() -> FearGreed:
    log.info("Fetching Fear & Greed Index (Alternative.me)...")
    resp = requests.get("https://api.alternative.me/fng/?limit=8", timeout=TIMEOUT)
    resp.raise_for_status()
    entries = resp.json().get("data", [])

    fg = FearGreed()
    if entries:
        fg.value = int(entries[0].get("value", 0))
        fg.label = entries[0].get("value_classification", "")
    if len(entries) >= 2:
        fg.yesterday_value = int(entries[1].get("value", 0))
    if len(entries) >= 8:
        fg.last_week_value = int(entries[7].get("value", 0))

    log.info(
        f"  F&G: {fg.value} ({fg.label})  "
        f"yesterday: {fg.yesterday_value}  last week: {fg.last_week_value}"
    )
    return fg


# ── Main entry point ─────────────────────────────────────────────────────────

def fetch_market_pulse() -> MarketPulse:
    """Fetch all macro data. Each source fails independently."""
    pulse = MarketPulse()

    for name, fn, attr in [
        ("Stablecoins",  fetch_stablecoin_data, "stablecoins"),
        ("Market overview", fetch_market_overview, "market"),
        ("Coin prices",  fetch_coin_prices, "coins"),
        ("DeFi TVL",     fetch_tvl_data, "tvl"),
        ("Fear & Greed", fetch_fear_greed, "fear_greed"),
    ]:
        try:
            setattr(pulse, attr, fn())
        except Exception as e:
            log.error(f"{name} fetch failed: {e}")
            pulse.errors.append(f"{name}: {type(e).__name__}")

    return pulse


# ── Formatting for the briefing ──────────────────────────────────────────────

def format_market_pulse(pulse: MarketPulse) -> str:
    lines = [
        "*MARKET PULSE*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Fear & Greed
    fg = pulse.fear_greed
    if fg.value:
        delta_yesterday = fg.value - fg.yesterday_value if fg.yesterday_value else 0
        delta_week      = fg.value - fg.last_week_value  if fg.last_week_value else 0
        delta_yest_str  = f" ({delta_yesterday:+d}d)" if delta_yesterday else ""
        delta_week_str  = f" / week `{delta_week:+d}`" if delta_week else ""
        lines.append(
            f"  {_fg_emoji(fg.value)}  *F&G* `{fg.value}` "
            f"{fg.label}{delta_yest_str}{delta_week_str}"
        )
        lines.append("")

    # Market overview
    m = pulse.market
    if m.total_mcap:
        lines.append(
            f"*Total Mcap:* `${m.total_mcap/1e12:.2f}T`  "
            f"(`{m.total_mcap_change_24h:+.1f}%` 24h)"
        )
        lines.append(f"*24h Vol:*  `${m.total_volume_24h/1e9:.1f}B`")
        lines.append(
            f"*BTC Dom:* `{m.btc_dominance:.1f}%`  |  "
            f"*ETH Dom:* `{m.eth_dominance:.1f}%`"
        )
        lines.append("")

    # Prices
    if pulse.coins:
        lines.append("*Prices*")
        for c in pulse.coins:
            arrow = "🟢" if c.change_24h >= 0 else "🔴"
            lines.append(
                f"  {arrow} *{c.symbol}* `{_fmt_price(c.price)}`  "
                f"24h:`{c.change_24h:+.1f}%`  7d:`{c.change_7d:+.1f}%`"
            )
        lines.append("")

    # DeFi TVL
    tvl = pulse.tvl
    if tvl.total:
        a24 = "🟢" if tvl.change_24h_pct >= 0 else "🔴"
        a7  = "🟢" if tvl.change_7d_pct  >= 0 else "🔴"
        a30 = "🟢" if tvl.change_30d_pct >= 0 else "🔴"
        lines.append("*DeFi TVL* (capital working in DeFi)")
        lines.append(f"  Total: `${tvl.total/1e9:,.1f}B`")
        lines.append(
            f"  {a24} 24h: `{tvl.change_24h_pct:+.2f}%`  "
            f"{a7} 7d: `{tvl.change_7d_pct:+.2f}%`  "
            f"{a30} 30d: `{tvl.change_30d_pct:+.2f}%`"
        )
        if tvl.top_chains:
            top_str = "  ".join(
                f"{c.name} `${c.tvl/1e9:.1f}B`"
                for c in tvl.top_chains[:5]
            )
            lines.append(f"  By chain: {top_str}")
        lines.append("")

    # Stablecoin flows
    sc = pulse.stablecoins
    if sc.total_mcap:
        a1  = "🟢" if sc.mcap_1d_change  >= 0 else "🔴"
        a7  = "🟢" if sc.mcap_7d_change  >= 0 else "🔴"
        a30 = "🟢" if sc.mcap_30d_change >= 0 else "🔴"
        lines.append("*Stablecoin Flows* (capital in/out of crypto)")
        lines.append(f"  Total supply: `${sc.total_mcap/1e9:.1f}B`")
        lines.append(
            f"  {a1} 1d: `${sc.mcap_1d_change/1e6:+,.0f}M`  "
            f"{a7} 7d: `${sc.mcap_7d_change/1e6:+,.0f}M`  "
            f"{a30} 30d: `${sc.mcap_30d_change/1e9:+,.1f}B`"
        )
        for sym, prefix in [("USDT", "usdt"), ("USDC", "usdc"), ("DAI", "dai")]:
            mcap = getattr(sc, f"{prefix}_mcap", 0)
            d1   = getattr(sc, f"{prefix}_1d_change", 0)
            if mcap:
                arrow = "▲" if d1 >= 0 else "▼"
                lines.append(
                    f"  {sym}: `${mcap/1e9:.1f}B`  "
                    f"{arrow} `${d1/1e6:+,.0f}M` (1d)"
                )
        lines.append("")

    # Derived signals
    signals = _derive_signals(pulse)
    if signals:
        lines.append("*Macro Signals*")
        for s in signals:
            lines.append(f"  {s}")
        lines.append("")

    if pulse.errors:
        lines.append("_⚠️ Some data unavailable:_")
        for err in pulse.errors:
            lines.append(f"  • {err}")

    return "\n".join(lines)


def _fg_emoji(value: int) -> str:
    if value <= 20: return "😱"
    if value <= 40: return "😟"
    if value <= 60: return "😐"
    if value <= 80: return "😏"
    return "🤑"


def _fmt_price(price: float) -> str:
    if price >= 1000: return f"${price:,.0f}"
    if price >= 1:    return f"${price:,.2f}"
    return f"${price:,.4f}"


def _derive_signals(pulse: MarketPulse) -> List[str]:
    out = []
    sc, fg, m = pulse.stablecoins, pulse.fear_greed, pulse.market

    # Stablecoin flow
    if sc.mcap_7d_change > 1_000_000_000:
        out.append("🟢 Heavy stablecoin inflow 7d (>$1B) — strong dry powder")
    elif sc.mcap_7d_change > 250_000_000:
        out.append("🟢 Stablecoin inflow 7d — capital entering crypto")
    elif sc.mcap_7d_change < -1_000_000_000:
        out.append("🔴 Heavy stablecoin outflow 7d (>$1B) — capital fleeing")
    elif sc.mcap_7d_change < -250_000_000:
        out.append("🔴 Stablecoin outflow 7d — capital exiting")

    # Fear & Greed contrarian
    if fg.value:
        if fg.value <= 20:
            out.append("🟢 Extreme Fear — historically a buy zone")
        elif fg.value <= 30:
            out.append("🟢 Fear — contrarian bullish")
        elif fg.value >= 80:
            out.append("🔴 Extreme Greed — historically a sell zone")
        elif fg.value >= 70:
            out.append("🟡 Greed — market may be overheated")

    # Fear & Greed momentum
    if fg.value and fg.last_week_value:
        d = fg.value - fg.last_week_value
        if d >= 15:
            out.append("🟢 Sentiment rising fast (F&G +15 in 7d)")
        elif d <= -15:
            out.append("🔴 Sentiment falling fast (F&G -15 in 7d)")

    # BTC dominance
    if m.btc_dominance >= 60:
        out.append("🟡 High BTC dominance (>60%) — alts underperforming")
    elif m.btc_dominance and m.btc_dominance <= 45:
        out.append("🟢 Low BTC dominance (<45%) — alt-season conditions")

    # 24h momentum
    if m.total_mcap_change_24h >= 3:
        out.append("🟢 Strong market rally (+3% in 24h)")
    elif m.total_mcap_change_24h <= -3:
        out.append("🔴 Sharp market drop (-3% in 24h)")

    return out


# ── Market Regime synthesis ──────────────────────────────────────────────────

@dataclass
class MarketRegime:
    """Accumulation vs distribution verdict synthesized from all signals."""
    score:    int = 0             # signed total (-N to +N)
    label:    str = "NEUTRAL"     # ACCUMULATION / DISTRIBUTION / NEUTRAL / TRANSITION
    emoji:    str = "🟡"
    bullish:  List[str] = field(default_factory=list)
    bearish:  List[str] = field(default_factory=list)
    narrative: str = ""           # one-line plain-English read on the market


def derive_market_regime(pulse: MarketPulse, etf_flows: Optional[Any] = None) -> MarketRegime:
    """
    Score the macro data on a -N..+N scale and bucket into a regime.

    `etf_flows` is duck-typed: any object with `.sum_7d` (float, $M) and
    `.consecutive` (int, signed) attributes works. Pass None if unavailable.
    """
    regime = MarketRegime()
    score = 0

    # ── Stablecoin 7d flow (capital in/out of crypto) ───────────────────
    sc7 = pulse.stablecoins.mcap_7d_change
    if sc7 > 1_000_000_000:
        score += 2
        regime.bullish.append(f"Stablecoin 7d inflow `${sc7/1e6:+,.0f}M` — strong dry powder")
    elif sc7 > 250_000_000:
        score += 1
        regime.bullish.append(f"Stablecoin 7d inflow `${sc7/1e6:+,.0f}M` — capital entering")
    elif sc7 < -1_000_000_000:
        score -= 2
        regime.bearish.append(f"Stablecoin 7d outflow `${sc7/1e6:+,.0f}M` — heavy exits")
    elif sc7 < -250_000_000:
        score -= 1
        regime.bearish.append(f"Stablecoin 7d outflow `${sc7/1e6:+,.0f}M` — capital exiting")

    # ── BTC ETF flows (institutional positioning) ────────────────────────
    if etf_flows is not None and getattr(etf_flows, "days", None):
        e7 = float(getattr(etf_flows, "sum_7d", 0) or 0)        # $M
        streak = int(getattr(etf_flows, "consecutive", 0) or 0)

        if e7 >= 1500:
            score += 2
            regime.bullish.append(f"BTC ETF 7d cumulative `${e7:+,.0f}M` — institutional accumulation")
        elif e7 >= 300:
            score += 1
            regime.bullish.append(f"BTC ETF 7d cumulative `${e7:+,.0f}M` — institutional bid")
        elif e7 <= -1500:
            score -= 2
            regime.bearish.append(f"BTC ETF 7d cumulative `${e7:+,.0f}M` — institutional distribution")
        elif e7 <= -300:
            score -= 1
            regime.bearish.append(f"BTC ETF 7d cumulative `${e7:+,.0f}M` — institutional selling")

        if streak >= 5:
            score += 1
            regime.bullish.append(f"{streak}-day ETF inflow streak — persistent bid")
        elif streak <= -5:
            score -= 1
            regime.bearish.append(f"{abs(streak)}-day ETF outflow streak — persistent selling")

    # ── Fear & Greed (contrarian at extremes) ────────────────────────────
    fg = pulse.fear_greed.value
    if fg:
        if fg <= 20:
            score += 2
            regime.bullish.append(f"F&G `{fg}` (Extreme Fear) — historically a buy zone")
        elif fg <= 30:
            score += 1
            regime.bullish.append(f"F&G `{fg}` (Fear) — contrarian bullish")
        elif fg >= 80:
            score -= 2
            regime.bearish.append(f"F&G `{fg}` (Extreme Greed) — historically a sell zone")
        elif fg >= 70:
            score -= 1
            regime.bearish.append(f"F&G `{fg}` (Greed) — overheated")

        # 7-day sentiment momentum
        wk_delta = fg - pulse.fear_greed.last_week_value if pulse.fear_greed.last_week_value else 0
        if wk_delta >= 15:
            score += 1
            regime.bullish.append(f"F&G `+{wk_delta}` in 7d — sentiment improving fast")
        elif wk_delta <= -15:
            score -= 1
            regime.bearish.append(f"F&G `{wk_delta}` in 7d — sentiment deteriorating fast")

    # ── DeFi TVL trend (capital working in DeFi) ─────────────────────────
    tvl7 = pulse.tvl.change_7d_pct
    if tvl7 >= 10:
        score += 2
        regime.bullish.append(f"DeFi TVL `+{tvl7:.1f}%` 7d — strong capital deploying into DeFi")
    elif tvl7 >= 5:
        score += 1
        regime.bullish.append(f"DeFi TVL `+{tvl7:.1f}%` 7d — capital flowing into DeFi")
    elif tvl7 <= -10:
        score -= 2
        regime.bearish.append(f"DeFi TVL `{tvl7:.1f}%` 7d — capital fleeing DeFi")
    elif tvl7 <= -5:
        score -= 1
        regime.bearish.append(f"DeFi TVL `{tvl7:.1f}%` 7d — capital leaving DeFi")

    # ── Short-term price action ──────────────────────────────────────────
    m24 = pulse.market.total_mcap_change_24h
    if m24 >= 3:
        score += 1
        regime.bullish.append(f"Total mcap `+{m24:.1f}%` 24h — strong rally")
    elif m24 <= -3:
        score -= 1
        regime.bearish.append(f"Total mcap `{m24:.1f}%` 24h — sharp drop")

    # ── BTC dominance regime ─────────────────────────────────────────────
    btc_dom = pulse.market.btc_dominance
    if btc_dom and btc_dom <= 45:
        score += 1
        regime.bullish.append(f"BTC dom `{btc_dom:.1f}%` — alt-season conditions")
    elif btc_dom >= 60:
        score -= 1
        regime.bearish.append(f"BTC dom `{btc_dom:.1f}%` — risk-off, alts weak")

    # ── Bucket the score ─────────────────────────────────────────────────
    regime.score = score
    if score >= 5:
        regime.label, regime.emoji = "STRONG ACCUMULATION", "🟢"
    elif score >= 2:
        regime.label, regime.emoji = "ACCUMULATION", "🟢"
    elif score <= -5:
        regime.label, regime.emoji = "STRONG DISTRIBUTION", "🔴"
    elif score <= -2:
        regime.label, regime.emoji = "DISTRIBUTION", "🔴"
    elif regime.bullish and regime.bearish:
        regime.label, regime.emoji = "TRANSITION", "🟡"
    else:
        regime.label, regime.emoji = "NEUTRAL", "🟡"

    # ── Narrative — describes the dominant pattern ───────────────────────
    regime.narrative = _build_narrative(score, regime, pulse, etf_flows)

    return regime


def _build_narrative(score: int, regime: MarketRegime, pulse: MarketPulse, etf_flows) -> str:
    """One-line plain-English read on what the market is doing."""
    sc7    = pulse.stablecoins.mcap_7d_change
    fg     = pulse.fear_greed.value
    btc_d  = pulse.market.btc_dominance
    e7     = float(getattr(etf_flows, "sum_7d", 0) or 0) if etf_flows else 0

    # Strong cases first
    if score >= 5:
        return ("Capital flowing into crypto across stables and ETFs while sentiment is "
                "still cautious — classic accumulation. Bull bias.")
    if score <= -5:
        return ("Capital fleeing crypto across stables and ETFs while sentiment is "
                "still elevated — classic distribution. Bear bias.")

    # Mixed signals — describe the tension
    if sc7 > 250_000_000 and e7 < -300:
        return ("Stables filling up (dry powder) but ETFs bleeding — "
                "retail accumulating, institutions de-risking. Mixed.")
    if sc7 < -250_000_000 and e7 > 300:
        return ("Stables draining but ETFs buying — institutions absorbing supply "
                "while retail exits. Often a late-cycle accumulation pattern.")

    if score >= 2:
        if btc_d and btc_d >= 60:
            return "Inflows present but parked in BTC — alts will lag until BTC tops."
        if btc_d and btc_d <= 45:
            return "Inflows rotating into alts — alt-season conditions building."
        return "Net buyers > net sellers — early-stage accumulation."

    if score <= -2:
        if fg and fg >= 70:
            return "Distribution while greed is elevated — typical late-cycle topping behavior."
        return "Net sellers > net buyers — distribution underway."

    # Neutral
    if regime.bullish and regime.bearish:
        return "Bullish and bearish forces roughly balanced — no high-conviction macro edge."
    return "Quiet macro tape — wait for stronger signal before sizing up."


def format_market_regime(regime: MarketRegime) -> str:
    """Render the regime block for the Telegram briefing."""
    lines = [
        f"*MARKET REGIME* — {regime.emoji} *{regime.label}*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Score: `{regime.score:+d}`  ({len(regime.bullish)} bullish / {len(regime.bearish)} bearish)",
        "",
        f"📖 _{regime.narrative}_",
        "",
    ]
    if regime.bullish:
        lines.append("*Signs of accumulation:*")
        for s in regime.bullish:
            lines.append(f"  ✓ {s}")
        lines.append("")
    if regime.bearish:
        lines.append("*Signs of distribution:*")
        for s in regime.bearish:
            lines.append(f"  ✗ {s}")
        lines.append("")
    return "\n".join(lines)


# ── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pulse = fetch_market_pulse()
    print(format_market_pulse(pulse))
    # Demo regime without ETF data
    regime = derive_market_regime(pulse, etf_flows=None)
    print(format_market_regime(regime))
