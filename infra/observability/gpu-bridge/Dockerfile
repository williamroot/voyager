FROM python:3.12-slim
RUN pip install --no-cache-dir "redis>=5,<6"
WORKDIR /app
COPY gpu_bridge.py /app/gpu_bridge.py
EXPOSE 9835
# unbuffered so logs show up immediately
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/gpu_bridge.py"]
