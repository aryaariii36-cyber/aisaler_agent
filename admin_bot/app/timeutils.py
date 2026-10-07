"""
تبدیل ساعت برای *نمایش* به ادمین، بر اساس تهران.

نکته‌ی مهم (دلیل وجود این فایل):
سرور PostgreSQL روی فرانکفورته و ستون‌های تاریخ/ساعت از نوع TIMESTAMPTZ
هستن. تنظیم "timezone" روی connection pool (که قبلاً امتحان شده بود) در
عمل هیچ اثری روی چیزی که این بات می‌بینه نداره؛ چون asyncpg مقدار
TIMESTAMPTZ رو همیشه از طریق پروتکل باینری و به‌صورت یک datetime آگاه
(aware) با tzinfo=UTC برمی‌گردونه - صرف‌نظر از اینکه GUC ای "timezone" چی
ست شده باشه. اون تنظیم فقط روی تبدیل متن در خودِ SQL (مثل ::text یا
to_char) اثر می‌ذاره، نه روی مقداری که به پایتون می‌رسه.

پس تنها راه درست و قابل‌اتکا این‌ه که خودِ پایتون، هر datetime ای که
می‌خواد به ادمین نشون بده رو صریحاً به Asia/Tehran تبدیل کنه - همینجا.
این فایل مستقل از نسخه‌ی مشابهش توی بات اصلیه (دو پروژه/دیپلوی جدا).
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def now_tehran() -> datetime:
    """ساعت الان، به وقت تهران (آگاه/aware)."""
    return datetime.now(TEHRAN_TZ)


def to_tehran(dt: datetime | None) -> datetime | None:
    """یک datetime (معمولاً TIMESTAMPTZ خونده‌شده از دیتابیس، که همیشه
    UTC-aware برمی‌گرده) رو برای *نمایش* به وقت تهران تبدیل می‌کنه.

    اگه dt به هر دلیلی naive باشه (بدون tzinfo)، فرض می‌کنیم UTC بوده
    (چون همینطور توی دیتابیس ذخیره شده) و از همونجا تبدیل می‌کنیم.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TEHRAN_TZ)


def format_tehran(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    converted = to_tehran(dt)
    return converted.strftime(fmt) if converted else "-"
