FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Docker builds run as root, so this can install Chromium's system
# dependencies directly (unlike Render's native Python runtime, which
# runs the build as an unprivileged user and can't escalate to install them).
RUN playwright install --with-deps chromium

COPY . .

CMD gunicorn app:app --bind 0.0.0.0:$PORT
