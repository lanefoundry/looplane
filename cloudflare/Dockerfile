FROM docker.io/cloudflare/sandbox:0.12.7-python@sha256:6dfa7301e69d3e5cd8e0404b92fd240026fe834ed7101ee29cb66337b0af0981

COPY .artifacts/rivumi-0.1.0-py3-none-any.whl /tmp/rivumi-0.1.0-py3-none-any.whl
COPY .artifacts/requirements.txt /tmp/requirements.txt
COPY rivumi-sandbox-run /usr/local/bin/rivumi-sandbox-run

RUN pip3 install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && pip3 install --no-cache-dir --no-deps /tmp/rivumi-0.1.0-py3-none-any.whl \
    && rm /tmp/rivumi-0.1.0-py3-none-any.whl /tmp/requirements.txt \
    && mkdir -p /opt/rivumi \
    && pip3 freeze --all | LC_ALL=C sort > /opt/rivumi/python-packages.txt \
    && groupadd --system rivumi \
    && useradd --system --gid rivumi --home-dir /home/rivumi --create-home --shell /usr/sbin/nologin rivumi \
    && chown root:root /usr/local/bin/rivumi-sandbox-run \
    && chmod 0555 /usr/local/bin/rivumi-sandbox-run

EXPOSE 8080
