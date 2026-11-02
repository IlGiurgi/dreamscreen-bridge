ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3

COPY bridge.py /bridge.py

CMD ["python3", "-u", "/bridge.py", "--config", "/data/options.json"]
