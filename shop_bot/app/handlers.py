import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from telegram import Bot, Update
from telegram.ext import ContextTypes

from . import db
from .config import (
    ADMIN_CHAT_ID,
    GAPGPT_API_KEY,
    GAPGPT_BASE_URL,
    REMINDER_DELAY_SECONDS,
    STORE_NAME,
)
from .graph import EXPECTED_RECEIPT, check_receipt, extract_receipt_info, graph
from .invoice import build_invoice
from .llm_client import chat_llm

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_state_dict(chat_id: int, **extra) -> dict:
    session = await db.get_session(chat_id)
    history = await db.get_recent_history(chat_id, limit=20)
    return {
        "chat_id": chat_id,
        "session_state": session["state"],
        "selected_category_id": session.get("selected_category_id"),
        "selected_gender": session.get("selected_gender"),
        "selected_product_id": session["selected_product_id"],
        "selected_variant_id": session["selected_variant_id"],
        "pending_order_id": session["pending_order_id"],
        "customer_name": session.get("customer_name"),
        "customer_address": session.get("customer_address"),
        "history": history,
        **extra,
    }


async def _apply_graph_result(chat_id: int, result: dict) -> None:
    if result.get("reset_session"):
        await db.reset_session(chat_id)
        return

    updates = {}
    if "new_session_state" in result:
        updates["state"] = result["new_session_state"]
    if "new_selected_category_id" in result:
        updates["selected_category_id"] = result["new_selected_category_id"]
    if "new_selected_gender" in result:
        updates["selected_gender"] = result["new_selected_gender"]
    if "new_selected_product_id" in result:
        updates["selected_product_id"] = result["new_selected_product_id"]
    if "new_selected_variant_id" in result:
        updates["selected_variant_id"] = result["new_selected_variant_id"]
    if "new_pending_order_id" in result:
        updates["pending_order_id"] = result["new_pending_order_id"]
    if "new_customer_name" in result:
        updates["customer_name"] = result["new_customer_name"]
    if "new_customer_address" in result:
        updates["customer_address"] = result["new_customer_address"]
    if updates:
        await db.save_session(chat_id, **updates)


async def _send_reply(update: Update, result: dict) -> None:
    text = result.get("reply_text") or "..."
    markup = result.get("reply_markup")
    message = update.callback_query.message if update.callback_query else update.message
    await message.reply_text(text, reply_markup=markup)

    # پیام‌های بعدی (مثلاً شماره کارت بعد از فاکتور) به‌ترتیب، جدا از پیام اول
    chat_id = update.effective_chat.id
    for extra in result.get("followup_texts") or []:
        await message.reply_text(extra)
        await db.log_message(chat_id, "assistant", extra)


async def _send_final_invoice(bot: Bot, order_id: int) -> None:
    """فاکتور نهایی (با کد پیگیری و زمان تقریبی تحویل) بعد از تایید پرداخت.
    خطا در ارسالش نباید فرآیند تایید پرداخت رو خراب کنه."""
    try:
        order = await db.get_order(order_id)  # tracking_number بانکی الان ذخیره شده
        if not order:
            return
        text = await build_invoice(order, stage="paid")
        await bot.send_message(order["chat_id"], text)
        await db.log_message(order["chat_id"], "assistant", text)
    except Exception:
        log.exception("Could not send final invoice for order #%s", order_id)


