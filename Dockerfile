FROM python:3.13-slim

RUN pip install --no-cache-dir fastapi==0.116.1 httpx==0.28.1 uvicorn==0.35.0
WORKDIR /app
COPY app.py /app/app.py
COPY isolated_status_projection.py /app/isolated_status_projection.py
COPY static /app/static
COPY version_static_assets.py /app/version_static_assets.py
RUN python version_static_assets.py static
USER 65532:65532
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
