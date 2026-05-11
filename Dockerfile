FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure DB directory exists (Railway uses ephemeral FS by default;
# for persistence add a Railway Volume mounted at /data)
ENV DB_PATH=/data/meals.db
RUN mkdir -p /data

EXPOSE 8080
ENV PORT=8080

CMD ["sh", "-c", "python -c 'from app import init_db; init_db()' && gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120"]
