FROM python:3.12-slim
WORKDIR /app
COPY src/main.py .
EXPOSE 8080
CMD ["python", "main.py"]
