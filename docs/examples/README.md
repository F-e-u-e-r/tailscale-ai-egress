# Automation Examples

Ready-to-adapt units for running the v1.1.0 failover controller and connector
monitor automatically. Edit the paths, the user, and `generated/failover.env`
first; see [Configuration](../Configuration.md) and [Failover](../Failover.md).

- `systemd/failover-exit-node.service` — Linux: exit-node failover controller (`--watch --apply`).
- `systemd/monitor-connectors.service` — Linux: read-only connector HA monitor (`--watch`).
- `launchd/com.tailscale-ai-egress.failover-exit-node.plist` — macOS: exit-node failover controller.
- `cron/failover.cron` — cron lines for `--once` runs.

These are not installed automatically. The controller only changes your exit
node when it runs with `--apply`; the monitor never changes anything.
