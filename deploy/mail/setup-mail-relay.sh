#!/usr/bin/env bash
# Send-only Postfix + OpenDKIM relay for a self-hosted Dojo instance.
#
# Dojo (in Docker) hands mail to Postfix on the host; Postfix DKIM-signs it
# and delivers it straight to recipients' mail servers. Nothing is accepted
# from the internet — only from localhost and Docker's bridge networks.
#
# Usage (as root, on the Docker host):
#   bash deploy/mail/setup-mail-relay.sh dojo.yourclub.co.uk
#
# The domain is what mail is sent from (noreply@<domain>). A subdomain that
# already points at this server, like the one Dojo is served on, works well:
# it keeps the main domain's email reputation separate, and doubles as the
# server's mail hostname.
#
# Optional environment overrides:
#   PUBLIC_IP      (default: this host's outbound IPv4 address) set it if the
#                  host is behind NAT; the DNS records must name the public one
#   MAIL_HOSTNAME  (default <domain> if it already points at this server,
#                  otherwise mail.<domain>)  must match the server IP's PTR record
#   DKIM_SELECTOR  (default dojo)
#   DKIM_BITS      (default 2048; use 1024 if your DNS host rejects long TXT records)
#   TRUSTED_NETWORKS  (default: every existing Docker bridge subnet)
#                  space-separated CIDRs allowed to relay
#   REPLACE_EXISTING_CONFIG=1  go ahead on a host that already has Postfix or
#                  OpenDKIM configured (the old files are backed up first)
#
# Run it with Dojo already up, so its Docker network exists. If that network
# is ever recreated on a different subnet, Postfix will reject Dojo's mail
# until you re-run this script.
#
# Safe to re-run: the DKIM key is only generated once, so records already
# published in DNS stay valid.
set -euo pipefail

DOMAIN="${1:-}"
if [[ -z "$DOMAIN" ]]; then
    echo "Usage: $0 <sending-domain>   e.g. $0 dojo.yourclub.co.uk" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "Run this as root." >&2
    exit 1
fi

