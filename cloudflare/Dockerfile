FROM docker.io/cloudflare/sandbox:0.12.7-python@sha256:6dfa7301e69d3e5cd8e0404b92fd240026fe834ed7101ee29cb66337b0af0981

COPY .artifacts/looplane-0.1.0-py3-none-any.whl /tmp/looplane-0.1.0-py3-none-any.whl
COPY .artifacts/requirements.txt /tmp/requirements.txt
COPY looplane-sandbox-run /usr/local/bin/looplane-sandbox-run

RUN pip3 install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && pip3 install --no-cache-dir --no-deps /tmp/looplane-0.1.0-py3-none-any.whl \
    && rm /tmp/looplane-0.1.0-py3-none-any.whl /tmp/requirements.txt \
    && mkdir -p /opt/looplane \
    && pip3 freeze --all | LC_ALL=C sort > /opt/looplane/python-packages.txt \
    && groupadd --system looplane \
    && useradd --system --gid looplane --home-dir /home/looplane --create-home --shell /usr/sbin/nologin looplane \
    && chown root:root /usr/local/bin/looplane-sandbox-run \
    && chmod 0555 /usr/local/bin/looplane-sandbox-run

EXPOSE 8080
