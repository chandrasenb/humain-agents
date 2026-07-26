# Stage 1: build the React chat UI. Kept isolated so the runtime image never
# carries node/npm/node_modules — only the compiled static assets cross over.
FROM node:20-slim AS frontend-builder

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install

COPY frontend/ ./
RUN npm run build

# Stage 2: runtime image (unchanged shape from before the UI existed).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# src/main.py mounts this directory at "/" via StaticFiles if it exists —
# see its "Static frontend" section.
COPY --from=frontend-builder /frontend/dist ./static

# src/ modules import each other with bare names (import calendar_tools),
# so it needs to be on the path in addition to /app (which is what makes
# the `src.main:app` dotted module path below resolvable).
ENV PYTHONPATH=/app/src

EXPOSE 8080

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
