"""Telegram side: deal alerts with a Buy button, and watchlist commands.

Commands:
  /add <ASIN> <ref_price>   add/update a watched item
  /remove <ASIN>            stop watching an item
  /list                     show the watchlist with last-seen prices
  /status                   bot status
"""
from __future__ import annotations

import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .buyer import Buyer
from .config import Config
from .scraper import BASE, PriceResult
from .storage import Store

log = logging.getLogger("telegram")

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


class TelegramBot:
    def __init__(self, cfg: Config, store: Store, buyer: Buyer):
        self.cfg = cfg
        self.store = store
        self.buyer = buyer
        self.app: Application = (
            Application.builder().token(cfg.telegram.bot_token).build()
        )
        self.app.add_handler(CommandHandler("start", self._cmd_status))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("add", self._cmd_add))
        self.app.add_handler(CommandHandler("remove", self._cmd_remove))
        self.app.add_handler(CommandHandler("list", self._cmd_list))
        self.app.add_handler(CallbackQueryHandler(self._on_button))

    # -- outgoing alert ----------------------------------------------------
    async def send_deal_alert(
        self, result: PriceResult, ref_price: float, discount: float
    ) -> None:
        title = html.escape((result.title or result.asin)[:120])
        lines = [
            "🚨 <b>PRICE DROP</b> 🚨",
            f"<b>{title}</b>",
            f"💰 <b>{result.price:.2f} SAR</b>"
            + (f"  (was ~{ref_price:.0f} SAR, −{discount:.0f}%)" if ref_price > 0 else ""),
            f'🔗 <a href="{BASE}/dp/{result.asin}">{result.asin}</a>',
        ]
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

    # -- button press ------------------------------------------------------
    async def _on_button(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user is None:
            return
        await query.answer()
        if update.effective_chat is None or update.effective_chat.id != self.cfg.telegram.chat_id:
            return  # ignore buttons pressed outside your own chat

        action, _, asin = (query.data or "").partition(":")
        if action == "skip":
            await query.edit_message_reply_markup(reply_markup=None)
            return
        if action != "buy" or not _ASIN_RE.match(asin):
            return

        if not self.cfg.buy.enabled:
            await query.edit_message_text(
                (query.message.text_html or "") + "\n\n⚠️ Auto-buy is disabled in config.",
                parse_mode=ParseMode.HTML,
            )
            return

        await query.edit_message_text(
            (query.message.text_html or "") + "\n\n⏳ Buying… (re-checking price at checkout)",
            parse_mode=ParseMode.HTML,
        )
        result = await self.buyer.buy(asin)
        self.store.record_purchase(
            asin, 0.0, "ok" if result.ok else "failed", result.message
        )
        status = "✅ " if result.ok else "❌ "
        await self.app.bot.send_message(
            chat_id=self.cfg.telegram.chat_id, text=f"{status}{asin}: {result.message}"
        )
        if result.screenshot and result.screenshot.exists():
            with open(result.screenshot, "rb") as fh:
                await self.app.bot.send_photo(chat_id=self.cfg.telegram.chat_id, photo=fh)

    # -- commands ----------------------------------------------------------
    async def _cmd_add(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        if not args or not _ASIN_RE.match(args[0].upper()):
            await update.message.reply_text("Usage: /add <ASIN> <normal_price>\ne.g. /add B0ABC12345 499")
            return
        asin = args[0].upper()
        ref = float(args[1]) if len(args) > 1 else 0.0
        self.store.upsert_product(asin, ref)
        await update.message.reply_text(f"Watching {asin} (ref {ref:.0f} SAR).")

    async def _cmd_remove(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        if args and self.store.remove_product(args[0].upper()):
            await update.message.reply_text(f"Removed {args[0].upper()}.")
        else:
            await update.message.reply_text("Usage: /remove <ASIN>")

    async def _cmd_list(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        products = self.store.products()
        if not products:
            await update.message.reply_text("Watchlist is empty. Add with /add <ASIN> <price>.")
            return
        rows = [
            f"• {p.asin} — last: "
            + (f"{p.last_price:.2f} SAR" if p.last_price is not None else "n/a")
            + (f" (ref {p.ref_price:.0f})" if p.ref_price else "")
            for p in products
        ]
        await update.message.reply_text("\n".join(rows))

    async def _cmd_status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        d = self.cfg.deal
        await update.message.reply_text(
            f"👀 Watching {len(self.store.products())} item(s) on Amazon.sa\n"
            f"Alert rule: price ≤ {d.max_price_sar:g} SAR"
            f"{f' AND ≥ {d.min_discount_pct:g}% off' if d.min_discount_pct else ''}\n"
            f"Auto-buy: {'ON' if self.cfg.buy.enabled else 'OFF'} "
            f"(hard cap {self.cfg.buy.max_auto_price_sar:g} SAR)"
        )
