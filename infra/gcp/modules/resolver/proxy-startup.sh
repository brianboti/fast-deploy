#!/bin/sh
set -eu
apt-get update
apt-get install -y --no-install-recommends squid
cat > /etc/squid/squid.conf <<'CONFIG'
http_port 3128
acl worker src 10.89.0.0/24
acl CONNECT method CONNECT
acl TLS port 443
acl approved dstdomain pypi.org files.pythonhosted.org registry.npmjs.org storage.googleapis.com
acl private dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 ::1/128 fc00::/7 fe80::/10
http_access deny private
http_access allow worker CONNECT TLS approved
http_access deny all
cache deny all
access_log none
cache_log /dev/null
forwarded_for delete
CONFIG
squid -k parse
systemctl restart squid
