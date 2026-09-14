FROM python:3.12-slim
WORKDIR /app
COPY portal/requirements.txt portal/requirements.txt
RUN pip install --no-cache-dir -r portal/requirements.txt
COPY portal/ portal/
COPY vendor/ vendor/
RUN useradd -r portal && mkdir -p portal/uploads && chown portal portal/uploads
ENV PORTAL_HOST=0.0.0.0 PORTAL_PORT=8771
USER portal
EXPOSE 8771
# python as PID 1 ignores SIGTERM; main() exits cleanly on KeyboardInterrupt
STOPSIGNAL SIGINT
CMD ["python3", "portal/portal.py"]
