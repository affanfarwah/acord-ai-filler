# ACORD AI Filler: one image, one process, serving the pages, the API and the PDFs from one origin.
#
# One process on purpose: AI jobs run in background threads and the data lives in SQLite on a
# Railway volume. Several workers or replicas would each run their own jobs against one database
# file, so the start command pins a single Uvicorn worker.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Dependencies first, so a code-only change does not reinstall them.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY templates ./templates

# The blank ACORD forms must be in the image. Without them the app starts, answers the health
# check, and every client's Forms tab is empty.
RUN test "$(ls templates/forms/*.pdf | wc -l)" -ge 7 || (echo "BUILD FAILED: ACORD templates missing" && exit 1)

# Database, uploads and the session key live here. Mount the Railway volume at /data, or every
# redeploy starts with no clients and signs everyone out.
ENV ACORD_DATA=/data
EXPOSE 8000

# Railway injects PORT. --proxy-headers: the app sits behind Railway's HTTPS proxy.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
