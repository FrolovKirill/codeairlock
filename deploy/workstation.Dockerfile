FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5
RUN apt-get update && apt-get install -y --no-install-recommends python3 git ripgrep firefox-esr xvfb x11-xkb-utils xkb-data openbox x11vnc xterm ca-certificates curl procps tini fonts-dejavu && rm -rf /var/lib/apt/lists/*
RUN npm install -g typescript@5.9.3 typescript-language-server@6.0.0 pyright@1.1.414
RUN npm install -g @lancedb/lancedb@0.26.2
RUN apt-get update && apt-get install -y --no-install-recommends chromium && rm -rf /var/lib/apt/lists/*
COPY vendor/code-server.tar.gz /tmp/code-server.tar.gz
RUN mkdir /opt/code-server && tar -xzf /tmp/code-server.tar.gz --strip-components=1 -C /opt/code-server && rm /tmp/code-server.tar.gz
COPY vendor/kilo.vsix /tmp/kilo.vsix
RUN /opt/code-server/bin/code-server --extensions-dir /opt/extensions --install-extension /tmp/kilo.vsix && rm /tmp/kilo.vsix && chmod -R a+rX /opt/extensions
RUN find /opt/extensions -type f -path '*/bin/kilo' -exec ln -s '{}' /usr/local/bin/kilo \;
COPY deploy/workstation.py /opt/workstation.py
COPY deploy/keyboard_layout.py /opt/keyboard_layout.py
COPY scripts/search_mcp.py /opt/search_mcp.py
COPY scripts/probe.py /opt/probe.py
USER 1000:1000
ENV HOME=/home/node DISPLAY=:0 XDG_CONFIG_HOME=/home/node/.config XDG_DATA_HOME=/home/node/.local/share XDG_CACHE_HOME=/home/node/.cache
ENV KILO_CONFIG=/config/kilo.json KILO_DISABLE_AUTOUPDATE=1 KILO_DISABLE_MODELS_FETCH=1 KILO_DISABLE_LSP_DOWNLOAD=1 KILO_EXPERIMENTAL_LSP_TOOL=1 KILO_DISABLE_EXTERNAL_SKILLS=1 KILO_DISABLE_CLAUDE_CODE=1 KILO_NO_DAEMON=1 DO_NOT_TRACK=1
ENV KILO_LANCEDB_PATH=/usr/local/lib/node_modules/@lancedb/lancedb/dist/index.js
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/opt/workstation.py"]
