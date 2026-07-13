# BYOC Logs Lab — Ansible

Automates the full single-node lab from the runbook: k3s, Datadog agent,
AIStor S3 (buckets + scoped key), CloudNativePG with WAL archiving, the
CloudPrem chart, Traefik ingress, and an end-to-end log-push verification.

> **Lab only.** Single node, local storage, no TLS, default root creds on
> the object store. Nothing here is production posture.

## Prerequisites (workstation / control node)

- `ansible-core` >= 2.15, `kubectl`, `helm`
- `pip install kubernetes`
- `ansible-galaxy collection install -r requirements.yml`
- SSH root access to the lab VM

## First run

1. Edit `inventory/hosts.yml` — the node's address lives **only** there.
   `node_ip` is derived from it (`hostvars[...].ansible_host`), so there is
   no second place to keep in sync. The `lab_node` group must contain exactly
   one host; `site.yml` asserts this.
2. Create the secrets file from the committed template, fill it in, encrypt it:

   ```bash
   cp group_vars/all/secrets.yml.example group_vars/all/secrets.yml
   $EDITOR group_vars/all/secrets.yml       # replace every CHANGE_ME
   ansible-vault encrypt group_vars/all/secrets.yml
   ```

   The real `secrets.yml` is gitignored; only the `.example` is committed.
   (Ansible auto-loads `.yml`/`.yaml`/`.json` from `group_vars`, so the
   `.example` extension is never read at runtime.) Edit it later without
   decrypting to disk: `ansible-vault edit group_vars/all/secrets.yml`.

   | Variable | Needed when | What it is |
   |---|---|---|
   | `datadog_api_key` | always | Datadog > Organization Settings > API Keys. Used by the agent and BYOC Logs. |
   | `s3_access_key` | always | S3 app key. **Created by the playbook**, not looked up — pick any 20-char A-Z0-9 string. CNPG (WAL archiving) and BYOC Logs (index storage) both use it. |
   | `s3_secret_key` | always | Its 40-char secret half. |
   | `aistor_license` | `s3_backend: aistor` | Free single-node eval licence from SUBNET (min.io). |
   | `aistor_root_password` | `s3_backend: aistor` | Tenant root, used only to provision buckets + the app key. Chart default `minio123`. |
   | `seaweedfs_admin_secret_key` | `s3_backend: seaweedfs` | Admin identity secret, used only to create buckets. |

   `site.yml` stops immediately if the file is missing, and refuses to run
   while any `CHANGE_ME` remains for the active backend.

3. Run:
   ```
   ansible-playbook site.yml --ask-vault-pass
   ```

The kubeconfig is fetched to `.kubeconfig/<distro>-byoc.yaml` (gitignored).

## Re-runs / partial runs

Everything is idempotent — `helm upgrade --install` semantics, `k8s` module
applies, and the AIStor AdminJob is gated behind a live bucket-access probe
(runs only when the probe fails). Tags for partial runs:

```
ansible-playbook site.yml -t distro          # just k3s
ansible-playbook site.yml -t storage         # just S3
ansible-playbook site.yml -t byoc,verify     # redeploy BYOC + smoke test
```

## Swapping variants

Two selectors in `group_vars/all/main.yml`:

| Variable     | Maps to                                                      |
|--------------|--------------------------------------------------------------|
| `k8s_distro` | `roles/<distro>` — must install k8s, wait Ready, fetch kubeconfig, and satisfy the distro contract vars (`ingress_flavor`, `default_storage_class`) |
| `s3_backend` | `roles/storage_<backend>` + `vars/storage_<backend>.yml` — must create the buckets in `s3_buckets`, provision `s3_access_key`/`s3_secret_key` scoped to them, and publish the contract |

**Storage contract** (`vars/storage_<backend>.yml`): `s3_namespace`,
`s3_service_name`, `s3_endpoint`, `s3_endpoint_external`,
`s3_force_path_style`. To add SeaweedFS or Rook: write the role, write the
contract file, flip `s3_backend`. Downstream roles (cnpg, byoc_logs) never
reference AIStor directly.

**Ingress flavor**: `byoc_logs` includes `tasks/ingress_<flavor>.yml`. To
support an nginx-based distro, add `ingress_nginx.yml` and set
`ingress_flavor: nginx` in the distro's contract.

