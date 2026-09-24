#!/usr/bin/env bash
#
# Apply the crawler egress filter, with a rollback that happens whether or not
# anyone remembers to run it.
#
#   ./apply.sh observe          load in log-and-count mode, refuse nothing
#   ./apply.sh counters         what has been refused (or would have been)
#   ./apply.sh status           which mode is loaded, if any
#   ./apply.sh enforce [MINS]   enforce, auto-reverting after MINS (default 20)
#   ./apply.sh keep             cancel the pending revert; rules stay until reboot
#   ./apply.sh remove           delete the table now
#
# The order that matters: run `observe` for a day, read `counters` against real
# traffic, and only then `enforce`. A rule that loads is not a rule that is
# safe — the two entries that most often break legitimate traffic are
# 100.64.0.0/10 and the IPv6 set.
#
# NOT PERSISTENT ACROSS REBOOT. The table lives in the kernel; a reboot clears
# it and the host returns to its current behaviour. `keep` cancels the revert
# timer, it does not install anything at boot. Making it survive a reboot is a
# separate, deliberate step — see README.md, "Persistence".
#
# Never flushes the ruleset. It only ever creates or deletes ONE table,
# `inet snoopscan_egress`; ufw's `ip filter` is untouched.
set -euo pipefail

TABLE="inet snoopscan_egress"
HERE="$(cd "$(dirname "$0")" && pwd)"
RULES="$HERE/snoopscan-egress.nft"
OBSERVE="/run/snoopscan-egress-observe.nft"
UNIT="snoopscan-egress-revert"
LOGTAG="snoopscan-egress-refused"

need_root() { [ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }; }

armed() { systemctl is-active "${UNIT}.timer" >/dev/null 2>&1; }

# Cancel any pending revert. Called on every mode change — an `enforce 20`
# followed by `observe` used to leave the timer armed, and twenty minutes
# later it deleted the observe table and silently ended the observation.
#
# ORDER MATTERS, and it is the opposite of the obvious one: this is called
# only AFTER the replacement is in place. Disarming first means any later
# failure — the file will not generate, will not parse, will not load —
# leaves the OLD enforcing rules in force with nothing to take them away.
disarm() {
    if armed; then
        echo "  (cancelled the revert timer left over from a previous enforce)"
    fi
    systemctl stop "${UNIT}.timer" 2>/dev/null || true
    systemctl reset-failed "$UNIT" 2>/dev/null || true
}

# Arm a revert, replacing any existing one. Returns non-zero if it could not.
arm() {
    local mins="$1"
    systemctl stop "${UNIT}.timer" 2>/dev/null || true
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    systemd-run --on-active="${mins}min" --unit="$UNIT" \
        /usr/sbin/nft delete table $TABLE >/dev/null 2>&1 || return 1
    armed || return 1
}
loaded()    { nft list table $TABLE >/dev/null 2>&1; }
enforcing() { loaded && nft list table $TABLE | grep -q "reject with"; }

# Observe mode is the same file with every reject turned into a log line. The
# log carries the DESTINATION, because two aggregate counters cannot tell you
# what would have broken — which host, which port, which process.
make_observe() {
    sed -E "s#counter reject with icmp(v6)? type admin-prohibited#counter limit rate 20/second log prefix \"$LOGTAG \" level info#" \
        "$RULES" > "$OBSERVE"
}

case "${1:-}" in
  observe)
    need_root
    # Generate, check and LOAD first; only then retire the old rollback. If
    # any of these fails, the previous rules and their timer are both still
    # in place — which is the safe direction to fail in.
    make_observe        || { echo "could not generate the observe rules" >&2; exit 1; }
    nft -c -f "$OBSERVE" || { echo "observe rules did not parse; nothing changed" >&2; exit 1; }
    nft -f "$OBSERVE"    || { echo "observe rules did not load; nothing changed" >&2; exit 1; }
    disarm
    echo "OBSERVE mode. Nothing is being refused; matches are logged."
    echo "Leave it for a day of real traffic, then: $0 counters"
    ;;

  enforce)
    need_root
    MINS="${2:-20}"
    nft -c -f "$RULES" || { echo "rules did not parse; nothing changed" >&2; exit 1; }

    # Armed BEFORE the rules load, so a filter that locks something out cannot
    # outlive its window even if this shell dies or the ssh session drops —
    # and CONFIRMED armed before anything is enforced, because replacing a
    # timer can fail and enforcement without a rollback is the state this
    # whole script exists to prevent.
    if ! arm "$MINS"; then
        echo "could not arm the revert timer — refusing to enforce." >&2
        if nft list table $TABLE >/dev/null 2>&1; then
            nft delete table $TABLE 2>/dev/null || true
            echo "previous rules removed too: enforcement must never outlive its rollback." >&2
        fi
        exit 1
    fi

    if ! nft -f "$RULES"; then
        echo "rules did not load. The revert timer is armed and will clean up." >&2
        exit 1
    fi

    echo "ENFORCING. Reverting automatically in ${MINS} minutes."
    echo "  verify now:  sudo $HERE/verify.sh"
    echo "  keep it:     $0 keep        (until reboot — not persistent)"
    echo "  revert now:  $0 remove"
    ;;

  counters)
    loaded || { echo "not loaded"; exit 1; }
    MODE=$(enforcing && echo "ENFORCING (these were refused)" || echo "observe (these WOULD have been refused)")
    echo "Mode: $MODE"
    echo
    echo "Totals:"
    nft -a list table $TABLE | grep -E "counter packets" | grep -vE "packets 0 bytes 0" \
        || echo "  none — nothing is hitting these ranges"
    echo
    echo "Destinations seen (last 200 log lines):"
    journalctl -k --no-pager -n 20000 2>/dev/null | grep "$LOGTAG" | tail -200 \
        | sed -E 's/.*(SRC=[^ ]+).*(DST=[^ ]+).*(PROTO=[^ ]+)( SPT=[^ ]+)?( DPT=[^ ]+)?.*/  \2 \3\5/' \
        | sort | uniq -c | sort -rn | head -25 \
        || echo "  (no log lines; enforce mode counts but does not log)"
    ;;

  status)
    if ! loaded; then echo "not loaded"; exit 0; fi
    enforcing && echo "ENFORCING" || echo "observe (log-and-count only)"
    echo "rules in table: $(nft list table $TABLE | grep -cE '^\s+(ip|ip6|ct|meta) ')"
    systemctl is-active "${UNIT}.timer" >/dev/null 2>&1 \
        && echo "revert timer: armed — $(systemctl list-timers "${UNIT}.timer" --no-pager 2>/dev/null | awk "NR==2 {print \$3, \$4}") left" \
        || echo "revert timer: none — the rules stay until removed or rebooted"
    ;;

  keep)
    need_root
    disarm
    loaded && echo "revert cancelled; rules stay until removed or REBOOTED" || echo "no table loaded"
    ;;

  remove)
    need_root
    disarm
    nft delete table $TABLE 2>/dev/null && echo "removed" || echo "not loaded"
    ;;

  *)
    sed -n '3,25p' "$0"
    exit 1
    ;;
esac
