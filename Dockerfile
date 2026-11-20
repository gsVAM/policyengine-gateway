# AGPL-3.0 wrapper (app.py) over the unmodified policyengine-us and policyengine-core
# packages from PyPI. Bump the pins in requirements.txt to move PolicyEngine versions.
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py gunicorn.conf.py ./
# Echoed as /calculate's identity; CI passes the git SHA and the workflow run id.
ARG GIT_SHA=unknown
ARG BUILD_ID=unknown
ENV WRAPPER_COMMIT=$GIT_SHA IMAGE_BUILD=$BUILD_ID PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["gunicorn", "-c", "gunicorn.conf.py"]
