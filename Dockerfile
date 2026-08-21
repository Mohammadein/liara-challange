FROM python:3.12-slim

WORKDIR /srv

# میرور PyPI لیارا با fallback به خود PyPI.
# دلیل: سرورهای build لیارا به میرور داخلی سریع‌تر و مطمئن‌تر می‌رسند؛
# extra-index-url تضمین می‌کند اگر پکیجی روی میرور نبود، از PyPI بیاید.
ARG PIP_INDEX_URL=https://package-mirror.liara.ir/repository/pypi/simple
ARG PIP_EXTRA_INDEX_URL=https://pypi.org/simple

# وابستگی‌ها جدا از کد کپی می‌شوند تا کش لایه‌ها بین دیپلوی‌ها حفظ شود
COPY requirements.txt .
RUN pip install --no-cache-dir \
      --index-url "${PIP_INDEX_URL}" \
      --extra-index-url "${PIP_EXTRA_INDEX_URL}" \
      -r requirements.txt

COPY app/    ./app/
COPY static/ ./static/
COPY data/   ./data/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
