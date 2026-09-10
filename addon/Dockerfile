FROM python:3.12-slim
WORKDIR /srv/addon
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV SIDECAR_URL=http://sidecar:7002
EXPOSE 8002
CMD ["granian", "--interface", "asgi", "app.main:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
