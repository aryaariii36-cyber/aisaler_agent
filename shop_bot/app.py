"""
Entry point at project root — بعضی PaaS ها (مثل این مورد) دنبال یک فایل
app.py در ریشه‌ی پروژه می‌گردن که برنامه رو اجرا کنه. منطق واقعی در
پکیج app/ (app/main.py و بقیه) هست؛ این فایل فقط همون رو صدا می‌زنه.
"""

from app.main import main

if __name__ == "__main__":
    main()
