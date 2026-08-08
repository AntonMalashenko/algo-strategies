# Worker image for the DB-driven multi-account runner (webapp/runner.py) and,
# later, other strategies once webapp/runner.py's dispatch is generalized.
# NOT a long-running service: Ofelia spins up a fresh, short-lived container
# from this image per scheduled tick (job-run), matching the stateless-tick
# design already used by scripts/s007_tick.py / scripts/s009_tick.py -- see
# decisions-log.md 2026-07-23 for why (a persistent process silently stalled
# for ~17h under macOS App Nap).
FROM python:3.13-slim

WORKDIR /app

# System deps for cryptography/Twisted (pyOpenSSL needs a C toolchain + libssl
# headers to build on some platforms; python:slim ships neither by default).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libssl-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY . .

# No ENTRYPOINT/CMD: Ofelia's job-run `command` label decides what to run on
# each firing (e.g. `python -m webapp.runner --strategy S007`) -- one image,
# many possible invocations, per strategy.
