import os
import tempfile

from telegram import Update
from telegram.ext import ContextTypes

from . import config, db, keyboards as kb, profile_agent, timeutils
from .pdf_export import build_chat_history_pdf

STATUS_LABELS = {
    "awaiting_payment": "در انتظار پرداخت",
    "paid_verified": "پرداخت تایید شده ✅",
    "rejected": "رد شده ❌",
}

# وضعیت ساده‌ی مکالمه‌ی ادمین (فقط یک ادمین داریم، پس یک دیکشنری کافیه)
_state: dict[int, dict] = {}


def _get_state(chat_id: int) -> dict:
    return _state.setdefault(
        chat_id,
        {"page": 0, "current_user": None, "shown_history": False, "shown_receipt": False, "shown_profile": False},
    )


def _remaining_menu(state: dict):
    """کیبورد جزئیات کاربر رو فقط با گزینه‌هایی که هنوز در همین نشست نشون داده نشدن می‌سازه."""
    return kb.user_detail_menu(
        show_history=not state["shown_history"],
        show_receipt=not state["shown_receipt"],
        show_profile=not state["shown_profile"],
    )


def _is_admin(chat_id: int) -> bool:
    return chat_id == config.ADMIN_CHAT_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔️ شما به این پنل دسترسی ندارید.")
        return
    _state[chat_id] = {
        "page": 0, "current_user": None, "shown_history": False, "shown_receipt": False, "shown_profile": False,
    }
    await update.message.reply_text(
        "به پنل ادمین خوش آمدید. لطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
        reply_markup=kb.MAIN_MENU,
    )


async def _guard(update: Update) -> bool:
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔️ شما به این پنل دسترسی ندارید.")
        return False
    return True


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    state = _get_state(chat_id)

    if text == kb.BTN_SALES:
        await _send_sales_report(update)
        return
    if text == kb.BTN_USERS:
        await _send_user_count(update)
        return
    if text == kb.BTN_INVENTORY:
        await _send_inventory(update)
        return
    if text == kb.BTN_NO:
        state.update(current_user=None, shown_history=False, shown_receipt=False, shown_profile=False)
        await update.message.reply_text("بازگشت به منوی اصلی.", reply_markup=kb.MAIN_MENU)
        return
    if text == kb.BTN_HISTORY:
        await _send_chat_history(update, state)
        return
    if text == kb.BTN_RECEIPT:
        await _send_payment_receipt(update, state)
        return
    if text == kb.BTN_PROFILE:
        await _send_user_profile(update, state)
        return

    # هر متن دیگه‌ای = نامعتبر، فقط از دکمه‌ها میشه استفاده کرد
    await update.message.reply_text("لطفاً فقط از دکمه‌های موجود استفاده کنید.", reply_markup=kb.MAIN_MENU)


# --------------------------------------------------------------------------
# گزارش فروش
# --------------------------------------------------------------------------

async def _send_sales_report(update: Update) -> None:
    report = await db.sales_report(limit=config.SALES_HISTORY_LIMIT)
    if report["count"] == 0:
        await update.message.reply_text("تا الان هیچ فروش تاییدشده‌ای ثبت نشده.", reply_markup=kb.MAIN_MENU)
        return

    header = (
        f"📊 گزارش فروش\n"
        f"تعداد کل فروش: {report['count']} مورد\n"
        f"جمع کل: {report['total_revenue']:,} تومان\n"
        f"—————————————\n"
    )
    lines = [header]
    for i, item in enumerate(report["items"], start=1):
        ts_str = timeutils.format_tehran(item["created_at"])
        lines.append(
            f"{i}. {item['product_name']} ({item['variant_desc']}) — {int(item['unit_price']):,} تومان\n"
            f"   خریدار: {item['chat_id']} | تاریخ: {ts_str}"
        )
    await _send_long_text(update, "\n".join(lines))


# --------------------------------------------------------------------------
# تعداد کاربران
# --------------------------------------------------------------------------

