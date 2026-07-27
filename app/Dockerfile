# Sample workload used to demonstrate the sign + verify flow.
# Runs as a non-root user; no third-party dependencies.
FROM python:3.12-slim

WORKDIR /app
COPY main.py .

# Drop privileges
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

EXPOSE 8080
ENTRYPOINT ["python", "main.py"]