# ---------------------------------------------------------------------------
# Inactivity reminder timer
#
# هر بار کاربر یک کار انجام میده (پیام/دکمه/عکس)، تایمر یادآوریِ همون کاربر
# ریست میشه (due_at قبلی جایگزین میشه). اگر تا قبل از رسیدن اون موعد، کاربر
# دوباره کاری بکنه، این تایمر دوباره ریست میشه؛ وگرنه دقیقاً همون یک بار
# یادآوری برای کاربر فرستاده میشه (طبق تصمیم: فقط یک یادآوری، نه یادآوری
# تکرارشونده). برای سفارش‌هایی که به نتیجه رسیدن (تایید/رد پرداخت) یا با
# /cancel لغو شدن، تایمر کنسل میشه چون دیگه موضوعی برای پیگیری نمونده.
#
# طراحی (بدون JobQueue، امن برای اجرای هم‌زمان چند نمونه از بات):
# قبلاً زمان‌بندی با python-telegram-bot JobQueue انجام می‌شد که کاملاً
# در-حافظه‌ی همون یک پروسه‌ست. این هم با ری‌استارت پاک می‌شد (و برای جبرانش
# یک منطق reschedule جدا لازم داشت) و هم - مهم‌تر - اگه چند نمونه از این
# بات هم‌زمان (پشت یک PaaS، برای مقیاس افقی) بالا باشن، هر نمونه JobQueue
# خودش رو داره و می‌تونه برای یک کاربر چند بار یادآوری بفرسته یا با
# race condition دچار رفتار نامشخص بشه.
# به‌جاش، due_at هر کاربر فقط توی جدول shopbot.pending_reminders (منبع
# حقیقتِ زمان‌بندی) نگه داشته می‌شه و یک background worker (پایین همین
# فایل: reminder_dispatch_loop) که هر REMINDER_POLL_INTERVAL_SECONDS ثانیه
# یک‌بار، روی *هر* نمونه‌ای از بات که در حال اجراست، موعدرسیده‌ها رو با
# db.claim_due_reminders (که از SELECT ... FOR UPDATE SKIP LOCKED استفاده
# می‌کنه) به‌صورت اتمیک claim و ارسال می‌کنه. با این روش هیچ کاربری یادآوری
# تکراری نمی‌گیره حتی اگه ده‌ها نمونه هم‌زمان در حال poll باشن، و ری‌استارت
# یا تغییر تعداد نمونه‌ها هیچ یادآوری‌ای رو گم نمی‌کنه.
# ---------------------------------------------------------------------------

async def _schedule_reminder(chat_id: int) -> None:
    due_at = datetime.now(timezone.utc) + timedelta(seconds=REMINDER_DELAY_SECONDS)
    await db.set_pending_reminder(chat_id, due_at)


async def _cancel_reminder(chat_id: int) -> None:
    await db.clear_pending_reminder(chat_id)


_REMINDER_SYSTEM_PROMPT = """
تو دستیار فروش فروشگاه پوشاک تلگرامی «{store}» هستی. کاربر وسط یک مکالمه،
بعد از آخرین پیامِ بات، مدتی جواب نداده و غیب شده. یک پیامِ یادآوریِ خیلی
کوتاه (فقط یکی دو جمله) و طبیعی بنویس که:
- دقیقاً به موضوع همین رد و بدل پیام‌های زیر مربوط باشه (نه یک پیام کلی).
- لحنش دوستانه و محترمانه باشه، کاربر رو تحت فشار نذاره.
- هیچ اطلاعات جدیدی (قیمت، شماره سفارش، تخفیف، وعده) از خودت نساز.
فقط و فقط خودِ متن پیام یادآوری رو بنویس؛ بدون گیومه و بدون توضیح اضافه.
""".strip()

_FALLBACK_REMINDER_TEXT = (
    "هنوز اونجایی؟ 🙂 هر وقت خواستی ادامه بدیم یا سوالی درباره محصولات داشتی، من اینجام."
)


