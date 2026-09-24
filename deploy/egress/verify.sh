#!/usr/bin/env bash
#
# Does the filter block what it should, allow what it must, and leave the
# API's own replies alone?
#
#   sudo ./verify.sh
#
# WHY IT DOES NOT JUDGE BY "did curl fail". An earlier version took curl's 000
# as proof of blocking, and 000 is also what you get from a connection that
# timed out, a port with nothing listening, and a server that answered with no
# HTTP at all. Sending HTTP at MySQL and calling the failure "isolation"
# proves nothing whatsoever.
#
# AND WHY IT DOES NOT JUDGE BY THE TABLE'S OWN COUNTERS EITHER. The version
# after that summed every reject counter and asserted the total moved, which
# a concurrent crawler request being refused satisfies just as well as the
# probe being refused — the test passed while the probe itself connected.
#
# AND WHY A ZERO IS NOT EVIDENCE ON ITS OWN. The version after THAT asserted
# a later counter had stayed at zero, and treated a failed counter read as a
# zero — so a probe that never emitted a packet passed, and so did a counter
# lookup that broke. A zero only says no matching packet arrived; it cannot
# tell "this filter rejected it" from "ufw dropped it first" or "the probe
# never sent anything".
#
# So each block test needs THREE things, and no two of them:
#   1. the connection must fail;
#   2. a counter BEFORE the filter must INCREASE — positive proof the probe
#      really emitted a packet and that it reached our filter rather than
#      being stopped by something earlier;
#   3. a counter AFTER the filter must NOT increase — proof it got no further.
#
# Both counters live in `snoopscan_verify`, one chain either side of the
# filter under test (priority +5 and +20 against its +10). `reject` ends
# evaluation, so a packet reaching the later chain was not rejected. Every
# read must parse as a number; a read that fails is a FAILED test, never a
# zero.
#
# Exit status is the number of failures, so it can gate an apply.
set -uo pipefail
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export NO_PROXY='*' no_proxy='*'

TABLE="inet snoopscan_egress"
PROBE="inet snoopscan_verify"
UID_CRAWLER=999
FAILURES=0
pass() { printf "  \033[32mPASS\033[0m  %s\n" "$1"; }
fail() { printf "  \033[31mFAIL\033[0m  %s\n" "$1"; FAILURES=$((FAILURES + 1)); }
info() { printf "  ....  %s\n" "$1"; }

as_crawler() { setpriv --reuid="$UID_CRAWLER" --regid="$UID_CRAWLER" --clear-groups "$@"; }

# Destinations each block test uses. A counter rule is built per entry so the
# evidence is attributable; comments in the loop explain the priority.
PROBES=(
    "169.254.169.254 80   cloud metadata"
    "127.0.0.1       3306 the other app's mysql"
    "10.0.0.1        80   RFC1918 10/8"
    "192.168.1.1     80   RFC1918 192.168/16"
    "100.64.0.1      80   carrier-grade NAT"
)
PROBE6=("::1 3306 IPv6 loopback")

build_probe_table() {
    {
        echo "table $PROBE"
        echo "delete table $PROBE"
        echo "table $PROBE {"
        # BEFORE the filter under test (+10): everything the probe emits
        # passes here first, so an increase is proof the packet was really
        # sent AND that nothing earlier (ufw, another table) stopped it.
        echo "  chain before {"
        echo "    type filter hook output priority filter + 5; policy accept;"
        echo "    meta skuid != $UID_CRAWLER accept"
        for entry in "${PROBES[@]}"; do
            read -r host port _ <<<"$entry"
            echo "    ip daddr $host tcp dport $port counter comment \"pre_${host}_${port}\""
        done
        for entry in "${PROBE6[@]}"; do
            read -r host port _ <<<"$entry"
            echo "    ip6 daddr $host tcp dport $port counter comment \"pre_${host}_${port}\""
        done
        echo "  }"
        # AFTER it (+20): reject ends evaluation, so anything arriving here
        # was NOT rejected by the filter.
        echo "  chain after {"
        echo "    type filter hook output priority filter + 20; policy accept;"
        echo "    meta skuid != $UID_CRAWLER accept"
        for entry in "${PROBES[@]}"; do
            read -r host port _ <<<"$entry"
            echo "    ip daddr $host tcp dport $port counter comment \"post_${host}_${port}\""
        done
        for entry in "${PROBE6[@]}"; do
            read -r host port _ <<<"$entry"
            echo "    ip6 daddr $host tcp dport $port counter comment \"post_${host}_${port}\""
        done
        echo "  }"
        echo "}"
    } | nft -f -
}

# Prints the counter, or nothing at all when the rule is missing or the value
# will not parse. The caller MUST treat empty as a failure, never as zero.
probe_counter() {
    local value
    value=$(nft list table $PROBE 2>/dev/null \
        | grep -F "\"$1\"" | grep -oE 'counter packets [0-9]+' | head -1 | awk '{print $3}')
    case "$value" in
        ''|*[!0-9]*) return 1 ;;
        *) printf '%s' "$value" ;;
    esac
}

