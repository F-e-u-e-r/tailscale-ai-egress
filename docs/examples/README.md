# Automation Examples

Ready-to-adapt units for running the failover controller and connector monitor
automatically. Edit the paths, the user, and `generated/failover.env` first; see
[Configuration](../Configuration.md) and [Failover](../Failover.md).

- `systemd/failover-exit-node.service` — Linux: exit-node failover controller (`--watch --apply`).
- `systemd/monitor-connectors.service` — Linux: read-only connector HA monitor (`--watch`).
- [`openrc/failover-exit-node`](openrc/failover-exit-node) — Alpine/OpenRC: exit-node failover controller (`--watch --apply`).
- [`openrc/monitor-connectors`](openrc/monitor-connectors) — Alpine/OpenRC: read-only connector HA monitor (`--watch`).
- `launchd/com.tailscale-ai-egress.failover-exit-node.plist` — macOS: exit-node failover controller.
- `cron/failover.cron` — cron lines for `--once` runs.

These are not installed automatically. The controller only changes your exit
node when it runs with `--apply`; the monitor never changes anything.

## Alpine / OpenRC

Alpine ships BusyBox `ash`, not Bash, and the wrappers are Bash scripts, so install
the prerequisites first, then the init script (each `.confd` is an optional
`/etc/conf.d/` override). These commands write under `/etc` and `/var/log`, so run
them from a root shell — `doas -s` or `sudo -s` first (a `doas <cmd>` prefix would
not elevate the shell redirections in the rotation step below):

```sh
apk add bash python3 logrotate
# install + authenticate Tailscale, then place this repo at /opt/tailscale-ai-egress
install -m 0755 docs/examples/openrc/failover-exit-node /etc/init.d/failover-exit-node
install -m 0644 docs/examples/openrc/failover-exit-node.confd /etc/conf.d/failover-exit-node  # optional
rc-update add failover-exit-node default
rc-service failover-exit-node start
```

The monitor installs the same way with `monitor-connectors`. Both use
`supervise-daemon`, the closest OpenRC analogue to systemd's `Restart=`: the watcher
is respawned (after 10s for the controller, 30s for the monitor) on any unexpected
exit. `start_pre` and `required_files` make a missing Bash/python3/tree a loud
`rc-service start` failure instead of a silent respawn loop.

Each service appends to `/var/log/<service>.log`. Rotate those logs (they grow every
cycle) — `logrotate` was installed above, and its daily job runs from `crond`:

```sh
rc-update add crond default && rc-service crond start   # usually already enabled
# /etc/logrotate.d/tailscale-ai-egress — supervise-daemon holds the log fd open, so copytruncate:
cat > /etc/logrotate.d/tailscale-ai-egress <<'CONF'
/var/log/failover-exit-node.log /var/log/monitor-connectors.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
CONF
```
