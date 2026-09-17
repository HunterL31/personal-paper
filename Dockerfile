# Personal Paper.
#
# Playwright's own image, at the tag that matches playwright==1.56.0 in
# requirements.txt: Chromium and every system library it needs are already
# installed under /ms-playwright. Never run `playwright install` here, and
# never let this tag drift from the pin in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    TZ=America/Los_Angeles

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
# --break-system-packages: Ubuntu noble marks its Python as externally
# managed, and this image is a single-application container.
COPY requirements.txt ./
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

# Stamped by the publish workflow so the page footer can say which build is
# running; "dev" for a local build.
ARG GIT_SHA=dev
ENV APP_BUILD=$GIT_SHA

# Settings, state, logs, the archive and the day's output all live here.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# One process: the web page, the task endpoint and the scheduler.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
