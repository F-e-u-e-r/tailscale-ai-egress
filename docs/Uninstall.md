# Uninstall and Cleanup

Complete removal, in a safe order (roughly the reverse of setup). Do the policy
and Admin Console steps too if you want no trace left in your tailnet, not just
on the VPS.

If you only want to pause the connector, jump to
[Temporarily take the connector offline](#temporarily-take-the-connector-offline).

## 1. Disable exit-node fallback (if you enabled it)

On the connector host:

```bash
./disable-exit-node.sh
```

This stops advertising exit-node capability while leaving the App Connector
setup in place. Verify the connector tag is still present in the output.

## 2. Roll back tailnet policy changes (Advanced Mode only)

If you used Advanced Policy Automation to apply policy, restore the pre-apply
policy before removing anything else.

Plan-based restore (0.4+ plan bundles):

```bash
python3 scripts/policy_tool.py list-plans
python3 scripts/policy_tool.py restore-plan generated/policy-plans/plan.<plan-id>
```

`restore-plan` rewrites the entire policy from the captured snapshot — any
policy edit made after that snapshot is lost.

Legacy backup restore (from the former direct `apply` command):

```bash
./rollback.sh --list
./rollback.sh generated/tailnet-policy.backup.<timestamp>.hujson
```

Without API credentials, `rollback.sh` prints the backup path and the Admin
Console URL so you can paste it back manually. If you only ever used Manual
Guided Mode, there is nothing to roll back — your policy is whatever you pasted.

## 3. Disconnect the VPS from your tailnet

```bash
# Log the node out of the tailnet (clears local node state).
sudo tailscale logout

# Stop the daemon.
sudo systemctl stop tailscaled      # systemd
sudo rc-service tailscale stop      # Alpine/OpenRC
```

To fully remove the Tailscale package as well, use your package manager (for
example `sudo apt-get remove --purge tailscale`). Skip this if the host runs
other Tailscale workloads.

## 4. Remove host forwarding configuration

`bootstrap.sh` (and `enable-exit-node.sh`) write a sysctl drop-in. Remove it and
turn forwarding back off:

```bash
sudo rm -f /etc/sysctl.d/99-tailscale-ai-egress.conf
sudo sysctl -w net.ipv4.ip_forward=0
sudo sysctl -w net.ipv6.conf.all.forwarding=0
```

Do **not** disable forwarding if this host still routes traffic for another
service.

## 5. Admin Console cleanup

In the [Tailscale Admin Console](https://login.tailscale.com/admin):

- **Machines:** delete the connector device record (for example
  `ai-egress-jp-01`) if you no longer want it listed.
- **Keys:** revoke the node auth key you used to bootstrap, and any OAuth client
  or API key you created for Advanced Mode.
- **Access controls (policy):** if no remaining connector uses the tag, remove
  the connector's entries that this project added:
  - the `nodeAttrs` app-connector block (`tailscale.com/app-connectors`),
  - the `autoApprovers.routes` entries for `0.0.0.0/0` and `::/0` tied to the
    connector tag,
  - the broad `grants` (`autogroup:member` → `autogroup:internet`) if you added
    it only for this connector,
  - the `tagOwners` entry for `tag:ai-egress-*`.

  Keep any policy entries shared with other infrastructure.

## 6. Remove local files

The repo's `generated/` directory can contain your tailnet policy, plan bundles,
backups, and `connector-identity.env` metadata with the connector region, tag,
and hostname. Treat it as sensitive.

```bash
rm -rf generated/policy-plans generated/*.hujson generated/*.snippet.json generated/connector-identity.env
# or remove the whole checkout once you have restored/recorded what you need
```

## Temporarily take the connector offline

If you just want to pause without uninstalling:

```bash
sudo systemctl stop tailscaled      # systemd
sudo rc-service tailscale stop      # Alpine/OpenRC
```

Start it again with `sudo systemctl start tailscaled` (or
`sudo rc-service tailscale start`). The node rejoins with its existing settings;
no re-bootstrap is needed.
