ARG PYTHON_IMAGE=docker.io/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

FROM ${PYTHON_IMAGE} AS runtime
ARG WITH_VOICE=true
RUN if [ "$WITH_VOICE" = "true" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends ffmpeg && \
        rm -rf /var/lib/apt/lists/*; \
    fi
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes --no-deps \
    -r /tmp/requirements.lock
RUN groupadd --system --gid 10001 matrix-for-ai && \
    useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin matrix-for-ai && \
    install -d -o 10001 -g 10001 -m 0700 /data /data/mailboxes /data/crypto
WORKDIR /app
COPY matrix_for_ai /app/matrix_for_ai
ENV PYTHONUNBUFFERED=1 MATRIX_FOR_AI_CONFIG=/etc/matrix-for-ai/config.json
EXPOSE 7700 7701
USER 10001:10001
CMD ["python", "-u", "-m", "matrix_for_ai"]


FROM runtime AS test
USER root
COPY requirements-test.lock requirements.lock /tmp/
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements-test.lock
COPY tests /app/tests
USER 10001:10001
RUN python -m pytest -q -p no:cacheprovider tests


FROM runtime
