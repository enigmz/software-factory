FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
USER root
RUN pip install --no-cache-dir pydantic==2.14.1 httpx==0.28.1 pytest==9.2.0 playwright==1.58.0
RUN groupadd -g 10001 factoryworker && useradd -u 10001 -g 10001 -M factoryworker
COPY factory/__init__.py factory/contracts.py factory/engine.py factory/browser.py factory/tool_output.py /opt/factory/
COPY docker/runner.py /opt/runner.py
ENV PYTHONPATH=/opt PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python", "-I", "/opt/runner.py"]
