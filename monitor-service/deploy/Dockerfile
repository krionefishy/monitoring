FROM node:22-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.13-slim
WORKDIR /app
COPY monitor-service/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor-service/app/v2 /app/app/v2
COPY monitor-service/app/__init__.py /app/app/__init__.py
COPY monitor-service/main.py /app/main.py
COPY --from=ui /ui/dist /app/frontend
RUN groupadd -g 10001 monitor && useradd -u 10001 -g monitor -m monitor && mkdir -p /app/data && chown monitor:monitor /app/data
USER 10001:10001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
