ARG BASE_IMAGE=alpine:3.24.2
FROM ${BASE_IMAGE}
LABEL org.opencontainers.image.source="https://github.com/ShuaiZha/media-importer"
LABEL org.opencontainers.image.description="USB camera import and SMB archiving with Feishu controls"
RUN apk add --no-cache python3 py3-pip py3-cryptography ca-certificates
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv --system-site-packages /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY media_importer /app/media_importer
COPY scripts/migrate_v010.py /app/migrate_v010.py
ENTRYPOINT ["python3", "-m", "media_importer"]
CMD ["daemon"]
