FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY release_arr.py .

CMD ["python", "-u", "release_arr.py"]
