FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

RUN apt-get update && apt-get install -y --no-install-recommends \
    novnc websockify x11vnc xvfb python3-venv && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/browser-venv && /opt/browser-venv/bin/pip install --no-cache-dir \
    playwright==1.63.0 starlette==0.46.2 uvicorn==0.35.0
RUN useradd --system --uid 10001 --home-dir /tmp/x-browser --shell /usr/sbin/nologin xbrowser
COPY browser_worker/worker.py /opt/browser-worker/worker.py
COPY browser_worker/entrypoint.sh /opt/browser-worker/entrypoint.sh
RUN chmod 0555 /opt/browser-worker/entrypoint.sh
USER 10001:10001
WORKDIR /opt/browser-worker
ENV HOME=/tmp/x-browser DISPLAY=:99 PYTHONUNBUFFERED=1 PATH=/opt/browser-venv/bin:$PATH
EXPOSE 6080 8785
ENTRYPOINT ["/opt/browser-worker/entrypoint.sh"]
