#!/bin/sh
set -eu
# Separate PID namespace and container. No repository, no listener, no docker.sock.
nft -f /policy/rules.nft
touch /tmp/ready
exec sleep infinity
