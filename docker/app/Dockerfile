FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

<<<<<<< HEAD
CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:5000", "--timeout", "120", "app.app:app"]
=======
CMD ["gunicorn","--workers", " 4", "--bind","0.0.0.0:5000", "app.app:app"]
>>>>>>> e9695b2 (Use Gunicorn for production)
