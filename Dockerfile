# Production image for the Hospital Bulk Processing API.
#
# The image is built from requirements.txt alone (uvicorn, the web server,
# is a runtime dependency). Application code and configuration are not baked
# into the image: every tunable comes from the process environment at
# start-up, so a single image serves every environment.

FROM python:3.12-slim

# Predictable, fast builds: no bytecode, unbuffered logs, pinned pip.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so the layer is reused when only the
# application changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app ./app

# Run as an unprivileged user: the service binds a non-privileged port and
# needs no filesystem access outside /app.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

# Render supplies PORT; the default matches the local development port.
ENV PORT=8000
EXPOSE 8000

# Shell form so the environment is consulted at start-up time. ``exec``
# replaces the shell with uvicorn so it becomes the container's main process
# and receives SIGTERM directly: on ``docker stop`` (or a Render redeploy)
# uvicorn shuts down gracefully and the ASGI lifespan closes the shared HTTP
# client instead of the process being killed mid-shutdown.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
