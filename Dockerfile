# One image per study (docs/packaging.md): kami-agent + pinned kami-harness + the GDD
# snapshot at reference/. Per-run config.yaml and .env are injected at
# provision time; the closed-world egress allowlist (SPEC I20) is applied at the
# VM level — see docs/packaging.md.
FROM python:3.13-slim

ARG HARNESS_REPO=https://github.com/tokedo/kami-harness
ARG HARNESS_SHA=27592ce5b2ef6d0d9361b39a5dac87280320cfd9
ARG GDD_REPO=
ARG GDD_SHA=

RUN apt-get update \
    && apt-get install -y --no-install-recommends git cron \
    && rm -rf /var/lib/apt/lists/*

# kami-agent
COPY . /opt/kami-agent
RUN pip install --no-cache-dir /opt/kami-agent

# pinned kami-harness (stdio child, spawned per session)
RUN git clone --filter=blob:none "$HARNESS_REPO" /opt/kami-harness \
    && git -C /opt/kami-harness checkout "$HARNESS_SHA" \
    && pip install --no-cache-dir -r /opt/kami-harness/executor/requirements.txt

# GDD snapshot → the read-only reference/ tree (SPEC D5); optional at build
# time so dev images can be built without the docs repo.
RUN if [ -n "$GDD_REPO" ]; then \
    git clone --filter=blob:none "$GDD_REPO" /opt/gdd \
    && git -C /opt/gdd checkout "$GDD_SHA" \
    && mkdir -p /srv/run \
    && cp -r /opt/gdd /srv/run/reference \
    && rm -rf /srv/run/reference/.git; \
    fi

WORKDIR /srv/run
# Provisioning: mount/inject /srv/run/config.yaml and /srv/run/.env — the
# .env must set MAINNET_RPC_URL (the harness refuses to start without it)
# alongside the provider API key and owner wallet key — then
#   kami-agent init --manifest /srv/run/config.yaml --run-dir /srv/run
# and install the supervisor cron entry:
#   kami-agent run-session --run-dir /srv/run
CMD ["kami-agent", "status", "--run-dir", "/srv/run"]