## Preflight

`roles/preflight` runs on the node, from gathered facts, **before k3s is
installed** — so an undersized VM fails in seconds instead of after a
ten-minute run ending in Pending pods.

```
preflight_required_vcpu: 20
preflight_required_ram_gb: 64
preflight_required_disk_gb: 100
```

Raw OS values (`ansible_processor_vcpus`, `ansible_memtotal_mb`, `df`), matching
the runbook's target VM spec. RAM gets 5% slack because /proc/meminfo always
reports a little under nominal (firmware/kernel reserve) — a real 64 GB VM shows
~62-63 GB, and the check exists to catch the wrong VM size, not to count bytes.

Override in `group_vars/all/main.yml`; bypass with `-e preflight_skip=true`.
Run alone with `ansible-playbook site.yml -t preflight`.

## Storage backends

`s3_backend` in `group_vars/all/main.yml` selects both the role and the
contract file. Currently shipped: `aistor`, `seaweedfs`.

The contract (`vars/storage_<backend>.yml`) is what downstream roles consume —
they never reference a backend by name:

| key | aistor | seaweedfs |
|---|---|---|
| `s3_namespace` | `primary-object-store` | `seaweedfs` |
| `s3_service_name` | `minio` (operator-chosen — renamed from `myminio` in v6) | `seaweedfs-s3` (pinned via `fullnameOverride`) |
| `s3_port` | 9000 | **8333** |
| `s3_endpoint` | derived from host+port | derived from host+port |
| `s3_force_path_style` | true | true |

Both roles publish the same contract and use the same probe-first
idempotency (verify with the app key → provision only on failure → re-probe
as the verdict) and the same shared `templates/mc_verify.sh.j2`.

Where they genuinely differ — and why the role, not the contract, is the
right place for it:

- **Key provisioning.** AIStor creates the scoped app key *after* deploy
  (`mc admin accesskey create` + an IAM-style JSON policy). SeaweedFS has no
  `mc admin` equivalent: identities are declared in a JSON config secret the
  gateway reads *at startup*, with per-bucket actions (`Read:byocdata`, ...).
- **Health gate.** AIStor polls its ObjectStore CR for `healthStatus: green`.
  SeaweedFS has no CR — it gates on the S3 Service having ready endpoints.
- **Naming.** AIStor's Service name is chosen by the operator and has drifted
  between versions. SeaweedFS derives it from the release name, so the role
  pins `fullnameOverride` to make it deterministic.

Both roles assert the contract's Service actually exists before anything
resolves it (`Assert the contract's S3 service exists`).

### Adding a backend (e.g. Rook)

1. `roles/storage_rook/` — deploy it, gate on ready, provision buckets + a
   key scoped to `s3_buckets`, assert the Service exists.
2. `vars/storage_rook.yml` — publish `s3_namespace`, `s3_service_name`,
   `s3_port`, `s3_host`, `s3_endpoint`, `s3_endpoint_external`,
   `s3_force_path_style`.
3. Set `s3_backend: rook`. Nothing else changes.

## Layout

```
site.yml                      two plays: node prep (SSH) + cluster (localhost)
vars/storage_aistor.yml       storage backend contract
roles/
  k3s/                        distro role: install, wait Ready, fetch kubeconfig
  datadog_agent/              operator + DatadogAgent (self-monitoring)
  storage_aistor/             operator, tenant, green gate, AdminJob, probe
  cnpg/                       operator, cluster + WAL to S3, hourly backup
  byoc_logs/                  secrets, metastore URI munging, chart, ingress
  verify/                     pod readiness, WAL check, test log push
```

## Known gates encoded in the automation

- Datadog/CNPG operators: `helm wait` + retries on the first CR apply
  (admission webhooks lag the deployment by a few seconds).
- AIStor AdminJob only runs against a **green** store; `volumesPerServer: 1`
  keeps a single-disk node green. The playbook polls `status.healthStatus`.
- AdminJobs are one-shot; idempotency comes from probing actual bucket
  access with the app key, not from the AdminJob object.
- CNPG `barmanObjectStore` is deprecated (1.26) — migrate to the Barman
  Cloud Plugin before CNPG 1.30 if this outlives a few upgrades.
