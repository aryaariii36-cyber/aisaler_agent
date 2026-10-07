"""
ساخت متن فاکتور برای مشتری.

دو مرحله داره:
  - stage="pending": بعد از گرفتن نام/آدرس، قبل از ارسال شماره کارت (در انتظار پرداخت)
  - stage="paid":    بعد از تایید پرداخت؛ همون فاکتور + کد پیگیری و زمان تقریبی تحویل

همه‌ی داده‌ها از خود دیتابیس میان (سفارش، محصول، واریانت، دسته‌بندی)، نه از LLM.
"""

from datetime import timedelta
from typing import Any, Optional

from . import db, timeutils
from .config import DELIVERY_MAX_DAYS, DELIVERY_MIN_DAYS, STORE_NAME

_LINE = "━━━━━━━━━━━━━━━━━━"


def order_code(order_id: int) -> str:
    """کد پیگیری سفارش (مخصوص خود فروشگاه؛ جدا از شماره پیگیری تراکنش بانکی)."""
    return f"SH-{int(order_id):06d}"


def _delivery_range_text() -> str:
    now = timeutils.now_tehran()
    start = timeutils.format_jalali(now + timedelta(days=DELIVERY_MIN_DAYS))
    end = timeutils.format_jalali(now + timedelta(days=DELIVERY_MAX_DAYS))
    return (
        f"{DELIVERY_MIN_DAYS} تا {DELIVERY_MAX_DAYS} روز کاری\n"
        f"   (بین {start} تا {end})"
    )


async def build_invoice(order: dict[str, Any], stage: str = "pending") -> str:
    """stage: 'pending' | 'paid'"""
    variant: Optional[dict] = await db.get_variant(order["variant_id"])
    product: Optional[dict] = (
        await db.get_product(variant["product_id"]) if variant else None
    )
    category: Optional[dict] = (
        await db.get_category(product["category_id"])
        if product and product.get("category_id")
        else None
    )

    price = int(order["unit_price"])
    created = timeutils.format_jalali(order["created_at"], with_time=True)

    lines = [
        f"🧾 فاکتور سفارش #{order['id']}" + (" (نهایی)" if stage == "paid" else ""),
        _LINE,
        f"🏬 فروشگاه: {STORE_NAME}",
        f"📅 تاریخ ثبت: {created}",
        "",
        "👤 مشخصات خریدار",
        f"نام و نام خانوادگی: {order.get('customer_name') or '-'}",
        f"آدرس: {order.get('customer_address') or '-'}",
        "",
        "🛍 مشخصات کالا",
        f"نام محصول: {order['product_name']}",
    ]
    if category:
        lines.append(f"دسته‌بندی: {category['name']}")
    if product and product.get("gender"):
        lines.append(f"مناسب: {product['gender']}")
    if variant:
        lines.append(f"سایز: {variant['size']}")
        lines.append(f"رنگ: {variant['color']}")
        if variant.get("sku"):
            lines.append(f"کد کالا: {variant['sku']}")
    else:
        lines.append(f"مشخصات: {order['variant_desc']}")
    if product and (product.get("description") or "").strip():
        lines.append(f"توضیحات: {product['description'].strip()}")
    lines += [
        "",
        "💰 مبلغ",
        f"تعداد: 1",
        f"قیمت واحد: {price:,} تومان",
        f"مبلغ قابل پرداخت: {price:,} تومان",
        _LINE,
    ]

    if stage == "paid":
        lines += [
            "✅ وضعیت: پرداخت تایید شد",
            f"🔖 کد پیگیری سفارش: {order_code(order['id'])}",
        ]
        if order.get("tracking_number"):
            lines.append(f"🏦 شماره پیگیری تراکنش: {order['tracking_number']}")
        lines.append(f"🚚 زمان تقریبی تحویل: {_delivery_range_text()}")
        lines += [_LINE, "ممنون از خریدت 🌸"]
    else:
        lines += ["⏳ وضعیت: در انتظار پرداخت"]

    return "\n".join(lines)
