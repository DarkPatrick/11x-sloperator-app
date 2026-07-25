FROM ubuntu:24.04

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        iproute2 \
        openvpn \
        tinyproxy \
    && rm -rf /var/lib/apt/lists/*

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY vpn-up /usr/local/bin/vpn-up
COPY entrypoint /usr/local/bin/entrypoint

RUN chmod 0755 /usr/local/bin/vpn-up /usr/local/bin/entrypoint

ENTRYPOINT ["/usr/local/bin/entrypoint"]
