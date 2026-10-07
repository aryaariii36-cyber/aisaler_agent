import os

import arabic_reshaper
from bidi.algorithm import get_display
from fpdf import FPDF

from . import timeutils

_FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "Vazirmatn-Regular.ttf")


def _rtl(text: str) -> str:
    """متن فارسی/عربی رو برای نمایش درست (raw shaping + بازچینی راست‌به‌چپ) آماده می‌کنه."""
    reshaped = arabic_reshaper.reshape(text or "")
    return get_display(reshaped)


class _PersianPDF(FPDF):
    def header(self):
        pass


def build_chat_history_pdf(chat_id: int, messages: list[dict], out_path: str) -> str:
    pdf = _PersianPDF(format="A4")
    pdf.add_font("Vazir", "", _FONT_PATH)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Vazir", size=14)
    pdf.cell(0, 10, _rtl(f"تاریخچه چت کاربر {chat_id}"), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Vazir", size=9)
    pdf.cell(0, 8, _rtl(f"تعداد پیام: {len(messages)}"), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Vazir", size=11)
    for m in messages:
        role = "کاربر" if m["role"] == "user" else "دستیار"
        ts_str = timeutils.format_tehran(m["created_at"], "%Y-%m-%d %H:%M:%S")
        header = _rtl(f"[{ts_str}] {role}:")
        pdf.set_text_color(30, 30, 120 if m["role"] == "user" else 0)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 7, header, align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 7, _rtl(m["content"] or ""), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    pdf.output(out_path)
    return out_path
