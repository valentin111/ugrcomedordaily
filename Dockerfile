# Alpine and the Python standard library keep the single-service image small.
FROM python:3.14-alpine
RUN apk add --no-cache tzdata ca-certificates \
    && addgroup -S app && adduser -S -G app app \
    && mkdir /data && chown app:app /data
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STATE_PATH=/data/delivery.sqlite3
COPY --chown=app:app app.py dish_images.py ./
USER app
ENTRYPOINT ["python", "app.py"]
CMD ["run"]
