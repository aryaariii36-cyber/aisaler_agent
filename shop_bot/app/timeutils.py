"""
تبدیل ساعت برای *نمایش* به کاربر/ادمین، بر اساس تهران.

نکته‌ی مهم (دلیل وجود این فایل):
سرور PostgreSQL روی فرانکفورته و ستون‌های تاریخ/ساعت از نوع TIMESTAMPTZ
هستن. تنظیم "timezone" روی connection pool (که قبلاً امتحان شده بود) در
عمل هیچ اثری روی چیزی که این بات می‌بینه نداره؛ چون asyncpg مقدار
TIMESTAMPTZ رو همیشه از طریق پروتکل باینری و به‌صورت یک datetime آگاه
(aware) با tzinfo=UTC برمی‌گردونه - صرف‌نظر از اینکه GUC ای "timezone" چی
ست شده باشه. اون تنظیم فقط روی تبدیل متن در خودِ SQL (مثل ::text یا
to_char) اثر می‌ذاره، نه روی مقداری که به پایتون می‌رسه.

پس تنها راه درست و قابل‌اتکا این‌ه که خودِ پایتون، هر datetime ای که
می‌خواد به یک انسان نشون بده رو صریحاً به Asia/Tehran تبدیل کنه - همینجا.
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


# ---------------------------------------------------------------------------
# تاریخ شمسی (برای نمایش در فاکتور)
# ---------------------------------------------------------------------------

def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """تبدیل تاریخ میلادی به شمسی (الگوریتم استاندارد جلالی)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    )
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def format_jalali(dt: datetime | None, with_time: bool = False) -> str:
    """نمایش datetime (به وقت تهران) به‌صورت تاریخ شمسی، مثلاً 1405/07/06 14:30."""
    converted = to_tehran(dt)
    if converted is None:
        return "-"
    jy, jm, jd = gregorian_to_jalali(converted.year, converted.month, converted.day)
    text = f"{jy:04d}/{jm:02d}/{jd:02d}"
    if with_time:
        text += f" {converted.strftime('%H:%M')}"
    return text
