# FlightDeck — flights, evidence, and automated passenger complaints

FlightDeck turns airline messages into complete flight records, tracks each journey through Telegram, and files passenger complaints on official airline and GACA websites. It extracts the passenger, PNR, ticket, route, schedule, payment card, receipts, boarding details, and disruption evidence from the mailbox.

## What it does

- Shows the payment method on every flight and filters by card brand.
- Filters flights by passenger name.
- Copies or downloads every filtered result as a complete TXT report.
- Preserves manual corrections across mailbox rescans.
- Checks GACA, EU261/EEA, and UK261 passenger-rights coverage.
- Files complaints through official airline websites, never by email or `mailto:`.
- Escalates eligible airline complaints through GACA's official E-Services website.
- Saves incident photos, official complaint references, status, and response history.
- Uses Telegram for post-flight check-ins, issue/photo intake, OTP and CAPTCHA assistance, airline-response alerts, and one-tap GACA escalation.

After the one-time profile and integration setup, the only per-incident input is a plain-language description such as “the seat was broken and the screen did not work,” plus any photos. FlightDeck supplies the stored passenger, booking, flight, evidence, legal basis, and requested remedies.

## Quick start

```powershell
pip install -r requirements.txt
python -m flight_bot demo
python -m flight_bot web
```

Open `http://127.0.0.1:5000`.

Complaint automation uses a visible Microsoft Edge window and a persistent local browser profile. This lets an official-site login survive later submissions without putting the portal password in Telegram or `config.json`.

On a Linux server, FlightDeck automatically uses headless Chromium while retaining the same persistent profile and Telegram screenshot/verification relay. Set `FLIGHTBOT_HEADLESS_BROWSER=0` only when the server has an interactive desktop session.

## Connect the mailbox

1. Enable two-step verification for the mailbox and create an app password.
2. Copy `config.example.json` to `config.json`.
3. Set `imap.user`, `imap.password`, and the reusable `user` profile.
4. Run `python -m flight_bot scan`, or select **Scan mailbox** in the interface.

The inbox is used to import flight records and detect airline replies. It is not used to submit complaints. All complaint submissions go through the airline's official web form.

## Connect Telegram

1. Create a private bot using Telegram's official [BotFather](https://t.me/BotFather) and copy its token.
2. Put the token in `telegram.bot_token` in `config.json`, or set `FLIGHTBOT_TELEGRAM_BOT_TOKEN`.
3. Send `/start` to the new bot, then run `python -m flight_bot telegram-id`.
4. Put the printed private chat ID in `telegram.chat_id`, or set `FLIGHTBOT_TELEGRAM_CHAT_ID`.
5. Run `python -m flight_bot telegram`. Running `python -m flight_bot web` also starts Telegram automatically when both credentials are configured.

The bot accepts messages only from the configured private chat ID. `/status` reports the current flight, complaint, and mailbox totals. `/web` returns a short-lived private sign-in link for opening the dashboard on a phone or other device; after opening it, that browser remains signed in for the configured session period. OTP replies are deleted from the Telegram chat after use when Telegram permits deletion. Evidence photos are downloaded to the ignored local `telegram_evidence/` directory and linked to the complaint record.

The workflow uses Telegram's official [Bot API](https://core.telegram.org/bots/api). Keep the bot token private; anyone with it can control the bot.

## Telegram workflow

After a recorded arrival, FlightDeck asks how the flight went. You can tap **Everything was good**, or describe an issue and attach photos. After a short collection window, FlightDeck opens the airline's official complaint form, fills it, attaches the evidence, submits it, saves the official reference, and confirms the outcome in Telegram.

If the portal requests an OTP, text CAPTCHA, image-grid CAPTCHA, legal declaration, Nafath approval, or similar human verification, FlightDeck sends the relevant prompt or screenshot to Telegram. Your reply is applied to the waiting portal session and automation resumes. These controls are assisted, not bypassed. Portal passwords are never requested through Telegram; a one-time sign-in is completed in the persistent Edge window.

FlightDeck scans the configured inbox for substantive airline replies. When one is matched to a submitted complaint, it sends a concise excerpt to Telegram and offers **Escalate to GACA** or **No, close**. GACA receives the original incident, evidence, flight data, airline complaint date, and airline reference automatically.

## Flight completion detection

The default `schedule` provider triggers after the stored arrival time plus `post_flight_delay_minutes`. For live landed-state checks, configure FlightAware AeroAPI:

```json
"flight_status": {
  "provider": "flightaware",
  "flightaware_api_key": "your-key",
  "poll_minutes": 10
}
```

The key can instead be supplied as `FLIGHTBOT_FLIGHTAWARE_API_KEY`. FlightAware usage may be billed under your AeroAPI plan; see the official [AeroAPI portal](https://www.flightaware.com/aeroapi/portal/).

## Official complaint workflow

The web interface remains available as a second control surface:

1. Open a flight and select **File on airline website**.
2. Type what went wrong.
3. FlightDeck fills and submits the official airline form and stores the reference.
4. If escalation is needed, select **Escalate on GACA website**. The airline reference and original incident are carried over automatically.

Dedicated adapters are included for Saudia, flynas, flyadeal, and GACA. Other registered airlines use the same official-site field mapper. GACA requires the airline complaint first; its official service permits escalation after seven days without a response or when the airline's resolution is unsatisfactory.

## Configuration without stored secrets

These environment variables override `config.json`:

```text
FLIGHTBOT_IMAP_HOST
FLIGHTBOT_IMAP_USER
FLIGHTBOT_IMAP_PASSWORD
FLIGHTBOT_TELEGRAM_BOT_TOKEN
FLIGHTBOT_TELEGRAM_CHAT_ID
FLIGHTBOT_FLIGHTAWARE_API_KEY
FLIGHTBOT_PUBLIC_BASE_URL
FLIGHTBOT_WEB_ACCESS_SECRET
```

## How it works

| Stage | Module | Purpose |
|---|---|---|
| Fetch | `mail_client.py` | Reads relevant flight and complaint-response messages over IMAP |
| Parse | `parser.py` | Extracts booking, ticket, passenger, payment, schedule, and disruption fields |
| Link | `linker.py` | Merges related messages into one flight |
| Store | `db.py` | Stores flights, corrections, evidence, complaint status, responses, and official references in SQLite |
| Assess | `compensation.py` | Checks GACA, EU/EEA, and UK passenger-rights coverage |
| Prepare | `complaints.py` | Builds a structured portal payload from the incident and extracted facts |
| Submit | `portal_automation.py` | Drives the official website in visible Edge and relays verification |
| Track | `flight_status.py` | Detects scheduled completion or optional live landed status |
| Converse | `telegram_bot.py` | Runs post-flight intake, verification, response alerts, and escalation |
| Interface | `webapp.py` + `templates/` | Dashboard, filters, TXT export, flight details, profile, and portal status |

## CLI

```text
python -m flight_bot scan
python -m flight_bot demo
python -m flight_bot list
python -m flight_bot relink
python -m flight_bot web
python -m flight_bot telegram-id
python -m flight_bot telegram
python -m flight_bot reset
```

The rights assessment is guidance, not legal advice. Official forms change periodically. If a portal control is no longer recognized, FlightDeck stops safely, reports that attention is needed, and preserves the complaint details instead of guessing.
