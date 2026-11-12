FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# The runner talks to the Docker daemon through the mounted socket, which is
# root-owned with group `docker`. The GID is host-specific (984 on the current
# host, 999 on many Debian/Ubuntu installs), so hardcoding it silently produced
# a container that starts fine and then fails every daemon call with a
# permission error. Override with --build-arg DOCKER_GID=$(stat -c '%g'
# /var/run/docker.sock) when the host differs.
ARG DOCKER_GID=984
RUN groupadd -g "${DOCKER_GID}" docker \
 && useradd -r -u 1000 -g docker pi-runner

USER pi-runner

EXPOSE 8092

# Probes the app, not the daemon, but /health itself pings the daemon with a 5s
# timeout (HEALTH_TIMEOUT_S), so a wedged socket surfaces here quickly instead
# of hanging. Uses python because this image has no curl.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8092/health', timeout=5)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8092"]
