# Sample workload used to demonstrate the sign + verify flow.
# Runs as a non-root user; no third-party dependencies.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

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
