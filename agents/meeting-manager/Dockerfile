FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# src/ modules import each other with bare names (import calendar_tools),
# so it needs to be on the path in addition to /app (which is what makes
# the `src.main:app` dotted module path below resolvable).
ENV PYTHONPATH=/app/src

EXPOSE 8080

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
