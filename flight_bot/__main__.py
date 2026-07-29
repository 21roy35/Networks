"""Command-line entry point.

    python -m flight_bot scan    # fetch airline emails over IMAP and link them
    python -m flight_bot demo    # load bundled sample emails (no credentials)
    python -m flight_bot web     # start the GUI at http://127.0.0.1:5000
    python -m flight_bot list    # print linked flights to the terminal
    python -m flight_bot reset   # wipe the local database
"""

import argparse
import time

from . import db
from .config import load_config
from .pipeline import load_demo, rebuild_flights, scan_mailbox


def cmd_list(_args):
    db.init_db()
    flights = db.list_flights()
    if not flights:
        print("No flights yet. Run `python -m flight_bot scan` or `demo` first.")
        return
    for flight in flights:
        route = f"{flight.get('origin') or '???'}->{flight.get('destination') or '???'}"
        print(f"[{flight['id']:>3}] {flight.get('flight_date') or '????-??-??'}  "
              f"{flight.get('airline_name') or '?':<18} "
              f"{flight.get('flight_number') or '?':<7} {route:<10} "
              f"PNR={flight.get('pnr') or '-':<8} "
              f"emails={flight.get('email_count')}")


def main():
    parser = argparse.ArgumentParser(prog="flight_bot",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="scan the mailbox over IMAP")
    sub.add_parser("demo", help="load bundled sample emails")
    sub.add_parser("relink", help="re-run linking on stored emails")
    sub.add_parser("list", help="print linked flights")
    web = sub.add_parser("web", help="start the web GUI")
    web.add_argument("--host", default=None)
    web.add_argument("--port", type=int, default=None)
    sub.add_parser("reset", help="wipe the local database")
    sub.add_parser("telegram", help="run the Telegram assistant and monitors")
    sub.add_parser("telegram-id", help="show chat ids that recently messaged the bot")
    args = parser.parse_args()

    config = load_config()
    if args.command == "scan":
        scan_mailbox(config)
        cmd_list(args)
    elif args.command == "demo":
        load_demo()
        cmd_list(args)
    elif args.command == "relink":
        rebuild_flights()
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "reset":
        db.init_db()
        db.reset()
        print("Database cleared.")
    elif args.command == "web":
        from .webapp import create_app
        app = create_app(config)
        host = args.host or config["web"]["host"]
        port = args.port or config["web"]["port"]
        print(f"GUI running at http://{host}:{port}")
        app.run(host=host, port=port, debug=False)
    elif args.command == "telegram":
        from .telegram_bot import start_telegram
        coordinator = start_telegram(config)
        if not coordinator:
            raise SystemExit(
                "Telegram is not configured. Add telegram.bot_token and "
                "telegram.chat_id to config.json or the matching environment variables.")
        print("Telegram assistant running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            coordinator.stop()
    elif args.command == "telegram-id":
        from .telegram_bot import TelegramAPI
        token = config.get("telegram", {}).get("bot_token") or ""
        if not token:
            raise SystemExit("Set telegram.bot_token first, then message the bot /start.")
        updates = TelegramAPI(token).updates(0, 1)
        chats = {}
        for update in updates:
            message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
            chat = message.get("chat") or {}
            if chat.get("id") is not None:
                chats[str(chat["id"])] = chat.get("username") or chat.get("first_name") or "private chat"
        if not chats:
            print("No recent chats. Send /start to the bot and run this again.")
        else:
            for chat_id, label in chats.items():
                print(f"{chat_id}  {label}")


if __name__ == "__main__":
    main()
