# MACS-V2 background worker: signal check -> trade -> log -> sleep 900s -> repeat.
# No HTTP server, so no EXPOSE. Every secret (DATABASE_URL, DERIV_API_TOKEN,
# DERIV_APP_ID, DISCORD_WEBHOOK_URL) is read from the environment at runtime;
# .env is excluded by .dockerignore and never enters the image.

# Matches .python-version (3.12).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp1: xgboost's Linux wheel links against OpenMP, which slim images lack.
# tini: PID 1 that forwards SIGTERM to the whole process group (-g), so
# `docker stop` reaches the bash loop and its python child instead of waiting
# out the grace period and SIGKILLing them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /home/app app

WORKDIR /app

# Dependencies first, so code changes don't invalidate the install layer.
# constraints.txt pins every transitive dependency to what the local venv
# resolved; without it pip backtracks for a very long time over the
# unpinned fastapi/pydantic entries against alpaca-trade-api's old pins.
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

COPY --chown=app:app . .

# The app writes macs.log and .heartbeat_state.json into its working
# directory, so /app itself must be writable by the runtime user.
RUN chown app:app /app

USER app

ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
# Same start command the current deployment runs.
CMD ["/bin/bash", "start_macs.sh"]
