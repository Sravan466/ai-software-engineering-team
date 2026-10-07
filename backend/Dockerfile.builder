# The builder service (#75): runs generated code in throwaway containers for a hosted
# backend, which has no Docker of its own and must never run that code itself.
#
# Docker's CLI and a Python interpreter, nothing else: the service is standard library
# only (app/build/builder_service.py + app/build/sandbox.py). It needs the host's Docker
# socket — see docker-compose.yml — and none of the backend's data, keys or database.
FROM docker:27-cli

RUN apk add --no-cache python3

WORKDIR /srv
COPY app/__init__.py app/__init__.py
COPY app/build/__init__.py app/build/sandbox.py app/build/builder_service.py app/build/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4)"
CMD ["python3", "-m", "app.build.builder_service", "--port", "8100"]
