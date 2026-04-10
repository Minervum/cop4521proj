#!/usr/bin/env python3
"""
Unified CLI entry point for the Kalshi trading bot platform.

Usage
-----
  python3 main.py botd <command>

Bot D commands
--------------
  run               Run one signal cycle and print results
  loop              Run a continuous 15-minute scheduled loop
  sync              Force a full data refresh (all four games)
  status            Print the bot status dashboard as JSON
  tournament-state  Show active tournament bracket context and exploitable
                    upcoming matches (clinched teams, must-win situations)

Examples
--------
  python3 main.py botd run
  python3 main.py botd tournament-state
  python3 main.py botd status
"""

import logging
import sys


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    bot = sys.argv[1].lower()

    if bot == "botd":
        # Strip the 'botd' positional arg so botd/bot.py's argparse sees
        # only the sub-command (run / loop / sync / status / tournament-state).
        sys.argv = [sys.argv[0]] + sys.argv[2:]

        logging.basicConfig(
            level=logging.WARNING,  # suppress INFO noise; bot.py re-configures if needed
            format="%(levelname)s %(name)s: %(message)s",
        )

        from botd.bot import main as botd_main
        botd_main()

    else:
        print(f"Unknown bot: {bot!r}")
        print("Available bots: botd")
        print("Run 'python3 main.py --help' for usage.")
        sys.exit(1)


if __name__ == "__main__":
    main()
