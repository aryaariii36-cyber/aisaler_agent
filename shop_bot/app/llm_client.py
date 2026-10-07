"""
یک نمونه‌ی مشترک از ChatOpenAI که به‌جای OpenAI واقعی، به GapGPT
(یک پروکسی OpenAI-compatible) وصل میشه. تمام مقادیر از متغیرهای محیطی
خونده میشن، پس برای عوض کردن مدل یا کلید فقط env رو عوض کن، نیازی به
تغییر کد نیست.
"""

from langchain_openai import ChatOpenAI

from . import config


def get_llm(temperature: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(
        model=config.GAPGPT_MODEL,
        base_url=config.GAPGPT_BASE_URL,
        api_key=config.GAPGPT_API_KEY,
        temperature=temperature,
    )


# دو نمونه‌ی آماده: یکی برای طبقه‌بندی (باید دقیق و بدون خلاقیت باشه)
# و یکی برای چت طبیعی با کاربر.
classifier_llm = get_llm(temperature=0.0)
chat_llm = get_llm(temperature=0.6)