async def _generate_reminder_text(chat_id: int) -> Optional[str]:
    """پیام یادآوریِ کانتکست‌محور، فقط بر اساس آخرین «مکالمه‌ی در حال انجام».

    چرا فقط ۶ تا پیام آخر کافی نیست: اگه کاربر خیلی قبل‌تر (مثلاً روزها
    پیش) یک بار درباره‌ی موضوع دیگه‌ای (مثلاً پرداخت) صحبت کرده باشه و الان
    فقط یک «درود» ساده گفته باشه، همون ۶ پیامِ آخرِ کلی می‌تونه شامل اون
    پیام‌های قدیمیِ نامرتبط هم بشه و باعث بشه یادآوری کاملاً بی‌ربط با
    آخرین رد و بدل واقعی ساخته بشه. برای همین، اول آخرین چند ده پیام رو
    می‌گیریم و بعد فقط بخشی از انتهای اون‌ها رو نگه می‌داریم که با هیچ شکاف
    زمانیِ بزرگ‌تر یا مساوی «فاصله‌ی یادآوری» (REMINDER_DELAY_SECONDS) از
    هم جدا نشده باشن؛ یعنی دقیقاً همون مکالمه‌ای که همین الان بعدش سکوت
    شده، نه هر مکالمه‌ی قدیمی‌تری.
    """
    history = await db.get_recent_history(chat_id, limit=30)
    if not history:
        return None

    session_gap = timedelta(seconds=REMINDER_DELAY_SECONDS)
    cutoff_index = 0
    for i in range(len(history) - 1, 0, -1):
        if history[i]["created_at"] - history[i - 1]["created_at"] >= session_gap:
            cutoff_index = i
            break
    recent_turns = history[cutoff_index:][-12:]

    history_text = "\n".join(
        f"{'کاربر' if turn['role'] == 'user' else 'بات'}: {turn['content']}"
        for turn in recent_turns
    )
    messages = [
        SystemMessage(content=_REMINDER_SYSTEM_PROMPT.format(store=STORE_NAME)),
        HumanMessage(content=f"آخرین پیام‌های مکالمه:\n{history_text}"),
    ]
    try:
        response = await chat_llm.ainvoke(messages)
        text = (response.content or "").strip()
        return text or None
    except Exception:
        log.exception("Reminder LLM generation failed for chat %s", chat_id)
        return _FALLBACK_REMINDER_TEXT


async def _send_reminder(bot: Bot, chat_id: int) -> None:
    # این تابع فقط بعد از claim شدنِ اتمیک ردیف (db.claim_due_reminders، که
    # همون‌جا ردیف pending_reminders رو هم حذف می‌کنه) صدا زده می‌شه، پس
    # اینجا دیگه نیازی به clear_pending_reminder نیست.
    session = await db.get_session(chat_id)

    # حالت پرداخت حساسه (پوله)؛ پیامش رو ثابت/قطعی نگه می‌داریم، نه دست LLM.
    if session.get("state") == "awaiting_payment_slip":
        text = (
            "یادت رفت عکس فیش پرداختت رو برام بفرستی 🙂 هر وقت آماده بود بفرست "
            "تا سفارشت نهایی بشه."
        )
    else:
        text = await _generate_reminder_text(chat_id)

    if not text:
        return

    try:
        await bot.send_message(chat_id, text)
    except Exception:
        log.exception("Could not send reminder message to chat %s", chat_id)
        return

    await db.log_message(chat_id, "assistant", text)


async def reminder_dispatch_loop(bot: Bot, poll_interval_seconds: int) -> None:
    """هر poll_interval_seconds ثانیه یک‌بار، یادآوری‌های موعدرسیده رو
    به‌صورت اتمیک claim و ارسال می‌کنه. مستقل از JobQueue و امن برای اجرای
    هم‌زمان روی چند نمونه از بات (به توضیح بالای همین فایل نگاه کن).
    از app.create_task در main.py صدا زده می‌شه و تا shutdown برنامه زنده
    می‌مونه؛ خطای موقتی دیتابیس/تلگرام باعث توقف کامل حلقه نمی‌شه."""
    while True:
        try:
            due = await db.claim_due_reminders()
            for row in due:
                try:
                    await _send_reminder(bot, row["chat_id"])
                except Exception:
                    log.exception("Reminder dispatch failed for chat %s", row["chat_id"])
        except Exception:
            log.exception("Reminder polling iteration failed.")
        await asyncio.sleep(poll_interval_seconds)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await db.reset_session(chat_id)
    text = "سلام! خوش اومدی 🌸 هر سوالی درباره محصولات داری بپرس، یا بگو «می‌خوام بخرم» تا لیست رو نشونت بدم."
    await db.log_message(chat_id, "assistant", text)
    await update.message.reply_text(text)
    await _schedule_reminder(chat_id)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await db.reset_session(chat_id)
    await update.message.reply_text("باشه، فرآیند فعلی لغو شد.")
    # کاربر خودش فرآیند رو لغو کرده؛ دیگه چیزی برای یادآوری نمونده.
    await _cancel_reminder(chat_id)


