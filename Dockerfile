FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data /app/curated /app/cases /app/output

ENTRYPOINT ["python3"]
CMD ["mcp_server.py"]