async def _send_user_count(update: Update) -> None:
    count = await db.count_users()
    await update.message.reply_text(
        f"👥 تعداد کل کاربران: {count} نفر",
        reply_markup=kb.user_list_entry_keyboard(),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await query.answer("⛔️ دسترسی ندارید.", show_alert=True)
        return
    await query.answer()

    data = query.data
    state = _get_state(chat_id)

    if data.startswith("adm_page_"):
        page = int(data.removeprefix("adm_page_"))
        await _render_users_page(query, page, state)
        return

    if data.startswith("adm_user_"):
        target_chat_id = int(data.removeprefix("adm_user_"))
        await _render_user_detail(update, target_chat_id, state)
        return


async def _render_users_page(query, page: int, state: dict) -> None:
    total = await db.count_users()
    total_pages = max(1, (total + config.USERS_PAGE_SIZE - 1) // config.USERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    users = await db.list_users(offset=page * config.USERS_PAGE_SIZE, limit=config.USERS_PAGE_SIZE)
    state["page"] = page

    text = f"👥 لیست کاربران — صفحه {page + 1} از {total_pages}\nروی هر کاربر بزنید تا اطلاعات کاملش را ببینید:"
    await query.edit_message_text(text, reply_markup=kb.users_page_keyboard(users, page, total_pages))


async def _render_user_detail(update: Update, target_chat_id: int, state: dict) -> None:
    info = await db.get_user_full_info(target_chat_id)
    if info is None:
        await update.effective_chat.send_message("این کاربر پیدا نشد.")
        return

    state.update(current_user=target_chat_id, shown_history=False, shown_receipt=False, shown_profile=False)

    orders = info["orders"]
    lines = [
        f"👤 اطلاعات کاربر {target_chat_id}",
        f"اولین بازدید: {_fmt(info['first_seen_at'])}",
        f"آخرین بازدید: {_fmt(info['last_seen_at'])}",
        f"تعداد سفارش‌ها: {len(orders)}",
        "—————————————",
    ]
    if orders:
        for o in orders:
            status = STATUS_LABELS.get(o["status"], o["status"])
            lines.append(
                f"#{o['id']} — {o['product_name']} ({o['variant_desc']}) — "
                f"{int(o['unit_price']):,} تومان — {status} — {_fmt(o['created_at'])}"
            )
    else:
        lines.append("هیچ سفارشی ثبت نکرده.")

    await update.effective_chat.send_message("\n".join(lines))
    await update.effective_chat.send_message(
        "چه اطلاعات دیگری از این کاربر می‌خواهید؟",
        reply_markup=kb.user_detail_menu(show_history=True, show_receipt=True, show_profile=True),
    )


def _fmt(ts) -> str:
    return timeutils.format_tehran(ts)


# --------------------------------------------------------------------------
# تاریخچه چت (PDF) و فیش پرداخت
# --------------------------------------------------------------------------

async def _send_chat_history(update: Update, state: dict) -> None:
    target_chat_id = state.get("current_user")
    if target_chat_id is None:
        await update.message.reply_text("ابتدا یک کاربر را از لیست انتخاب کنید.", reply_markup=kb.MAIN_MENU)
        return

    messages = await db.get_full_history(target_chat_id, limit=config.MESSAGES_PAGE_LIMIT)
    if not messages:
        await update.message.reply_text("این کاربر هیچ پیامی ندارد.")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, f"chat_history_{target_chat_id}.pdf")
            build_chat_history_pdf(target_chat_id, messages, pdf_path)
            with open(pdf_path, "rb") as f:
                await update.message.reply_document(f, filename=f"chat_history_{target_chat_id}.pdf")

    state["shown_history"] = True
    await update.message.reply_text("چه کار دیگری انجام بدم؟", reply_markup=_remaining_menu(state))


async def _send_payment_receipt(update: Update, state: dict) -> None:
    target_chat_id = state.get("current_user")
    if target_chat_id is None:
        await update.message.reply_text("ابتدا یک کاربر را از لیست انتخاب کنید.", reply_markup=kb.MAIN_MENU)
        return

    order = await db.get_latest_order_with_payment(target_chat_id)
    if order is None:
        await update.message.reply_text("هیچ فیش پرداختی برای این کاربر ثبت نشده.")
    else:
        status = STATUS_LABELS.get(order["status"], order["status"])
        caption = (
            f"سفارش #{order['id']} — {order['product_name']} ({order['variant_desc']})\n"
            f"مبلغ سفارش: {int(order['unit_price']):,} تومان\n"
            f"مبلغ تشخیص‌داده‌شده (OCR): {order['ocr_extracted_amount'] or '—'}\n"
            f"وضعیت تایید: {status}"
        )
        await update.message.reply_photo(order["payment_image"], caption=caption)

    state["shown_receipt"] = True
    await update.message.reply_text("چه کار دیگری انجام بدم؟", reply_markup=_remaining_menu(state))


# --------------------------------------------------------------------------
# خصوصیات کاربر (تحلیل رفتار توسط ایجنت جداگانه - app/profile_agent.py)
# --------------------------------------------------------------------------

async def _send_user_profile(update: Update, state: dict) -> None:
    target_chat_id = state.get("current_user")
    if target_chat_id is None:
        await update.message.reply_text("ابتدا یک کاربر را از لیست انتخاب کنید.", reply_markup=kb.MAIN_MENU)
        return

    wait_msg = await update.message.reply_text("⏳ در حال تحلیل خصوصیات کاربر، لطفاً چند لحظه صبر کنید...")

    try:
        profile_text = await profile_agent.get_user_characteristics(target_chat_id)
    except Exception:
        await wait_msg.edit_text("مشکلی در ساخت تحلیل خصوصیات این کاربر پیش اومد؛ لطفاً دوباره تلاش کنید.")
    else:
        if profile_text is None:
            await wait_msg.edit_text(
                "بررسی خصوصیات این کاربر هنوز امکان‌پذیر نیست؛ این گزینه فقط برای کاربرانی فعاله که "
                "حداقل یک سفارش ثبت کرده باشند."
            )
        else:
            await wait_msg.delete()
            header = f"🧠 خصوصیات کاربر {target_chat_id}\n—————————————\n"
            await _send_long_text_no_menu(update, header + profile_text)

    state["shown_profile"] = True
    await update.message.reply_text("چه کار دیگری انجام بدم؟", reply_markup=_remaining_menu(state))


# --------------------------------------------------------------------------
# موجودی انبار
# --------------------------------------------------------------------------

async def _send_inventory(update: Update) -> None:
    products = await db.inventory_snapshot()
    if not products:
        await update.message.reply_text("هیچ محصولی در کاتالوگ ثبت نشده.", reply_markup=kb.MAIN_MENU)
        return

    lines = ["📦 موجودی انبار", "—————————————"]
    for p in products:
        active = "" if p["is_active"] else " (غیرفعال)"
        lines.append(f"\n🔸 {p['name']}{active} — قیمت پایه: {int(p['base_price']):,} تومان")
        if not p["variants"]:
            lines.append("   بدون تنوع ثبت‌شده")
        for v in p["variants"]:
            price = int(v["price_override"]) if v["price_override"] else int(p["base_price"])
            va = "" if v["is_active"] else " (غیرفعال)"
            lines.append(f"   - سایز {v['size']} / {v['color']}{va}: موجودی {v['stock']} — قیمت {price:,} تومان")

    await _send_long_text(update, "\n".join(lines))


# --------------------------------------------------------------------------
# ابزار کمکی
# --------------------------------------------------------------------------

async def _send_long_text(update: Update, text: str, chunk_size: int = 3500) -> None:
    await _send_long_text_no_menu(update, text, chunk_size)
    await update.message.reply_text("منوی اصلی:", reply_markup=kb.MAIN_MENU)


async def _send_long_text_no_menu(update: Update, text: str, chunk_size: int = 3500) -> None:
    """مثل _send_long_text ولی در آخر منوی اصلی رو نمی‌فرسته - برای جاهایی که
    خود caller قراره یک کیبورد دیگه (مثلاً منوی جزئیات کاربر) رو بعدش بفرسته."""
    for i in range(0, len(text), chunk_size):
        await update.message.reply_text(text[i:i + chunk_size])