# ---------------------------------------------------------------------------
# Text messages
# ---------------------------------------------------------------------------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text or ""

    await db.log_message(chat_id, "user", text)

    state = await _load_state_dict(chat_id, input_type="text", text=text, callback_data=None)
    result = await graph.ainvoke(state)

    await _apply_graph_result(chat_id, result)
    if result.get("reply_text"):
        await db.log_message(chat_id, "assistant", result["reply_text"])
    await _send_reply(update, result)
    await _schedule_reminder(chat_id)


# ---------------------------------------------------------------------------
# Inline-button callbacks (product list / final confirmation)
# ---------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.from_user.id
    data = query.data

    state = await _load_state_dict(chat_id, input_type="callback", text=None, callback_data=data)
    result = await graph.ainvoke(state)

    await _apply_graph_result(chat_id, result)
    if result.get("reply_text"):
        await db.log_message(chat_id, "assistant", result["reply_text"])
    await _send_reply(update, result)
    await _schedule_reminder(chat_id)


# ---------------------------------------------------------------------------
# Payment slip photo
# ---------------------------------------------------------------------------

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = await db.get_session(chat_id)

    if session["state"] != "awaiting_payment_slip" or not session["pending_order_id"]:
        await update.message.reply_text(
            "الان منتظر عکس فیش نیستم. اگر می‌خوای خرید کنی، بگو «می‌خوام بخرم» 🙂"
        )
        await _schedule_reminder(chat_id)
        return

    order_id = session["pending_order_id"]
    order = await db.get_order(order_id)
    if not order or order["status"] != "awaiting_payment":
        await update.message.reply_text("این سفارش دیگه معتبر نیست، دوباره از اول شروع کن.")
        await db.reset_session(chat_id)
        await _cancel_reminder(chat_id)
        return

    photo = update.message.photo[-1]  # largest size
    tg_file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())

    # علاوه بر file_id تلگرام (که ممکنه بعد از مدتی نامعتبر بشه)، خودِ
    # فایل عکس فیش رو هم توی دیتابیس (ستون payment_image همین ردیف سفارش
    # در جدول مشترک shopbot.orders) نگه می‌داریم تا همیشه در دسترس بمونه.
    await db.update_order(
        order_id,
        payment_file_id=photo.file_id,
        payment_image=image_bytes,
        payment_image_mime="image/jpeg",
    )

    # مبلغ مورد انتظار باید مخصوص همین سفارش باشه، نه یک مقدار ثابت تستی.
    # unit_price توی دیتابیس به تومانه؛ فیلد amount در ReceiptInfo به ریال تعریف
    # شده (طبق توضیح فیلدش)، پس باید در ۱۰ ضرب بشه.
    order_expected_receipt = EXPECTED_RECEIPT.model_copy(
        update={"amount": int(order["unit_price"]) * 10}
    )

    try:
        extracted = await extract_receipt_info(image_bytes)
    except Exception:
        log.exception("Receipt AI verification failed for order #%s", order_id)
        await update.message.reply_text(
            "متاسفانه در بررسی فیش مشکلی پیش اومد؛ لطفاً دوباره عکس رو بفرست یا با پشتیبانی هماهنگ کن."
        )
        await _schedule_reminder(chat_id)
        return

    # شماره پیگیری هیچ‌وقت به‌عنوان معیار تایید چک نمی‌شه؛ فقط برای سابقه/رهگیری
    # ذخیره می‌شه. اگه به هر دلیلی (مثلاً migration دیتابیس هنوز اجرا نشده) این
    # ذخیره‌سازی fail بشه، نباید کل فرآیند تایید پرداخت رو متوقف کنه.
    try:
        await db.update_order(
            order_id,
            tracking_number=extracted.tracking_number or None,
            ocr_extracted_amount=extracted.amount or None,
        )
    except Exception:
        log.exception(
            "Could not persist tracking_number/ocr_extracted_amount for order #%s "
            "(did you re-run setup_database.py after the schema update?)",
            order_id,
        )

    try:
        checker_result = await check_receipt(
            extracted, order_expected_receipt, GAPGPT_API_KEY, base_url=GAPGPT_BASE_URL
        )
    except Exception:
        log.exception("Receipt AI verification failed for order #%s", order_id)
        await update.message.reply_text(
            "متاسفانه در بررسی فیش مشکلی پیش اومد؛ لطفاً دوباره عکس رو بفرست یا با پشتیبانی هماهنگ کن."
        )
        await _schedule_reminder(chat_id)
        return

    if checker_result.approved:
        ok = await db.decrement_stock(order["variant_id"])
        if not ok:
            await db.update_order(order_id, status="rejected")
            await db.reset_session(chat_id)
            await update.message.reply_text(
                "متاسفانه موجودی این کالا همین حالا تموم شد؛ لطفاً با پشتیبانی هماهنگ کن تا وجهت برگردونده بشه."
            )
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"⚠️ سفارش #{order_id}: پرداخت تایید شد ولی موجودی صفر بود. نیاز به بررسی دستی/عودت وجه.",
            )
            await _cancel_reminder(chat_id)
            return

        await db.update_order(order_id, status="paid_verified")
        await db.reset_session(chat_id)
        confirm_text = f"پرداخت سفارش #{order_id} تایید شد ✅ به‌زودی برات ارسال میشه. ممنون از خریدت 🌸"
        await update.message.reply_text(confirm_text)
        await db.log_message(chat_id, "assistant", confirm_text)
        await _send_final_invoice(context.bot, order_id)
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"✅ سفارش #{order_id} ({order['product_name']} / {order['variant_desc']}) "
            f"با تشخیص هوش مصنوعی خودکار تایید شد.",
        )
        await _cancel_reminder(chat_id)
        return

    # رد شده -> فقط به خود کاربر اطلاع بده (بدون دخالت ادمین)
    await db.update_order(order_id, status="rejected")
    await db.reset_session(chat_id)
    await update.message.reply_text(
        f"❌ فیش پرداخت شما تایید نشد.\nدلیل: {checker_result.reason}\n"
        "اگر فکر می‌کنی اشتباهی رخ داده، با پشتیبانی در ارتباط باش."
    )
    await _cancel_reminder(chat_id)


