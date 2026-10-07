"""
ایجنت جداگانه‌ی تحلیل «خصوصیات کاربر».

ورودی این ایجنت:
- کل تاریخچه‌ی مکالمه‌ی کاربر با بات اصلی، همراه با ساعت دقیق هر پیام
  (از "{schema}".messages).
- لیست کامل سفارش‌هایی که کاربر ثبت کرده (از "{schema}".orders).

خروجی: یک متن تحلیلی معمولی (نه خیلی کوتاه، نه خیلی طولانی) درباره‌ی
خصوصیات، سبک ارتباطی، الگوی زمانی فعالیت و علایق خرید کاربر - برای نمایش
مستقیم به ادمین در بات مدیریت.

سیاست اجرا:
- فقط برای کاربرانی که حداقل یک سفارش ثبت کرده‌اند قابل اجراست؛ در غیر این
  صورت get_user_characteristics مقدار None برمی‌گردونه (handlers.py مسئول
  اطلاع‌رسانی مناسب به ادمینه).
- نتیجه در "{schema}".user_profile کش می‌شه. دفعه‌ی بعد که ادمین همین کلید
  رو بزنه، اگه از آخرین بار تحلیل، پیام یا سفارش جدیدی از کاربر ثبت نشده
  باشه، همون نسخه‌ی کش‌شده بدون فراخوانی مجدد مدل برگردونده می‌شه؛ وگرنه
  (پیام/سفارش جدید ثبت شده) بلافاصله دوباره ساخته و جایگزین کش قبلی می‌شه.
"""

from typing import Any, Optional

from openai import AsyncOpenAI

from . import config, db, timeutils

# AsyncOpenAI (نه کلاینت sync): این ماژول توی همون event loop تکی و مشترکِ
# بات پنل ادمین (که فقط یک ادمین رو سرویس می‌ده، ولی همون معماری
# async def / await رو داره) صدا زده می‌شه. کلاینت sync یعنی هر بار که
# ادمین دکمه‌ی «خصوصیات کاربر» رو می‌زنه، کل event loop برای چند ثانیه
# (طول فراخوانی مدل) بلاک می‌شه.
_client = AsyncOpenAI(api_key=config.GAPGPT_API_KEY, base_url=config.GAPGPT_BASE_URL)


def _fmt_ts(ts: Any) -> str:
    return timeutils.format_tehran(ts)


_SYSTEM_PROMPT = """
تو یک تحلیل‌گر رفتار مشتری برای یک فروشگاه پوشاک آنلاین روی تلگرام هستی.
بر اساس تاریخچه‌ی کامل مکالمه‌ی یک کاربر (همراه با ساعت دقیق هر پیام) و
لیست سفارش‌هایی که ثبت کرده، یک تحلیل متنی درباره‌ی خصوصیات این کاربر برای
ادمین فروشگاه بنویس.

در تحلیلت - فقط در صورتی که واقعاً از داده‌ها قابل استخراج باشه - به این
محورها بپرداز:
- سبک و لحن ارتباطی کاربر (رسمی/غیررسمی، مردد/قاطع، کم‌حرف/پرحرف)
- الگوی زمانی فعالیت کاربر (مثلاً بیشتر چه ساعاتی از شبانه‌روز پیام می‌ده)
- نوع محصولات، سایز یا رنگی که بهشون علاقه نشون داده یا واقعاً خریده
- رفتار خرید (سریع تصمیم می‌گیره یا مردده، به قیمت حساسه یا نه، قبل از خرید
  چقدر سوال می‌پرسه)
- هر نکته‌ی دیگه‌ای که برای فروش بهتر به این کاربر در آینده به‌کار ادمین بیاد

قوانین مهم:
- خروجی فقط متن ساده‌ی فارسی باشه؛ بدون Markdown، بدون ستاره، بدون تیتر.
  می‌تونی از چند پاراگراف کوتاه یا خط تیره برای فهرست استفاده کنی.
- طول متن معمولی باشه: نه یکی دو خط خلاصه، نه خیلی طولانی و پرحرف - در حد
  یک تحلیل کامل و خوانا (تقریباً ۱۵۰ تا ۳۰۰ کلمه).
- فقط بر اساس داده‌های واقعی داده‌شده قضاوت کن؛ اگه داده‌ای برای یک محور
  وجود نداشت، همون محور رو کلاً حذف کن و الکی حدس نزن.
- لحنت حرفه‌ای و کاربردی باشه، انگار داری به همکار فروشنده‌ت درباره‌ی یک
  مشتری مشخص توضیح می‌دی.
""".strip()


def _build_user_prompt(messages: list[dict], orders: list[dict]) -> str:
    msg_lines = []
    for m in messages:
        role = "کاربر" if m["role"] == "user" else "فروشنده"
        msg_lines.append(f"[{_fmt_ts(m['created_at'])}] {role}: {m['content']}")
    messages_block = "\n".join(msg_lines) if msg_lines else "(پیامی ثبت نشده)"

    order_lines = []
    for o in orders:
        order_lines.append(
            f"- {o['product_name']} ({o['variant_desc']}) — {int(o['unit_price']):,} تومان "
            f"— وضعیت: {o['status']} — تاریخ ثبت: {_fmt_ts(o['created_at'])}"
        )
    orders_block = "\n".join(order_lines) if order_lines else "(سفارشی ثبت نشده)"

    return (
        "تاریخچه‌ی کامل مکالمه‌ی این کاربر با فروشگاه (به ترتیب زمان، همراه با ساعت دقیق هر پیام):\n"
        f"{messages_block}\n\n"
        "سفارش‌های ثبت‌شده توسط این کاربر:\n"
        f"{orders_block}"
    )


async def get_user_characteristics(chat_id: int) -> Optional[str]:
    """
    متن تحلیل خصوصیات کاربر رو برمی‌گردونه (از کش یا تازه‌ساخته‌شده).

    اگه کاربر هنوز هیچ سفارشی ثبت نکرده باشه، None برمی‌گردونه (یعنی این
    ویژگی برای این کاربر هنوز فعال نیست).
    """
    order_count = await db.get_order_count(chat_id)
    if order_count == 0:
        return None

    message_count = await db.get_message_count(chat_id)
    cached = await db.get_cached_profile(chat_id)

    is_fresh = (
        cached is not None
        and cached["source_message_count"] == message_count
        and cached["source_order_count"] == order_count
    )
    if is_fresh:
        return cached["profile_text"]

    messages = await db.get_full_history(chat_id, limit=config.PROFILE_MESSAGE_LIMIT)
    orders = await db.get_all_orders(chat_id)

    completion = await _client.chat.completions.create(
        model=config.GAPGPT_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(messages, orders)},
        ],
    )
    profile_text = completion.choices[0].message.content.strip()

    await db.save_profile(chat_id, profile_text, message_count, order_count)
    return profile_text
