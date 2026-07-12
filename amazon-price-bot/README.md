# Amazon.sa Price-Drop Sniper Bot

Watches a list of Amazon.sa products, and when one crashes to a snipe-worthy
price (e.g. a 500 SAR item listed at 1–10 SAR), it instantly sends you a
Telegram alert with a **🛒 BUY NOW** button. Tapping the button auto-purchases
the item through your own Amazon account using a saved browser session.

## How it works (and why it's fast)

- **No browser for monitoring.** Prices are polled over raw HTTP/2 with
  `httpx`, hitting Amazon's lightweight *All Offers* AJAX endpoint first
  (~10× smaller than the product page), with the full page as fallback.
  Parsing uses `selectolax` (C-speed) plus regex fallbacks.
- **Concurrent sweeps.** The whole watchlist is checked in parallel (bounded
  concurrency), with randomized stagger/jitter and automatic cool-down when
  Amazon throttles, so the bot survives instead of getting IP-banned.
- **Browser only at purchase time.** Playwright loads your saved session,
  clicks *Buy Now*, and completes Turbo (one-tap) or classic checkout.
  Your password is never stored — only session cookies from a login you do
  yourself, once.
- **Two safety re-checks before money moves:** the live product price and the
  checkout order total are both verified against `buy.max_auto_price_sar`.
  If the deal vanished between alert and click, the purchase aborts and you
  get a screenshot.

## Setup

```bash
cd amazon-price-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp config.example.yaml config.yaml   # then edit it
```

1. **Telegram bot:** talk to [@BotFather](https://t.me/BotFather), create a
   bot, put its token in `config.yaml`. Get your numeric chat id from
   [@userinfobot](https://t.me/userinfobot) and set `chat_id`.
2. **Amazon session (one time):**
   ```bash
   python -m bot.login
   ```
   A real browser opens — log in to Amazon.sa yourself (including OTP), press
   Enter in the terminal, and the session is saved to `amazon_session.json`.
3. **Amazon account prep:** set a default delivery address and a default
   payment method. If your bank forces an OTP on every card charge, checkout
   will stop at that step (the bot sends you a screenshot when that happens).
4. **Run it:**
   ```bash
   python -m bot.main
   ```

## Using it

- Watchlist lives in the `watchlist:` section of `config.yaml` and/or via
  Telegram:
  - `/add B0ABC12345 499` — watch an ASIN, with its normal price
  - `/remove B0ABC12345`, `/list`, `/status`
- Alert rule (both must pass; set one to `0` to disable it):
  - `deal.max_price_sar` — absolute threshold (e.g. `10`)
  - `deal.min_discount_pct` — e.g. `85` (% below the item's `ref_price`)
- When an alert fires you get title, price, discount, link, and buttons.
  **🛒 BUY NOW** triggers the purchase; you get back a result message plus a
  screenshot of the confirmation (or of whatever blocked it).

Run it 24/7 on any small VPS; a `systemd` unit or `tmux` session is enough.
Keep `headless: true` on servers. `sniper.db`, `amazon_session.json` and
`screenshots/` are created next to the config.

## Honest caveats

- **Glitch prices usually get cancelled.** Amazon's terms let them cancel
  orders placed at obvious pricing errors, and they typically do. The bot
  gets your order in fast; it can't force Amazon to honour it.
- **Scraping and automated purchasing are against Amazon's Terms of Use.**
  Amazon may throttle, captcha, or in principle action the account. The bot
  polls politely (jitter, back-off) to keep a low profile — keep the
  watchlist reasonable (tens of items, not thousands) and the interval ≥30 s.
- **Bank OTP / 3-D Secure can't be automated** (by design). For true one-tap
  buying, use a payment method that doesn't challenge every charge.
- Amazon changes its HTML regularly; if prices stop parsing, the selectors in
  `bot/scraper.py` are the place to update.

## Files

| Path | Purpose |
|---|---|
| `bot/main.py` | entrypoint, wires everything |
| `bot/monitor.py` | polling loop + deal rules |
| `bot/scraper.py` | fast HTTP price fetch/parse |
| `bot/telegram_bot.py` | alerts, Buy button, commands |
| `bot/buyer.py` | Playwright checkout with price safety caps |
| `bot/login.py` | one-time interactive Amazon login |
| `bot/storage.py` | SQLite watchlist/alert/purchase state |
