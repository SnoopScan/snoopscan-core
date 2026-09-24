# Crawler egress filter — plan for review

**Tested temporarily and removed; not currently loaded.** Each mode has been
loaded on the host, driven, and deleted again — see "What was tested" below.
Nothing is in force now, and nothing persists across a reboot.

## What was measured on the host before writing it

| | |
|---|---|
| Existing firewall | **ufw active**, iptables-nft backend, owns `table ip filter`. `nftables.service` inactive. fail2ban present. |
| Containers | **None.** No Docker, no netns. |
| Crawler identity | engine runs as **uid 999 (`snoopscan`)**; chromium-based browsers as **uid 994 (`snoopbrowser`)** — see "Browser isolation" |
| Loopback services it must keep | postgres `5432`, redis `6379`, searxng `8888` — all on `127.0.0.1` |
| Loopback service it must NOT have | mysql `3306` (the Laravel app's database) |
| Resolvers | `185.12.64.1`, `185.12.64.2`, `2a01:4ff:ff00::add:2` — all public |

Two consequences worth stating, because both are places this design is
commonly got wrong:

- **No containers means no `forward` chain and no host-gateway case.** The
  rule people miss is that a container reaching the host's own veth address
  is delivered locally and never enters `forward`, so a forward-only denylist
  does not stop it. It does not apply here, and if anything is containerised
  later this ruleset must be revisited rather than assumed to cover it.
- **DNS is not inside a blocked range on this host**, so the resolver pin is
  defensive rather than load-bearing. If the host ever moves to a stub
  resolver on `127.0.0.53`, DNS breaks the moment this loads unless the pin is
  updated — and the tempting fix, allowing the whole `127.0.0.0/8`, is an SSRF
  hole with extra steps.

## Two paths, two controls — and this only covers one

This is the point the review made, and it is correct.

```
DIRECT path          engine --> resolve (ssrf.py) --> connect --> TARGET
                                                        ^
                                              nftables sees the TARGET

PROXIED path         engine --> CONNECT target --> RESIDENTIAL PROXY --> TARGET
                                     ^                    ^
                        nftables sees only the PROXY   the proxy resolves
                                                       and connects; we have
                                                       no control here
```

**Where the destination is decided, per path:**

| | Who resolves the name | What the local packet is addressed to | What nftables can enforce |
|---|---|---|---|
| Direct | us, in `ssrf.py` | the target | the target — fully |
| Proxied | **the proxy vendor** | the proxy's public IP | nothing about the target |
| Browser, proxied | the proxy vendor | the proxy's public IP | nothing about the target |
| Browser, implicit bypass | Chromium | the target | the target — and this is the case that mattered |

That last row is why this is worth deploying even though production is mostly
proxied. Chromium bypasses the proxy for loopback and `169.254/16` regardless
of what proxy you set; those requests are addressed locally, so they are
exactly what the kernel filter catches.

**Is our internal network reachable on the proxied path? No.** The proxy sits
outside; it has no route to this host's loopback or RFC1918 space. A redirect
to `169.254.169.254` through a residential exit reaches *the vendor's* metadata
service, not ours. That is still worth refusing — we validate it pre-flight in
`ssrf.py` and refuse it, and being the vector for someone else's compromise is
not acceptable — but it is not a route into us.

**DNS rebinding, honestly.** Rebinding needs the resolution we act on to
differ from the one we validated.

We **always** resolve and validate the target first — `ScrapeService.scrape()`
calls `resolve_and_validate()` at the top of every request, before any proxy
decision, so this happens on the proxied path too. (An earlier draft of this
document said we never resolve on the proxied path. That was wrong.)

What differs is what happens *next*:

- **Direct.** We validate our resolution, then the fetcher connects by
  hostname and the OS resolves a **second** time. The window between those two
  is real and **still open**.
- **Proxied.** Our validation still runs and still refuses an obviously
  internal target. But the connection is made by the **vendor**, who resolves
  the name independently and may get a different answer than we did. We cannot
  pin what we do not dial.

Closing the direct-path window needs connect-time IP pinning — a custom
httpcore network backend that dials the validated literal and default-denies
an unpinned authority. **That is not built.** The kernel filter narrows the
consequence on the direct path (a rebind onto a private address is refused at
the socket) without removing the race, and it cannot help on the proxied path
at all. This is the largest remaining gap.

## What this does not cover

- **Anything proxied**, per above.
- **UDP to PUBLIC destinations** — WebRTC and QUIC. The reject rules carry no
  protocol restriction, so UDP *to the blocked ranges* is refused like
  anything else; what is not constrained is direct UDP to a public address.
  The browser flags disable non-proxied UDP and QUIC, but that is Chromium's
  parsing, not the kernel's.
- **The browser request guard is not a boundary.** It races Chromium's own
  resolution, and Playwright's `route()` is documented as firing only for the
  first URL of a redirect chain. It is policy and logging.
- **WebSockets** — our route guard does not see them. Playwright does have a
  dedicated `WebSocketRoute` API; we do not use it, and it would be worth
  adding for observability. Note it works by replacing the page's own
  `WebSocket` global, so it does not reach workers — a boundary implemented
  inside the environment being contained is not a boundary. Through the proxy,
  WebSocket traffic does appear as a `CONNECT`, so a validating forward proxy
  would see it where interception cannot.

## Browser isolation

The browsers used to share the engine's uid, so the postgres/redis/searxng
exceptions applied to them too: a page that persuaded Chromium to open
`127.0.0.1:5432` was allowed by the very rule that lets the engine reach its
own database. The kernel could not tell them apart, because they were the same
identity.

They now run as **`snoopbrowser` (uid 994)**, which has no loopback exception
of any kind. `install-browser-user.sh` sets it up and `--remove` undoes it
completely.

How it works, since none of it is obvious:

- Playwright has no "run as another user" option, so `executable_path` points
  at `snoopscan-browser`, a wrapper that `sudo`s to the browser user. The
  sudoers rule permits exactly one binary and exactly one target user.
- **The profile directory.** Playwright creates it as the *calling* user with
  mode 0700 and the browser then cannot write it. The wrapper hands it to a
  shared group first — it runs as the caller, before `sudo`, which is the only
  moment that is possible.
- **`sudo -C 20`.** Playwright gives the browser its remote-debugging pipe on
  fds 3 and 4, and sudo closes everything above 2 by default; without this the
  browser exits 13 with *"Remote debugging pipe file descriptors are not
  open"*. It needs `closefrom_override` in the sudoers drop-in.
- **`sudo -H`.** crashpad wants a writable HOME, so the browser user has one.

Measured with the filter enforcing:

| | postgres | redis | searxng | public web |
|---|---|---|---|---|
| `snoopscan` (engine) | reachable | reachable | reachable | 200 |
| `snoopbrowser` | **refused** | **refused** | **refused** | 200 |

And end to end through the API: a browser-tier scrape of a JavaScript-rendered
page returned 245 words with its actions performed, while five browser
processes ran as `snoopbrowser`.

**`ENGINE_BROWSER_EXECUTABLE` is empty by default.** A self-hosted deployment
with no such user, and no egress filter to scope, launches the browser
normally as before.

**Not yet covered: the Camoufox rung.** It is Firefox, launched by its own
library rather than through `executable_path`, so it still runs as the engine
user. The chromium-based rungs — browser and stealth — are the ones this
covers.

## Persistence

**None.** The table lives in the kernel and a reboot clears it; the host
returns to exactly its current behaviour. `apply.sh keep` cancels the revert
timer — it does not install anything at boot.

That is deliberate for a first deployment: if it turns out to break something
at 3am, a reboot fixes it. Making it survive a reboot is a separate decision
(a systemd unit running `apply.sh enforce` with no revert, ordered before the
engine), and should only be taken after it has run enforcing for a while.

## What was tested, on the host, before asking you to review it

Not a syntax check — the modes were actually loaded and driven.

| Test | Result |
|---|---|
| Load the same file twice | 10 rules both times. `nft -f` appends to an existing table, so the file opens `table`/`delete table`/`table` — without that, a second load doubled every rule. |
| `enforce` over the top of `observe` | mode switches cleanly; no stale reject rules left behind |
| `observe` over the top of `enforce` | refuses nothing again, and says so truthfully |
| **API replies under enforcement** | health **200** over loopback and **200** through nginx |
| **The same rules with the conntrack line removed** | health **000** locally and **000** through nginx — the whole product down |
| All 15 verifier checks under enforcement | pass, each block proved by a counter *before* the filter increasing and the one *after* it staying put — e.g. "2 packet(s) reached the filter, 0 got past" |
| Automatic rollback | fired on its own after the window; table gone, health 200 |
| `enforce 2` then `observe` | the stale revert timer is cancelled; the observe table **survived** past the old deadline. Before the fix it would have been deleted mid-observation. |
| The verifier against an UNPROTECTED host | 7 failures, correctly — it reports "the probe CONNECTED" where something listens and "packets reached the wire" where nothing does. It cannot pass when nothing is being blocked. |
| A crawler connection to a forbidden address, open BEFORE enforcement | its outbound packets are refused (counter +25). `ct direction reply` excuses only answers to connections someone else opened. |
| ufw | untouched throughout — `ip filter` and `ip6 filter` only |

And the failure paths, which the live runs above do not exercise:

| Failure path | Result |
|---|---|
| `observe` whose rules will not parse, while `enforce` is armed | exit 1, **enforce rules still loaded and their timer still armed**. The replacement is generated, checked and loaded before the old rollback is retired, so a failure leaves the protected state alone. |
| `enforce` that cannot arm its rollback | exit 1, **and the previous table is removed**. Enforcement must never outlive its rollback, so failing to arm means not enforcing at all. Health stayed 200. |
| Verifier whose counter read fails | every block test FAILS with "evidence missing, not a pass". A broken lookup used to read as zero, and zero used to read as blocked. |
| Verifier whose probe emits no packet | every block test FAILS with "the probe emitted NOTHING at this filter". |

One nuance on that last row, because a naive test reads the wrong thing: data
that already arrived sits in the socket's receive buffer and can still be
read after enforcement starts. What the connection cannot do is send anything
further — every outbound packet on it is refused. A test that writes a byte
and calls `write()` succeeding "still usable" is measuring the socket buffer,
not the wire; the counter is the evidence.

The conntrack row is the one that matters. That is the fault the review caught,
reproduced and then fixed, and no `nft -c` would ever have found it.

## Deployment plan

1. **Review these three files.** They have been exercised on the host and
   removed again; nothing is loaded now.
2. `sudo ./apply.sh observe` — loads with every reject replaced by a counter
   and a rate-limited log line carrying the destination. Refuses nothing.
3. Leave it for a day of real traffic. `./apply.sh counters` shows the totals
   AND the destinations — host, protocol and port — because two aggregate
   counters cannot tell you what would have broken. **Expect zero.** Anything non-zero is either a real find
   or a rule that would have caused an outage — resolve it before step 4.
4. `sudo ./apply.sh enforce 20` — enforces, and arms a `systemd-run` timer to
   delete the table in 20 minutes. The revert is armed *before* the rules
   load, so it survives the ssh session dropping or the operator walking away.
5. `sudo ./verify.sh` inside that window — checks both directions: metadata,
   loopback, RFC1918 and CGNAT refused; postgres, redis, searxng, DNS, the
   public web and our own API still working; other users unaffected; engine
   health 200. Exit status is the failure count.
6. `sudo ./apply.sh keep` only if verify passed. Otherwise do nothing and let
   the timer revert it.

**It never flushes the ruleset.** It creates and deletes exactly one table,
`inet snoopscan_egress`. ufw's `ip filter` is not read or written. nftables
evaluates every table, so a `reject` here stands whatever ufw accepted, and
removing this table returns the host precisely to its current behaviour.

`reject` rather than `drop` throughout: a refused connection fails in
milliseconds and surfaces as an error, where a dropped one hangs until the
request times out and reads like a slow site.