ip_to_int() {
    local IFS=.
    local a b c d
    read -r a b c d <<<"$1"
    echo $(( (a << 24) | (b << 16) | (c << 8) | d ))
}
# Whether two IPv4 CIDRs share any address: they agree on the shorter prefix.
cidrs_overlap() {
    local len1=32 len2=32
    [[ "$1" == */* ]] && len1="${1#*/}"
    [[ "$2" == */* ]] && len2="${2#*/}"
    local len=$(( len1 < len2 ? len1 : len2 ))
    local mask=$(( len == 0 ? 0 : (0xFFFFFFFF << (32 - len)) & 0xFFFFFFFF ))
    (( ($(ip_to_int "${1%/*}") & mask) == ($(ip_to_int "${2%/*}") & mask) ))
}

# The address mail leaves from, which the DNS records must name. Behind NAT
# the local address is private and wrong, so require it to be given.
PUBLIC_IP="${PUBLIC_IP:-$(ip -4 route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')}"
for private in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10; do
    if cidrs_overlap "$PUBLIC_IP" "$private"; then
        echo "This server's address ($PUBLIC_IP) is private, so it's behind NAT and the" >&2
        echo "DNS records would be wrong. Re-run with PUBLIC_IP set to the public address" >&2
        echo "this server sends mail from." >&2
        exit 1
    fi
done

points_here() {
    getent ahostsv4 "$1" | awk '{print $1}' | grep -qx "$PUBLIC_IP"
}
if [[ -z "${MAIL_HOSTNAME:-}" ]]; then
    if points_here "$DOMAIN"; then MAIL_HOSTNAME="$DOMAIN"; else MAIL_HOSTNAME="mail.$DOMAIN"; fi
fi
DKIM_SELECTOR="${DKIM_SELECTOR:-dojo}"
DKIM_BITS="${DKIM_BITS:-2048}"
KEY_DIR="/etc/opendkim/keys/$DOMAIN"

# Networks allowed to relay (and get DKIM-signed): only the Docker bridge
# subnets that actually exist, never a whole private range.
if [[ -n "${TRUSTED_NETWORKS:-}" ]]; then
    DOCKER_SUBNETS="$TRUSTED_NETWORKS"
else
    DOCKER_SUBNETS=""
    if command -v docker >/dev/null; then
        for net in $(docker network ls -q --filter driver=bridge); do
            for subnet in $(docker network inspect "$net" -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}'); do
                [[ "$subnet" == *:* ]] && continue  # IPv4 only
                [[ " $DOCKER_SUBNETS " == *" $subnet "* ]] || DOCKER_SUBNETS="$DOCKER_SUBNETS $subnet"
            done
        done
    fi
    DOCKER_SUBNETS="${DOCKER_SUBNETS# }"
    if [[ -z "$DOCKER_SUBNETS" ]]; then
        echo "No Docker bridge networks found. Start Dojo first (docker compose up -d)," >&2
        echo "or set TRUSTED_NETWORKS to the subnets allowed to relay." >&2
        exit 1
    fi
fi
echo "==> Networks allowed to relay: $DOCKER_SUBNETS"

# The interfaces Docker created, named from its own metadata: docker0 for the
# default network, br-<network id> unless a network sets its own bridge name.
DOCKER_BRIDGES="docker0"
if command -v docker >/dev/null; then
    for net in $(docker network ls -q --no-trunc --filter driver=bridge); do
        name="$(docker network inspect "$net" -f '{{index .Options "com.docker.network.bridge.name"}}')"
        [[ "$name" == "<no value>" ]] && name=""  # how some Docker versions print a missing option
        DOCKER_BRIDGES="$DOCKER_BRIDGES ${name:-br-${net:0:12}}"
    done
fi

# Trusting those networks is only safe while Docker is the only thing on them.
# Any other interface in the same range could relay through Postfix. Checked
# before anything is installed or changed.
OVERLAPS=""
while read -r _ iface _ addr _; do
    iface="${iface%%@*}"
    [[ " lo $DOCKER_BRIDGES " == *" $iface "* ]] && continue
    for trusted in $DOCKER_SUBNETS; do
        if cidrs_overlap "$addr" "$trusted"; then
            OVERLAPS="$OVERLAPS    $iface $addr"$'\n'
            break
        fi
    done
done < <(ip -4 -o addr show)
if [[ -n "$OVERLAPS" ]]; then
    echo "These non-Docker interfaces overlap the networks allowed to relay mail:" >&2
    echo -n "$OVERLAPS" >&2
    echo "Set TRUSTED_NETWORKS to your Docker subnets only (see 'docker network inspect')." >&2
    exit 1
fi

# This sets the host up as a dedicated send-only relay, replacing Postfix's
# and OpenDKIM's configuration, so don't silently take over existing mail.
MARKER="# Managed by Dojo's deploy/mail/setup-mail-relay.sh"
EXISTING=""
for f in /etc/postfix/main.cf /etc/opendkim.conf; do
    if [[ -f "$f" ]] && ! grep -qF "$MARKER" "$f"; then EXISTING="$EXISTING $f"; fi
done
if [[ -n "$EXISTING" ]]; then
    if [[ "${REPLACE_EXISTING_CONFIG:-}" != 1 ]]; then
        echo "Mail is already configured on this host:$EXISTING" >&2
        echo "This script would replace that configuration. To go ahead anyway, re-run" >&2
        echo "with REPLACE_EXISTING_CONFIG=1; each file is backed up to <file>.pre-dojo first." >&2
        exit 1
    fi
    for f in $EXISTING; do cp -n "$f" "$f.pre-dojo"; done
fi

echo "==> Installing Postfix and OpenDKIM"
debconf-set-selections <<EOF
postfix postfix/main_mailer_type select Internet Site
postfix postfix/mailname string $MAIL_HOSTNAME
EOF
DEBIAN_FRONTEND=noninteractive apt-get install -y -q postfix opendkim opendkim-tools >/dev/null
# Claim the freshly installed config straight away, so a re-run after a later
# failure isn't mistaken for existing mail configuration.
for f in /etc/postfix/main.cf /etc/opendkim.conf; do
    grep -qF "$MARKER" "$f" || sed -i "1i $MARKER" "$f"
done
echo "$MAIL_HOSTNAME" > /etc/mailname

echo "==> Configuring OpenDKIM"
mkdir -p "$KEY_DIR"
if [[ ! -f "$KEY_DIR/$DKIM_SELECTOR.private" ]]; then
    opendkim-genkey -b "$DKIM_BITS" -h sha256 -d "$DOMAIN" -s "$DKIM_SELECTOR" -D "$KEY_DIR"
else
    echo "    Keeping existing DKIM key $KEY_DIR/$DKIM_SELECTOR.private"
fi
chown -R opendkim:opendkim /etc/opendkim/keys
chmod 700 "$KEY_DIR"
chmod 600 "$KEY_DIR/$DKIM_SELECTOR.private"

printf '%s\n' 127.0.0.1 ::1 $DOCKER_SUBNETS > /etc/opendkim/TrustedHosts

cat > /etc/opendkim.conf <<EOF
$MARKER
Syslog                  yes
SyslogSuccess           yes
UMask                   007
UserID                  opendkim
PidFile                 /run/opendkim/opendkim.pid
Socket                  inet:8891@127.0.0.1
Mode                    s
Canonicalization        relaxed/simple
SignatureAlgorithm      rsa-sha256
OversignHeaders         From
Domain                  $DOMAIN
Selector                $DKIM_SELECTOR
KeyFile                 $KEY_DIR/$DKIM_SELECTOR.private
# Mail from these addresses is signed; OpenDKIM only trusts localhost otherwise,
# and Dojo's mail arrives from a Docker network.
InternalHosts           /etc/opendkim/TrustedHosts
TrustAnchorFile         /usr/share/dns/root.key
EOF
# Older Debian/Ubuntu units pass the socket from /etc/default/opendkim on the
# command line, which overrides the one in opendkim.conf.
if [[ -f /etc/default/opendkim ]] && grep -q '^SOCKET=' /etc/default/opendkim; then
    sed -i 's|^SOCKET=.*|SOCKET=inet:8891@127.0.0.1|' /etc/default/opendkim
    if [[ -x /lib/opendkim/opendkim.service.generate ]]; then
        /lib/opendkim/opendkim.service.generate
        systemctl daemon-reload
    fi
fi

echo "==> Configuring Postfix (send-only)"
postconf -e \
    "myhostname = $MAIL_HOSTNAME" \
    "myorigin = \$myhostname" \
    "mydestination = \$myhostname, localhost.localdomain, localhost" \
    "relayhost =" \
    "inet_interfaces = all" \
    "inet_protocols = ipv4" \
    "mynetworks = 127.0.0.0/8 $DOCKER_SUBNETS" \
    "smtpd_client_restrictions = permit_mynetworks, reject" \
    "smtpd_relay_restrictions = permit_mynetworks, reject" \
    "disable_vrfy_command = yes" \
    "smtpd_banner = \$myhostname ESMTP" \
    "smtp_tls_security_level = may" \
    "smtp_tls_loglevel = 1" \
    "milter_default_action = accept" \
    "milter_protocol = 6" \
    "smtpd_milters = inet:127.0.0.1:8891" \
    "non_smtpd_milters = inet:127.0.0.1:8891"
# inet_protocols = ipv4: the PTR record is only set for the IPv4 address, and
# Gmail/Outlook reject mail from IPv6 addresses without reverse DNS.
# myorigin/mydestination = $myhostname: the server's own mail (cron, root)
# stays in /var/mail instead of being sent out to the domain.

systemctl enable --now opendkim >/dev/null 2>&1
systemctl restart opendkim
for _ in $(seq 1 10); do
    ss -ltn | grep -q '127.0.0.1:8891' && break
    sleep 1
done
if ! ss -ltn | grep -q '127.0.0.1:8891'; then
    echo "OpenDKIM isn't listening on 127.0.0.1:8891 — check: journalctl -u opendkim" >&2
    exit 1
fi
systemctl enable --now postfix >/dev/null 2>&1
systemctl restart postfix

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    echo "==> Allowing Docker containers to reach Postfix through ufw"
    for subnet in $DOCKER_SUBNETS; do
        ufw allow proto tcp from "$subnet" to any port 25 comment 'Docker -> Postfix' >/dev/null
    done
    if ufw status | grep -E '^25(/tcp)?\s+ALLOW\s+Anywhere' >/dev/null; then
        echo "    WARNING: ufw allows port 25 from anywhere. Postfix rejects those"
        echo "    clients anyway, but this relay doesn't need inbound mail."
    fi
fi

DKIM_RECORD="$(grep -o '"[^"]*"' "$KEY_DIR/$DKIM_SELECTOR.txt" | tr -d '"\n')"

echo
if timeout 5 bash -c '</dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
    echo "Outbound port 25: OPEN — mail will be delivered."
else
    echo "Outbound port 25: BLOCKED — mail will queue on this server (see 'mailq')"
    echo "until your provider unblocks it, then deliver automatically."
fi

if points_here "$MAIL_HOSTNAME"; then
    A_RECORD="(already points at this server — nothing to add)"
else
    A_RECORD="$PUBLIC_IP"
fi

cat <<EOF

================================================================
 DNS records for sending as noreply@$DOMAIN
================================================================
 Names are shown in full. Most DNS panels want only the part before
 your main domain — e.g. "_dmarc.dojo" for _dmarc.dojo.yourclub.co.uk.

 A     $MAIL_HOSTNAME
       $A_RECORD
 PTR   $PUBLIC_IP -> $MAIL_HOSTNAME
       (set in your server provider's panel, not your DNS host)
 TXT   $DOMAIN
       v=spf1 ip4:$PUBLIC_IP ~all
       If this name already has a "v=spf1" record, add "ip4:$PUBLIC_IP"
       to it instead — a name must only have one SPF record.
 TXT   $DKIM_SELECTOR._domainkey.$DOMAIN
       $DKIM_RECORD
 TXT   _dmarc.$DOMAIN
       v=DMARC1; p=none; adkim=s; aspf=s
       Once mail has delivered cleanly for a few weeks, tighten
       p=none to p=quarantine.

The server's own hostname should match too:
   hostnamectl set-hostname $MAIL_HOSTNAME

Once DNS has propagated, check the DKIM record with:
   opendkim-testkey -d $DOMAIN -s $DKIM_SELECTOR -vvv

Then in Dojo's .env:
   EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
   EMAIL_HOST=host.docker.internal
   EMAIL_PORT=25
   EMAIL_USE_TLS=False
   EMAIL_USE_SSL=False
   EMAIL_HOST_USER=
   EMAIL_HOST_PASSWORD=
   DEFAULT_FROM_EMAIL=noreply@$DOMAIN
and restart the web container: docker compose up -d web
================================================================
EOF
