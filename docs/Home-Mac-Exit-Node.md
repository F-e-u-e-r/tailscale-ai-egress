# Home Mac Exit Node Fallback

Use this only when you intentionally want a residential/home fallback exit node. It is separate from the Linux App Connector scripts in this repo.

## What This Is

A home Mac exit node routes all selected-client internet traffic through your home connection. That can be useful when a residential ASN is required, but it depends on your ISP upload speed, home router stability, and Mac power settings.

Do not run the Linux helper scripts on the Mac. `enable-exit-node.sh`, `disable-exit-node.sh`, and `restore-connector.sh` are for Linux connector hosts.

## Tailscale App Store Vs Standalone

The Mac App Store and standalone Tailscale builds expose settings differently. Use the Tailscale menu bar app when possible:

- Enable the setting that lets the Mac run as an exit node.
- Approve the exit node in the Tailscale Admin Console if your tailnet requires it.
- On the client device, select the Mac as the exit node.

If you use the CLI-capable standalone build, Tailscale's documented flow is:

```bash
sudo tailscale set --advertise-exit-node
```

Older client versions or local installs may require `tailscale up` flags instead. Follow Tailscale's current Mac exit-node docs for your installed build.

## macOS Forwarding

macOS forwarding is not the same as Linux forwarding. If Tailscale or your setup requires manual forwarding, the macOS IPv4 knob is:

```bash
sudo sysctl -w net.inet.ip.forwarding=1
```

Do not copy Linux-only commands such as `net.ipv4.ip_forward` or `/etc/sysctl.d/99-tailscale-ai-egress.conf` onto macOS.

## Reliability Checklist

- Disable sleep while you rely on the Mac as an exit node.
- Keep the Mac on AC power.
- Confirm your ISP upload bandwidth is enough for the client traffic.
- Expect home router restarts, Wi-Fi roaming, or sleep to disconnect the exit node.
- Remember that all selected-client internet traffic uses the home connection while the exit node is active.

For everyday AI-only routing, the Linux App Connector remains the recommended mode.
