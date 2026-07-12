"""Command-line entry point.

    python -m flight_bot scan    # fetch airline emails over IMAP and link them
    python -m flight_bot demo    # load bundled sample emails (no credentials)
    python -m flight_bot web     # start the GUI at http://127.0.0.1:5000
    python -m flight_bot list    # print linked flights to the terminal
    python -m flight_bot reset   # wipe the local database
"""

import argparse

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


if __name__ == "__main__":
    main()
