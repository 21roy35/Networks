# FlightDeck — flights, evidence, and automated passenger complaints

FlightDeck turns airline messages into complete flight records, tracks each journey through Telegram, and files passenger complaints on official airline and GACA websites. It extracts the passenger, PNR, ticket, route, schedule, payment card, receipts, boarding details, and disruption evidence from the mailbox.

## What it does

- Shows each payment method as card brand plus last four digits and filters by that exact card.
- Filters flights by passenger name.
- Prefills each passenger's complaint profile from labeled ticket-email and ticket-PDF evidence, including title, contact details, National ID/passport/Iqama, and Alfursan ID when available.
- Copies or downloads every filtered result as a complete TXT report.
- Preserves manual corrections across mailbox rescans.
- Checks GACA, EU261/EEA, and UK261 passenger-rights coverage.
- Files complaints through official airline websites, never by email or `mailto:`.
- Escalates eligible airline complaints through GACA's official E-Services website.
- Saves incident photos, official complaint references, status, and response history.
- Uses Telegram for post-flight check-ins, issue/photo intake, OTP and CAPTCHA assistance, airline-response alerts, and one-tap GACA escalation.
- Answers ordinary Telegram questions through Ghala: it can retrieve exact flights, passengers, complaints, references, responses, email, evidence and screenshots; refresh live flight evidence; recommend the best complaint action; trigger Gmail sync; or return the dashboard link.
- Uses the optional Ghala-200 Claude assistant to organize plain-language incidents, understand airline decisions, and recover safely when an official portal changes.

After the one-time profile and integration setup, the only per-incident input is a plain-language description such as “the seat was broken and the screen did not work,” plus any photos. FlightDeck supplies the stored passenger, booking, flight, evidence, legal basis, and requested remedies. Family bookings use separate passenger profiles: the bot asks once for that passenger's identity and never substitutes the account owner's National ID or loyalty number.

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

The bot accepts messages only from the configured private chat ID. `/status` reports the current flight, complaint, and mailbox totals. `/flightstatus SV1671` refreshes source-attributed live evidence for an exact flight, and `/recommend SV1671` explains its best complaint next step. `/web` returns a short-lived private sign-in link for opening the dashboard on a phone or other device; after opening it, that browser remains signed in for the configured session period. OTP replies are deleted from the Telegram chat after use when Telegram permits deletion. Evidence photos are downloaded to the ignored local `telegram_evidence/` directory and linked to the complaint record.

When Anthropic is enabled, you can also write normal requests such as “show my latest complaint,” “what did the airline say about C_2761389?”, “send the latest portal screenshot,” “show Mansour’s SV1671,” or “check Gmail now.” Claude is used only to interpret the intent and selectors. Flight, passenger, case, email, and image matching is performed by deterministic code against stored records; Claude cannot invent or directly alter them. Slash commands, SMS-reference capture, active post-flight intake, OTP, and CAPTCHA replies always take priority over this fallback. AI lookups run outside the Telegram polling thread so a slow Anthropic response does not freeze the bot.

The workflow uses Telegram's official [Bot API](https://core.telegram.org/bots/api). Keep the bot token private; anyone with it can control the bot.

## Telegram workflow

After a recorded arrival, FlightDeck asks how the flight went. You can tap **Everything was good**, or describe an issue and attach photos. After a short collection window, FlightDeck opens the airline's official complaint form, fills it, attaches the evidence, submits it, saves the official reference, and confirms the outcome in Telegram.

If the portal requests a reCAPTCHA and 2Captcha is configured, FlightDeck obtains and applies the token automatically. If that service is unavailable or the token cannot be applied, it falls back to the numbered Telegram image-grid flow. OTPs, missing required fields, dropdown/radio choices, legal declarations, Nafath approval, and final confirmation continue to use the relevant Telegram prompt and screenshot. Your reply is applied to the waiting VPS browser session and automation resumes, so the complaint can be started from a phone. Portal passwords are never requested through Telegram.

