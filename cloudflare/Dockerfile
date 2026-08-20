FROM docker.io/cloudflare/sandbox:0.12.7-python@sha256:6dfa7301e69d3e5cd8e0404b92fd240026fe834ed7101ee29cb66337b0af0981

COPY .artifacts/python_coding_agent-0.1.0-py3-none-any.whl /tmp/python_coding_agent-0.1.0-py3-none-any.whl
COPY .artifacts/requirements.txt /tmp/requirements.txt
COPY pca-sandbox-run /usr/local/bin/pca-sandbox-run

RUN pip3 install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && pip3 install --no-cache-dir --no-deps /tmp/python_coding_agent-0.1.0-py3-none-any.whl \
    && rm /tmp/python_coding_agent-0.1.0-py3-none-any.whl /tmp/requirements.txt \
    && mkdir -p /opt/pca \
    && pip3 freeze --all | LC_ALL=C sort > /opt/pca/python-packages.txt \
    && groupadd --system pca \
    && useradd --system --gid pca --home-dir /home/pca --create-home --shell /usr/sbin/nologin pca \
    && chown root:root /usr/local/bin/pca-sandbox-run \
    && chmod 0555 /usr/local/bin/pca-sandbox-run

EXPOSE 8080
