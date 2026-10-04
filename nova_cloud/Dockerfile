# NOVA Cloud — identity, sync, telemetry and the admin console.
#
# Deliberately does not import NOVA's desktop stack: no torch, no audio, no
# GUI. The image stays small because this service does one job.
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not re-resolve the whole tree.
COPY nova_cloud/requirements.txt /app/nova_cloud/requirements.txt
RUN pip install --no-cache-dir -r /app/nova_cloud/requirements.txt

COPY nova_cloud/ /app/nova_cloud/

# Run as a non-root user. A web process that can rewrite its own code is a
# larger problem than the one it solves.
RUN useradd --create-home --uid 10001 nova && chown -R nova:nova /app
USER nova

EXPOSE 8080

# The health check queries the database rather than returning 200 blindly, so
# an orchestrator restarts the container when the dependency is what broke.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=8).status==200 else 1)"

# Timeout 300 s: streamed model answers pass through the gateway.
# Threads, not extra workers: the work is IO-bound on Postgres and Resend, and
# the Supabase pooler is the real connection limit.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "300", "--access-logfile", "-", "--error-logfile", "-", \
     "nova_cloud.app:create_app()"]
