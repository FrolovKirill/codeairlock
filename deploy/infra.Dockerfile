FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
RUN apt-get update && apt-get install -y --no-install-recommends nftables iproute2 novnc websockify tini ca-certificates && rm -rf /var/lib/apt/lists/*
COPY deploy/guard.sh /opt/guard.sh
COPY scripts/gateway.py /opt/gateway.py
COPY scripts/search_admin.py /opt/search_admin.py
COPY scripts/mock_api.py /opt/mock_api.py
ENTRYPOINT ["/usr/bin/tini", "--"]
