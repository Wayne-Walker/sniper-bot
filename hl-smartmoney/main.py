import sys
import signal
from src.bot import SmartMoneyBot
from src.config import config
from src.data.liquidation_monitor import LiquidationMonitor
from src.analysis.edge_filter import EdgeFilter
from src.utils.logger import get_logger

log = get_logger("main")


HELP = """
╔══════════════════════════════════════════════════════════════╗
║      Hyperliquid Smart Money Sentiment Bot                   ║
╚══════════════════════════════════════════════════════════════╝

Commands:
  global              Full market scan — all coins, all divergences
  coin <SYMBOL>       Deep report for a specific coin
  edge                Scan all coins — only show signals above EDGE_THRESHOLD
  edge <SYMBOL>       Check edge for a specific coin
  check <SYMBOL> <long|short>  Gate check — is this trade allowed?
  refresh             Re-fetch leaderboard and positions
  help                Show this message
  exit                Quit

Examples:
  > global
  > coin SOL
  > edge
  > edge BTC
  > check SOL long
  > check ETH short
"""


def run_cli(bot: SmartMoneyBot, edge_filter: EdgeFilter) -> None:
    print(HELP)

    while True:
        try:
            raw = input("smart-money> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            sys.exit(0)

        if not raw:
            continue

        parts = raw.lower().split()
        cmd   = parts[0]

        if cmd in ("exit", "quit", "q"):
            print("Bye.")
            sys.exit(0)

        elif cmd == "help":
            print(HELP)

        elif cmd == "global":
            try:
                bot.run_global()
            except Exception as e:
                log.error(f"Global report failed: {e}")

        elif cmd == "coin" and len(parts) >= 2:
            try:
                bot.run_coin(parts[1])
            except Exception as e:
                log.error(f"Coin report failed: {e}")

        elif cmd == "coin":
            print("  Usage: coin <SYMBOL>   e.g. coin SOL")

        elif cmd == "edge" and len(parts) >= 2:
            # Single coin edge check
            try:
                _run_edge_coin(edge_filter, parts[1].upper())
            except Exception as e:
                log.error(f"Edge check failed: {e}")

        elif cmd == "edge":
            # Full market edge scan
            try:
                _run_edge_scan(edge_filter)
            except Exception as e:
                log.error(f"Edge scan failed: {e}")

        elif cmd == "check" and len(parts) >= 3:
            # Gate check for a bot trade
            try:
                _run_check(edge_filter, parts[1].upper(), parts[2].lower())
            except Exception as e:
                log.error(f"Check failed: {e}")

        elif cmd == "check":
            print("  Usage: check <SYMBOL> <long|short>   e.g. check SOL long")

        elif cmd == "refresh":
            try:
                bot.refresh()
                print("  Data refreshed.")
            except Exception as e:
                log.error(f"Refresh failed: {e}")

        else:
            print(f"  Unknown command: '{raw}'. Type 'help' for options.")


def _run_edge_scan(ef: EdgeFilter) -> None:
    signals = ef.scan()
    print(f"\n  EDGE FILTER SCAN — threshold: {ef.threshold}%")
    print("─" * 65)
    if not signals:
        print(f"  No signals above {ef.threshold}% threshold right now.")
        print(f"  Try lowering EDGE_THRESHOLD in .env for more signals.\n")
        return
    for s in signals:
        arrow = "🟢" if s.direction == "long" else "🔴"
        print(
            f"  {arrow} ${s.coin:<8}  {s.direction.upper():<6}  "
            f"gap={s.gap_pct:.1f}%  "
            f"mark=${s.mark_price:,.4f}  "
            f"{s.net_sentiment}"
        )
        print(
            f"     Smart: {s.winner_long_pct:.0f}% long  "
            f"net=${s.winner_net_notional:>+12,.0f}  "
            f"lev={s.winner_avg_leverage:.1f}x"
        )
        print(
            f"     Dumb : {s.loser_long_pct:.0f}% long  "
            f"net=${s.loser_net_notional:>+12,.0f}  "
            f"lev={s.loser_avg_leverage:.1f}x"
        )
        print()
    print(f"  Signals saved → signals/latest.json + latest.txt\n")


def _run_edge_coin(ef: EdgeFilter, coin: str) -> None:
    signal = ef.get_signal(coin)
    print(f"\n  EDGE CHECK — ${coin}  (threshold: {ef.threshold}%)")
    print("─" * 65)
    if signal is None:
        print(f"  ✗  No qualifying signal for ${coin} above {ef.threshold}%")
        print(f"     Either no divergence detected or gap is below threshold.\n")
    else:
        arrow = "🟢" if signal.direction == "long" else "🔴"
        print(f"  {arrow}  SIGNAL CONFIRMED")
        print(f"     Direction : {signal.direction.upper()}")
        print(f"     Gap       : {signal.gap_pct:.1f}%")
        print(f"     Mark price: ${signal.mark_price:,.4f}")
        print(f"     Sentiment : {signal.net_sentiment}")
        print(f"     Smart net : ${signal.winner_net_notional:>+12,.0f}  ({signal.winner_long_pct:.0f}% long, {signal.winner_avg_leverage:.1f}x lev)")
        print(f"     Dumb  net : ${signal.loser_net_notional:>+12,.0f}  ({signal.loser_long_pct:.0f}% long, {signal.loser_avg_leverage:.1f}x lev)")
        print()


def _run_check(ef: EdgeFilter, coin: str, direction: str) -> None:
    if direction not in ("long", "short"):
        print("  Direction must be 'long' or 'short'")
        return
    ok, reason = ef.is_tradeable(coin, direction)
    status = "✅ ALLOWED" if ok else "🚫 BLOCKED"
    print(f"\n  GATE CHECK — ${coin} {direction.upper()}")
    print("─" * 65)
    print(f"  {status}")
    print(f"  {reason}\n")


def main() -> None:
    log.info("Starting Hyperliquid Smart Money Sentiment Bot...")
    log.info(
        f"Config: top {config.TOP_N_WALLETS} wallets | "
        f"window: {config.LEADERBOARD_WINDOW} | "
        f"edge threshold: {config.EDGE_THRESHOLD}%"
    )

    bot         = SmartMoneyBot()
    edge_filter = EdgeFilter(threshold=config.EDGE_THRESHOLD)

    # Start liquidation monitor in background
    liq_monitor = LiquidationMonitor()
    liq_monitor.start()

    # One-shot CLI args
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg == "global":
            bot.run_global()
        elif arg == "edge" and len(sys.argv) > 2:
            _run_edge_coin(edge_filter, sys.argv[2].upper())
        elif arg == "edge":
            _run_edge_scan(edge_filter)
        elif arg == "check" and len(sys.argv) > 3:
            _run_check(edge_filter, sys.argv[2].upper(), sys.argv[3].lower())
        else:
            bot.run_coin(sys.argv[1])
        sys.exit(0)

    if config.AUTO_RUN_ON_START:
        try:
            bot.run_global()
        except Exception as e:
            log.error(f"Auto-run failed: {e}")

    def shutdown(signum, frame):
        log.warning("Shutting down...")
        liq_monitor.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    run_cli(bot, edge_filter)


if __name__ == "__main__":
    main()
