# ✈ Flight Bot — flight email scraper & compensation assistant

Scans your mailbox for airline emails, links related messages together
(one email may hold the boarding pass, another the e-ticket number,
another the delay notice — they get merged into one flight record), and
serves a local web GUI where you can:

- see **all flights taken**, with date, airline, route and how many emails
  were linked for each;
- click a flight to get every detail with a **copy button on each field**:
  ticket number, boarding pass details (seat/gate/boarding time), cabin
  class, airline, times, origin, destination, payment method, price, PNR;
- get an automatic **compensation eligibility check** based on landing
  time and other variables (GACA Customer Protection Regulation for
  flights touching Saudi Arabia, EU261 for EU carriers);
- generate a **pre-filled complaint to GACA** (one click — letter is built
  from all the scraped ticket info, with a direct link to GACA's complaint
  portal);
- generate a **pre-filled complaint to the airline** (one click — send it
  directly from the app via SMTP, open it in your mail app via mailto,
  or copy it).

## Quick start (demo, no credentials needed)

```bash
pip install -r requirements.txt
python -m flight_bot demo     # loads bundled sample airline emails
python -m flight_bot web      # open http://127.0.0.1:5000
```

## Connecting your real mailbox (Gmail)

1. Turn on 2-Step Verification for your Google account, then create an
   **App Password**: https://myaccount.google.com/apppasswords
2. `cp config.example.json config.json` and fill in:
   - `imap.user` — your Gmail address
   - `imap.password` — the app password (NOT your normal password)
   - `user.full_name` / `phone` — used to sign complaint letters
3. Run:

```bash
python -m flight_bot scan     # or click "Scan mailbox" in the GUI
python -m flight_bot web
```

Credentials can also be supplied via environment variables instead of
config.json: `FLIGHTBOT_IMAP_USER`, `FLIGHTBOT_IMAP_PASSWORD`, etc.
`config.json` and the local database are git-ignored so nothing sensitive
gets committed. Any IMAP provider works — set `imap.host` accordingly.

## How it works

| Stage | Module | What it does |
|---|---|---|
| Fetch | `mail_client.py` | IMAP search for airline sender domains + flight keywords, HTML→text |
| Parse | `parser.py` | Classifies each email (booking / e-ticket / boarding pass / check-in / delay / cancellation / receipt) and extracts PNR, e-ticket number, flight numbers, route, times, class, seat, gate, passenger, payment method, amounts, delay hours |
| Link | `linker.py` | Groups emails sharing a PNR, ticket number, or flight+date; merges them with per-field authority (boarding-pass email wins for seat/gate, e-ticket email wins for ticket number, delay notice wins for new times) |
| Store | `db.py` | SQLite (`flightbot.db`), manual corrections survive rescans |
| Assess | `compensation.py` | GACA (KSA) + EU261 eligibility from arrival delay, cancellation notice, denied boarding |
| Complain | `complaints.py` | Pre-filled GACA and airline letters; SMTP send / mailto |
| GUI | `webapp.py` + templates | Flight list → detail with copyable fields → complaint pages |

## Notes & limitations

- **GACA has no public complaints API**, so full automation of filing is
  not possible; the app produces a completely filled letter plus a button
  that opens GACA's customer-support portal (https://cs.gaca.gov.sa/csportal/en/,
  phone 8001168888) — every field has a copy button to paste quickly.
  Airline complaints *can* be sent automatically via your SMTP account.
- Emails almost never contain the **actual landing time**; the app uses
  delay-notification emails when present, and the flight page has a
  "Corrections" box to enter the actual arrival, cancellation notice
  period, or denied boarding — the eligibility check re-runs instantly.
- The eligibility check is **guidance, not legal advice**; extraordinary
  circumstances (weather, ATC) can reduce or exclude compensation.
- Parsers are regex-based and airline emails vary; unrecognised airlines
  still work generically as long as a PNR/flight number is present. Add
  domains/complaint addresses in `flight_bot/airlines.py`.

## CLI

```
python -m flight_bot scan     # fetch + parse + link mailbox emails
python -m flight_bot demo     # load sample_emails/*.eml
python -m flight_bot list     # print flights to terminal
python -m flight_bot relink   # re-run linking after a parser change
python -m flight_bot web      # start the GUI
python -m flight_bot reset    # wipe the local database
```
