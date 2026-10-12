# ModelScope Docker Studio: port 7860 is health-only.
# Codeg is accessed through a Cloudflare remotely-managed Tunnel.
FROM ghcr.io/spacering-net/codeg:latest

# The official image contains Python 3, curl and the Codeg web/server binaries.
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) cf_arch=amd64 ;; \
      arm64) cf_arch=arm64 ;; \
      *) echo 'Unsupported architecture for cloudflared' >&2; exit 1 ;; \
    esac; \
    curl -fL --retry 3 --connect-timeout 15 \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cf_arch}" \
      -o /usr/local/bin/cloudflared; \
    chmod 0755 /usr/local/bin/cloudflared; \
    /usr/local/bin/cloudflared version

ENV PYTHONUNBUFFERED=1 \
    HOME=/mnt/workspace/codeg/userhome \
    CODEG_HOST=127.0.0.1 \
    CODEG_PORT=3080 \
    CODEG_DATA_DIR=/mnt/workspace/codeg/data \
    CODEG_HOME=/mnt/workspace/codeg/codeg-home \
    CODEG_BRIDGE_PORTS=off \
    CODEG_BROWSER_TUNNEL=off \
    HEALTH_PORT=7860 \
    TUNNEL_TRANSPORT_PROTOCOL=auto

WORKDIR /opt/codeg-lite
COPY supervisor.py /opt/codeg-lite/supervisor.py

# Parent image also documents EXPOSE 3080; only port 7860 is bound externally
# by ModelScope. Codeg itself binds 127.0.0.1, not 0.0.0.0.
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=4s --start-period=90s --retries=3 \
  CMD python3 -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:7860/health', timeout=2)" || exit 1

CMD ["python3", "-u", "/opt/codeg-lite/supervisor.py"]
