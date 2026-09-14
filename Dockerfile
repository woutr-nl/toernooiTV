FROM python:3.12-slim
WORKDIR /app
COPY portal/ portal/
COPY vendor/ vendor/
RUN useradd -r portal && mkdir -p /data portal/uploads && chown portal /data portal/uploads
ENV PORTAL_HOST=0.0.0.0 PORTAL_PORT=8771 PORTAL_DB=/data/portal.db
USER portal
EXPOSE 8771
# python as PID 1 ignores SIGTERM; main() exits cleanly on KeyboardInterrupt
STOPSIGNAL SIGINT
CMD ["python3", "portal/portal.py"]