FlightDeck scans the configured inbox for substantive airline replies. When one is matched to a submitted complaint, it sends a concise excerpt to Telegram and offers **Escalate to GACA** or **No, close**. GACA receives the original incident, evidence, flight data, airline complaint date, and airline reference automatically.

When Ghala-200 is enabled, it summarizes the airline's actual outcome, extracts stated amounts or deadlines, and recommends whether to accept, reply, wait, review, or escalate. The recommendation never files an escalation by itself; the Telegram buttons remain the authority for that step.

## Ghala-200 AI assistance

Set `FLIGHTBOT_ANTHROPIC_API_KEY` on the server to enable the guarded Anthropic integration. The default model is `claude-sonnet-5`, and the display name is `Ghala-200`; either can be overridden with `FLIGHTBOT_AI_MODEL` and `FLIGHTBOT_AI_NAME`.

Ghala-200 receives only the complaint facts needed for its task. For portal recovery it may receive the current official-page screenshot, visible control metadata, and complaint payload. It may fill exact values already present in that payload or select safe navigation such as **Next**, **Continue**, or **Retry**. Code-level guardrails prevent it from supplying passwords, OTPs, CAPTCHA answers, security information, declarations, payment details, invented values, or final submission actions. Those protected steps are relayed to Telegram with a screenshot.

For incomplete passenger profiles, Ghala-200 reviews only the matching passenger's bounded ticket or PDF blocks after deterministic extraction runs. A proposed field is accepted only when its value and the correct field label both appear in the cited source block. Results are cached by passenger, evidence hash, and model, and conflicting deterministic identifiers remain blocked.

If Anthropic is unavailable or returns an unusable answer, FlightDeck continues with its deterministic form mappings and Telegram assistance. AI output can be wrong and is not legal advice. Anthropic API usage may incur model charges.

## Automatic CAPTCHA solving

Set `FLIGHTBOT_2CAPTCHA_API_KEY` to enable the 2Captcha API v2 integration. FlightDeck creates a proxyless reCAPTCHA v2 task, polls at the documented five-second minimum, applies the returned token and invokes the page callback when present. The configured Telegram chat remains the fallback. Solver usage consumes the balance on the configured 2Captcha account.

## Live flight evidence

FlightDeck persists normalized observations and derives a current status with its provider, timestamp, confidence, and contradictions. It polls only near departure: every 30 minutes from six hours before departure, every 10 minutes close to departure, and every four minutes while a flight may be airborne. Final states back off automatically.

The account-free default uses Airplanes.live for live ADS-B callsign tracking, adsb.lol as fallback, Aviation Weather Center METARs for context, booking emails for exact cancellation or rebooking evidence, and the stored schedule as the final fallback. Missing ADS-B data is never treated as cancellation, schedule completion is separate from verified landing, and weather is context rather than proof of legal cause.

FlightAware AeroAPI remains an optional higher-confidence operational source when a key is available:

```json
"flight_status": {
  "provider": "auto",
  "flightaware_api_key": "your-key",
  "poll_minutes": 10,
  "airplanes_live_enabled": true,
  "adsb_lol_enabled": true,
  "weather_enabled": true
}
```

No account is required for the default providers. A FlightAware key can instead be supplied as `FLIGHTBOT_FLIGHTAWARE_API_KEY`; its usage may be billed under your AeroAPI plan. The dashboard shows the evidence timeline, confidence, recommended action, missing facts, tailored remedy, and escalation deadline.

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
FLIGHTBOT_2CAPTCHA_API_KEY
FLIGHTBOT_PUBLIC_BASE_URL
FLIGHTBOT_WEB_ACCESS_SECRET
FLIGHTBOT_ANTHROPIC_API_KEY
FLIGHTBOT_AI_MODEL
FLIGHTBOT_AI_NAME
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
| Understand | `ai_assistant.py` | Organizes incidents, interprets replies, and proposes guarded portal recovery actions |
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
