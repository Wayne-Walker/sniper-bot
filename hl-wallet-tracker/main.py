import sys
from src.bot import WalletTrackerBot
from src.config import config
from src.utils.logger import get_logger

log = get_logger("main")

HELP = """
╔══════════════════════════════════════════════════════════════════╗
║        Hyperliquid Wallet Tracker                                ║
║        Track the most successful trading wallets                 ║
╚══════════════════════════════════════════════════════════════════╝

Commands:
  all                   Full report — all tracked wallets
  wallet <ADDRESS>      Deep dive for one wallet (partial address OK)
  coin <SYMBOL>         All trades for a coin across all wallets
  wallets               List all tracked wallet addresses
  refresh               Re-fetch all data
  help                  Show this message
  exit                  Quit

Examples:
  > all
  > wallet 0x4f8a3b
  > coin BTC
  > coin SOL
  > coin HYPE
"""


def run_cli(bot: WalletTrackerBot) -> None:
    print(HELP)
    while True:
        try:
            raw = input("tracker> ").strip()
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
        elif cmd == "all":
            try:
                bot.run_all()
            except Exception as e:
                log.error(f"Error: {e}")
        elif cmd == "wallet" and len(parts) >= 2:
            try:
                bot.run_wallet(parts[1])
            except Exception as e:
                log.error(f"Error: {e}")
        elif cmd == "wallet":
            print("  Usage: wallet <address>")
        elif cmd == "coin" and len(parts) >= 2:
            try:
                bot.run_coin(parts[1])
            except Exception as e:
                log.error(f"Error: {e}")
        elif cmd == "coin":
            print("  Usage: coin <SYMBOL>  e.g. coin BTC")
        elif cmd == "wallets":
            try:
                bot.list_wallets()
            except Exception as e:
                log.error(f"Error: {e}")
        elif cmd == "refresh":
            bot.refresh()
            print("  Refreshed. Run 'all' to re-analyse.")
        else:
            print(f"  Unknown command: '{raw}'. Type 'help'.")


def main() -> None:
    log.info("Starting Hyperliquid Wallet Tracker...")
    log.info(
        f"Config: lookback={config.LOOKBACK_DAYS}d | "
        f"wallets={'custom' if config.WATCH_WALLETS else f'top {config.LEADERBOARD_TOP_N} leaderboard'} | "
        f"window={config.LEADERBOARD_WINDOW}"
    )

    bot = WalletTrackerBot()

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "all":
            bot.run_all()
        elif cmd == "wallet" and len(sys.argv) > 2:
            bot.run_wallet(sys.argv[2])
        elif cmd == "coin" and len(sys.argv) > 2:
            bot.run_coin(sys.argv[2])
        else:
            bot.run_wallet(sys.argv[1])
        sys.exit(0)

    run_cli(bot)


if __name__ == "__main__":
    main()
