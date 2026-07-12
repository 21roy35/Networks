# Amazon.sa Price-Drop Sniper Bot

Scans **the entire Amazon.sa catalog** for glitch prices (e.g. a 500 SAR item
listed at 1–10 SAR) and instantly sends a Telegram alert with a **🛒 BUY NOW**
button — or buys automatically, if you enable `auto_buy`. Purchases go through
your own Amazon account using a saved browser session.

## How it covers the whole catalog efficiently

Brute-force polling every ASIN is physically impossible: Amazon.sa lists
millions of products, and one bot can politely make maybe ~1 request/second —
that's a full year per pass. Instead the **discovery engine** makes Amazon's
own search index do the filtering *server-side*. Per sweep, for each catalog
department it requests:

```
/s?i=<dept>&low-price=1&high-price=10&s=price-asc-rank&rh=p_n_pct-off-with-tax:90-
```

Every response is ~24–60 products that *already match* "costs almost nothing
right now", each result card carrying the current price **and** the
strike-through list price. Anything with a big list price (≥ 100 SAR by
default) and a tiny current price is a glitch candidate. So **~36 requests
(~1 MB) screen the whole store every 5 minutes** — that's the most efficient
coverage possible without Amazon's internal firehose.

Candidates are then **re-verified against the live buy-box** (search indexes
lag) before any alert or purchase fires, so a stale index entry can't waste
your click or your money.

On top of that there's a **priority watchlist** — specific ASINs you care
about, polled every ~45 s via the lightweight *All Offers* AJAX endpoint
(~10× smaller than a product page), for items you want caught faster than the
catalog sweep.

## Why it's fast

- **No browser for monitoring.** Everything is raw HTTP/2 with `httpx`;
  parsing uses `selectolax` (C-speed) plus regex fallbacks.
- **Concurrent sweeps.** Watchlist checks and catalog sweeps run in parallel
  (bounded concurrency), with randomized stagger/jitter and automatic
  cool-down when Amazon throttles, so the bot survives instead of getting
  IP-banned. Optional proxy support (`monitor.proxy`).
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

Telegram commands:

| Command | What it does |
|---|---|
| `/add B0ABC12345 499` | watch an ASIN (with its normal price) at high frequency |
| `/remove B0ABC12345`, `/list` | manage the priority watchlist |
| `/buy B0ABC12345` | trigger a purchase manually, right now |
| `/sweep` | run a full catalog discovery sweep immediately |
| `/pause`, `/resume` | pause/resume all scanning |
| `/status` | uptime, sweep stats, rules, auto-buy state |

Tuning (in `config.yaml`):

- **Catalog-wide rule** (`discovery:`): current price within
  `min_price_sar`–`max_price_sar`, list price ≥ `min_list_price_sar`, and
  discount ≥ `min_discount_pct`. The `min_list_price_sar` floor is what
  separates a real glitch (500 SAR → 5 SAR) from items that are legitimately
  cheap (stickers, cables).
- **Watchlist rule** (`deal:`): `max_price_sar` AND `min_discount_pct`
  vs the item's `ref_price` (set either to `0` to disable it).
- **`buy.auto_buy: true`** skips the button and purchases the moment a
  verified deal is found — for glitches that die in seconds. The alert still
  arrives, marked "auto-buying". Every purchase path (button, `/buy`,
  auto-buy) re-checks the live price and the checkout order total against
  `buy.max_auto_price_sar` before placing the order, and you always get a
  result message + confirmation screenshot back in Telegram.

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
  watchlist reasonable (tens of items, not thousands), the poll interval
  ≥30 s, and the sweep interval ≥5 min. If you get throttled often, add more
  categories/pages *slower*, not faster, or set `monitor.proxy`.
- **The `p_n_pct-off-with-tax` refinement isn't officially documented.** If
  Amazon ignores or drops it, the price band + list-price floor still do the
  filtering; set `use_pct_off_filter: false` if it ever causes empty results.
- **Bank OTP / 3-D Secure can't be automated** (by design). For true one-tap
  buying, use a payment method that doesn't challenge every charge.
- Amazon changes its HTML regularly; if prices stop parsing, the selectors in
  `bot/scraper.py` are the place to update.

## Files

| Path | Purpose |
|---|---|
| `bot/main.py` | entrypoint, wires everything, auto-buy dispatch |
| `bot/discovery.py` | full-catalog sweep via server-side-filtered search |
| `bot/monitor.py` | high-frequency priority watchlist polling |
| `bot/scraper.py` | fast HTTP price fetch/parse (shared client) |
| `bot/telegram_bot.py` | alerts, Buy button, commands |
| `bot/buyer.py` | Playwright checkout with price safety caps |
| `bot/login.py` | one-time interactive Amazon login |
| `bot/storage.py` | SQLite watchlist/alert/discovery/purchase state |
| `bot/state.py` | shared pause/stats state |