# ---------------------------------------------------------------------------
# Admin approve/reject callbacks (separate from the shopper's graph flow)
# ---------------------------------------------------------------------------

async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # admin_approve_<id> | admin_reject_<id>

    action, order_id_str = data.rsplit("_", 1)
    order_id = int(order_id_str)
    order = await db.get_order(order_id)
    if not order:
        await query.edit_message_caption(caption="سفارش پیدا نشد.")
        return

    buyer_chat_id = order["chat_id"]

    if action == "admin_approve":
        ok = await db.decrement_stock(order["variant_id"])
        if not ok:
            await db.update_order(order_id, status="rejected")
            await context.bot.send_message(
                buyer_chat_id, "متاسفانه موجودی این کالا تموم شده؛ پشتیبانی به‌زودی باهات هماهنگ می‌کنه."
            )
            await query.edit_message_caption(caption=f"سفارش #{order_id}: موجودی صفر بود، رد شد.")
        else:
            await db.update_order(order_id, status="paid_verified")
            await context.bot.send_message(
                buyer_chat_id, f"پرداخت سفارش #{order_id} توسط فروشگاه تایید شد ✅ به‌زودی ارسال میشه."
            )
            await _send_final_invoice(context.bot, order_id)
            await query.edit_message_caption(caption=f"سفارش #{order_id}: تایید شد ✅")
        await db.reset_session(buyer_chat_id)
        await _cancel_reminder(buyer_chat_id)

    elif action == "admin_reject":
        await db.update_order(order_id, status="rejected")
        await context.bot.send_message(
            buyer_chat_id,
            f"متاسفانه پرداخت سفارش #{order_id} تایید نشد. لطفاً با پشتیبانی در ارتباط باش یا دوباره فیش رو بررسی/ارسال کن.",
        )
        await query.edit_message_caption(caption=f"سفارش #{order_id}: رد شد ❌")
        await db.reset_session(buyer_chat_id)
        await _cancel_reminder(buyer_chat_id)
