FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN cd backend && pip install -r requirements.txt
WORKDIR /app/backend
CMD uvicorn app.main:app --host 0.0.0.0 --port $PORT
