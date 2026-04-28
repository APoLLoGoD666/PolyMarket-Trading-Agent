FROM python:3.11

WORKDIR /home

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/home

CMD ["sh", "-c", "uvicorn scripts.python.server:app --host 0.0.0.0 --port ${PORT:-8080}"]