# BLOCKED = the connection failed, the packet demonstrably reached our filter,
# and it demonstrably got no further. A missing reading fails the test.
probe_blocked() {
    local host="$1" port="$2" label="$3" tag="$4"
    local pre_before pre_after post_before post_after connected=0

    if ! pre_before=$(probe_counter "pre_$tag") || ! post_before=$(probe_counter "post_$tag"); then
        fail "$label — could not READ the probe counters (evidence missing, not a pass)"
        return
    fi

    if as_crawler timeout 4 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then
        connected=1
    fi

    if ! pre_after=$(probe_counter "pre_$tag") || ! post_after=$(probe_counter "post_$tag"); then
        fail "$label — probe counters vanished mid-test (evidence missing)"
        return
    fi

    local emitted=$((pre_after - pre_before))
    local escaped=$((post_after - post_before))

    if [ "$connected" -eq 1 ]; then
        fail "$label — the probe CONNECTED"
    elif [ "$emitted" -le 0 ]; then
        # The whole point of the pre-counter. Without it, a probe that never
        # sent a packet — a socket error, or something earlier in the chain
        # dropping it — looked exactly like a successful block.
        fail "$label — the probe emitted NOTHING at this filter; nothing was proved"
    elif [ "$escaped" -gt 0 ]; then
        fail "$label — $escaped packet(s) got past the filter"
    else
        pass "$label — $emitted packet(s) reached the filter, 0 got past"
    fi
}

probe_allowed() {
    local label="$1" host="$2" port="$3"
    if as_crawler timeout 5 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then
        pass "$label"
    else
        fail "$label — UNREACHABLE, the engine depends on this"
    fi
}

cleanup() { nft delete table $PROBE 2>/dev/null || true; }
trap cleanup EXIT

echo "0. The filter must actually be enforcing"
if ! nft list table $TABLE >/dev/null 2>&1; then
    echo "  table not loaded — nothing to verify"; exit 1
fi
if nft list table $TABLE | grep -q "reject with"; then
    pass "enforce mode is active"
else
    fail "observe mode is loaded — block tests below cannot pass, and must not"
fi
build_probe_table || { echo "  could not build the probe table"; exit 1; }

echo
echo "1. Infrastructure must be refused, and provably THIS filter"
for entry in "${PROBES[@]}"; do
    read -r host port label <<<"$entry"
    probe_blocked "$host" "$port" "$label $host:$port" "${host}_${port}"
done
for entry in "${PROBE6[@]}"; do
    read -r host port label <<<"$entry"
    probe_blocked "$host" "$port" "$label [$host]:$port" "${host}_${port}"
done

echo
echo "2. The services the engine needs must still work"
probe_allowed "postgres 127.0.0.1:5432" 127.0.0.1 5432
probe_allowed "redis 127.0.0.1:6379" 127.0.0.1 6379
probe_allowed "searxng 127.0.0.1:8888" 127.0.0.1 8888

echo
echo "3. DNS and the public web must still work"
if as_crawler timeout 6 getent hosts example.com >/dev/null 2>&1; then
    pass "DNS resolves"
else
    fail "DNS BROKEN — check the resolver pins against /etc/resolv.conf"
fi
code=$(as_crawler timeout 10 curl -s -o /dev/null -w '%{http_code}' --max-time 8 https://example.com/ 2>/dev/null)
[ "$code" = "200" ] && pass "public https (example.com $code)" || fail "public web unreachable (got '${code:-nothing}')"

echo
echo "4. THE API'S OWN REPLIES — the rule this ruleset previously broke"
# The engine listens on 127.0.0.1:8099 as uid 999. Its answers go to the
# client's ephemeral port, inside 127.0.0.0/8 and not one of the three allowed
# ports. Without the conntrack rule this fails as the whole product.
body=$(timeout 8 curl -s --max-time 6 -w '\n%{http_code}' http://127.0.0.1:8099/health 2>/dev/null)
code="${body##*$'\n'}"
[ "$code" = "200" ] && pass "engine health over loopback (200)" \
    || fail "engine health = '${code:-no answer}' — the API cannot reply to local clients"

if systemctl is-active nginx >/dev/null 2>&1; then
    # 200 ONLY. Accepting "any status but 000" passed on 502 and 503, so a
    # broken upstream read as success — the exact thing being tested for.
    code=$(timeout 10 curl -sk -o /dev/null -w '%{http_code}' --max-time 8 https://api.snoopscan.com/health 2>/dev/null)
    if [ "$code" = "200" ]; then
        pass "through nginx (200)"
    else
        fail "nginx -> API path returned '${code:-nothing}' (502/503 mean the upstream is refused)"
    fi
    info "note: dialled from the server itself, so this is not an outside probe"
else
    info "nginx not active; skipped the proxied path"
fi

echo
echo "5. Everything else on the box is untouched"
if timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/3306' 2>/dev/null; then
    pass "root still reaches mysql (the rules are uid-scoped)"
else
    fail "mysql unreachable as root — the uid scope is wrong"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "All checks passed. Safe to keep:  sudo $(dirname "$0")/apply.sh keep"
else
    echo "$FAILURES failed. Do NOT keep — let the timer revert, or: apply.sh remove"
fi
exit "$FAILURES"
