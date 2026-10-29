# Sample workload used to demonstrate the sign + verify flow.
# Runs as a non-root user; no third-party dependencies.
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

# The pod runs with a read-only root filesystem, so no bytecode cache.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY main.py .

# Drop privileges
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

EXPOSE 8080
ENTRYPOINT ["python", "main.py"]
