"""Telegram side: deal alerts with a Buy button, and control commands.

Commands:
  /add <ASIN> <ref_price>   add/update a watched item
  /remove <ASIN>            stop watching an item
  /list                     show the watchlist with last-seen prices
  /buy <ASIN>               trigger a purchase manually
  /sweep                    run a catalog discovery sweep right now
  /pause  /resume           pause/resume all monitoring
  /status                   bot status
"""
from __future__ import annotations

import html
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .buyer import Buyer, BuyResult
from .config import Config
from .scraper import BASE, PriceResult
from .state import RunState
from .storage import Store

log = logging.getLogger("telegram")

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


class TelegramBot:
    def __init__(self, cfg: Config, store: Store, buyer: Buyer, state: RunState):
        self.cfg = cfg
        self.store = store
        self.buyer = buyer
        self.state = state
        self.sweep_now = None  # set by main: async () -> int
        self.app: Application = (
            Application.builder().token(cfg.telegram.bot_token).build()
        )
        for name, fn in (
            ("start", self._cmd_status),
            ("status", self._cmd_status),
            ("add", self._cmd_add),
            ("remove", self._cmd_remove),
            ("list", self._cmd_list),
            ("buy", self._cmd_buy),
            ("sweep", self._cmd_sweep),
            ("pause", self._cmd_pause),
            ("resume", self._cmd_resume),
        ):
            self.app.add_handler(CommandHandler(name, fn))
        self.app.add_handler(CallbackQueryHandler(self._on_button))

    def _authorized(self, update: Update) -> bool:
        return (
            update.effective_chat is not None
            and update.effective_chat.id == self.cfg.telegram.chat_id
        )

    # -- outgoing alert ----------------------------------------------------
    async def send_deal_alert(
        self,
        result: PriceResult,
        ref_price: float,
        discount: float,
        evidence: str | None = None,
        auto_buying: bool = False,
    ) -> None:
        title = html.escape((result.title or result.asin)[:120])
        lines = [
            "🚨 <b>PRICE DROP</b> 🚨",
            f"<b>{title}</b>",
            f"💰 <b>{result.price:.2f} SAR</b>"
            + (f"  (was ~{ref_price:.0f} SAR, −{discount:.0f}%)" if ref_price > 0 else ""),
            f'🔗 <a href="{BASE}/dp/{result.asin}">{result.asin}</a>',
        ]
        if evidence:
            lines.append(f"🧾 <i>{html.escape(evidence)}</i>")
        keyboard = None
        if auto_buying:
            lines.append("⚡ <i>Auto-buy is ON — purchasing now…</i>")
        else:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🛒 BUY NOW", callback_data=f"buy:{result.asin}"),
                        InlineKeyboardButton("❌ Ignore", callback_data=f"skip:{result.asin}"),
                    ]
                ]
            )
        await self.app.bot.send_message(
            chat_id=self.cfg.telegram.chat_id,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=False,
        )

    async def notify_buy_result(self, asin: str, result: BuyResult) -> None:
        status = "✅" if result.ok else "❌"
        await self.app.bot.send_message(
            chat_id=self.cfg.telegram.chat_id, text=f"{status} {asin}: {result.message}"
        )
        if result.screenshot and result.screenshot.exists():
            with open(result.screenshot, "rb") as fh:
                await self.app.bot.send_photo(chat_id=self.cfg.telegram.chat_id, photo=fh)

    # -- purchase plumbing shared by button, /buy and auto-buy --------------
    async def do_buy(self, asin: str) -> BuyResult:
        result = await self.buyer.buy(asin)
        self.store.record_purchase(asin, 0.0, "ok" if result.ok else "failed", result.message)
        await self.notify_buy_result(asin, result)
        return result

    # -- button press ------------------------------------------------------
    async def _on_button(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        if not self._authorized(update):
            return

        action, _, asin = (query.data or "").partition(":")
        if action == "skip":
            await query.edit_message_reply_markup(reply_markup=None)
            return
        if action != "buy" or not _ASIN_RE.match(asin):
            return

        if not self.cfg.buy.enabled:
            await query.edit_message_reply_markup(reply_markup=None)
            await self.app.bot.send_message(
                chat_id=self.cfg.telegram.chat_id,
                text=f"⚠️ Auto-buy is disabled in config; buy manually: {BASE}/dp/{asin}",
            )
            return

        try:
            await query.edit_message_text(
                (query.message.text_html or "")
                + "\n\n⏳ Buying… (re-checking price at checkout)",
                parse_mode=ParseMode.HTML,
            )
        except Exception:  # noqa: BLE001 - editing is cosmetic; never block the buy
            pass
        await self.do_buy(asin)

    # -- commands ----------------------------------------------------------
    async def _cmd_add(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = ctx.args or []
        if not args or not _ASIN_RE.match(args[0].upper()):
            await update.message.reply_text(
                "Usage: /add <ASIN> <normal_price>\ne.g. /add B0ABC12345 499"
            )
            return
        asin = args[0].upper()
        ref = float(args[1]) if len(args) > 1 else 0.0
        self.store.upsert_product(asin, ref)
        await update.message.reply_text(f"Watching {asin} (ref {ref:.0f} SAR).")

    async def _cmd_remove(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = ctx.args or []
        if args and self.store.remove_product(args[0].upper()):
            await update.message.reply_text(f"Removed {args[0].upper()}.")
        else:
            await update.message.reply_text("Usage: /remove <ASIN>")

    async def _cmd_list(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        products = self.store.products()
        if not products:
            await update.message.reply_text(
                "Watchlist is empty (discovery still scans the whole catalog).\n"
                "Add priority items with /add <ASIN> <price>."
            )
            return
        rows = [
            f"• {p.asin} — last: "
            + (f"{p.last_price:.2f} SAR" if p.last_price is not None else "n/a")
            + (f" (ref {p.ref_price:.0f})" if p.ref_price else "")
            for p in products
        ]
        await update.message.reply_text("\n".join(rows))

    async def _cmd_buy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        args = ctx.args or []
        if not args or not _ASIN_RE.match(args[0].upper()):
            await update.message.reply_text("Usage: /buy <ASIN>")
            return
        if not self.cfg.buy.enabled:
            await update.message.reply_text("Auto-buy is disabled in config.")
            return
        await update.message.reply_text(f"⏳ Buying {args[0].upper()}…")
        await self.do_buy(args[0].upper())

    async def _cmd_sweep(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.sweep_now is None:
            await update.message.reply_text("Discovery is disabled in config.")
            return
        await update.message.reply_text("🔍 Sweeping the catalog now…")
        found = await self.sweep_now()
        await update.message.reply_text(
            f"Sweep done — {found} candidate(s) matched the deal rules."
        )

    async def _cmd_pause(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self.state.paused = True
        await update.message.reply_text("⏸ Monitoring paused. /resume to continue.")

    async def _cmd_resume(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self.state.paused = False
        await update.message.reply_text("▶️ Monitoring resumed.")

    async def _cmd_status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        d, disc = self.cfg.deal, self.cfg.discovery
        stats = self.store.discovery_stats()
        uptime_h = (time.time() - self.state.started_at) / 3600
        await update.message.reply_text(
            f"{'⏸ PAUSED' if self.state.paused else '▶️ Running'} — up {uptime_h:.1f} h\n"
            f"📋 Watchlist: {len(self.store.products())} item(s), "
            f"rule ≤ {d.max_price_sar:g} SAR"
            f"{f' AND ≥ {d.min_discount_pct:g}% off' if d.min_discount_pct else ''}\n"
            f"🔍 Catalog discovery: "
            + (
                f"{len(disc.categories)} categories, {self.state.sweeps_done} sweeps, "
                f"{self.state.candidates_seen} candidates "
                f"(alerted {stats.get('alerted', 0)}, "
                f"filtered {stats.get('junk', 0) + stats.get('lowscore', 0)}, "
                f"stale {stats.get('stale', 0)}, gone {stats.get('gone', 0)})\n"
                if disc.enabled
                else "OFF\n"
            )
            + f"🛒 Buy: {'ON' if self.cfg.buy.enabled else 'OFF'}"
            f"{' + AUTO-BUY' if self.cfg.buy.auto_buy else ''} "
            f"(hard cap {self.cfg.buy.max_auto_price_sar:g} SAR)"
        )
