# BYOC Logs Lab — Ansible

[![ci](https://github.com/juanbrny/byoc-logs-lab-automaton/actions/workflows/ci.yml/badge.svg)](https://github.com/juanbrny/byoc-logs-lab-automaton/actions/workflows/ci.yml)

Automates the full single-node lab from the runbook: k3s, Datadog agent, S3 storage (either internal within lab server or external), PostgreSQL metadata store using CloudNativePG with WAL archiving, the
CloudPrem chart, Traefik ingress, and an end-to-end log-push verification.

> **Lab only.** Single node, local storage with no TLS, default root creds on
> the object store. Nothing here resembles production, just local testing.

## Prerequisites (workstation / control node)

- `ansible-core` >= 2.15, `kubectl`, `helm`
- `pip install kubernetes`
- `ansible-galaxy collection install -r requirements.yml`
- SSH root access to the lab VM (AWS SSM support will come next)

## First run

1. Create your inventory from the template:

   ```bash
   cp inventory/hosts.yml.example inventory/hosts.yml
   $EDITOR inventory/hosts.yml        # set ansible_host + the real hostname
   ```

   The real `hosts.yml` is gitignored. The node's address lives **only** there —
   `node_ip` is derived from it (`hostvars[...].ansible_host`), so there is no
   second place to keep in sync. The `lab_node` group must contain exactly one
   host; `site.yml` asserts this. Name the inventory host after the node's real
   hostname: a mismatch is how you end up debugging the wrong cluster.
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

## Credential flow

Storage backends **publish** the S3 app credentials; downstream roles
**consume** them. The direction matters, because backends differ in who picks
the keys:

| Backend | Who chooses the credentials |
|---|---|
| `aistor`, `seaweedfs` | **You do.** `s3_app_access_key` / `s3_app_secret_key` from the vault are an *input*: the backend creates that key and scopes it to `s3_buckets`. |
| `external` | **The other system does.** `external_s3_access_key` / `_secret_key` are issued elsewhere; the playbook validates and forwards them. |
| a future `rook` | **Ceph does.** It generates them and returns them in a Secret; the role would read and publish them. |

Every storage role ends by calling `s3_credentials/publish`, which sets facts
for this run **and** writes Secret `byoc-s3-credentials` into the storage
namespace. Downstream (`cnpg`, `byoc_logs`, `verify`) never reads
`s3_app_*` or `external_s3_*` — only the published `s3_access_key` /
`s3_secret_key`.

That Secret is why partial runs work: `-t byoc` on an existing deployment
resolves the credentials from it without re-running the storage role
(`s3_credentials/main`). If neither the facts nor the Secret exist, you get a
clear failure instead of an undefined-variable trace.

## Storage backends

`s3_backend` in `group_vars/all/main.yml` selects both the role and the
contract file. Currently shipped: `aistor`, `seaweedfs`.

The contract (`vars/storage_<backend>.yml`) is what downstream roles consume —
they never reference a backend by name:

| key | aistor | seaweedfs | external |
|---|---|---|---|
| `s3_namespace` | `primary-object-store` | `seaweedfs` | `byoc-s3` (mc pods + creds Secret only) |
| `s3_scheme` | http | http | usually **https** |
| `s3_host` | in-cluster service FQDN | in-cluster service FQDN | whatever you point it at |
| `s3_port` | 9000 | **8333** | 443 |
| `s3_force_path_style` | true | true | true (false for real AWS S3) |

`s3_endpoint` is always derived as `{scheme}://{host}:{port}` — CI fails any
contract that hardcodes it instead. `s3_service_name` is **backend-internal**
(aistor and seaweedfs use it to build the FQDN and to assert the Service
exists); `external` has no k8s Service, so it isn't part of the contract.

### Inputs vs contract

Every backend takes its own `<backend>_s3_*` **inputs** from
`group_vars/all/main.yml` and maps them onto the neutral `s3_*` **contract**
keys in `vars/storage_<backend>.yml`. Same shape for all three, so the knob you
reach for doesn't depend on which backend is active:

```yaml
aistor_s3_scheme: http      # -> s3_scheme
seaweedfs_s3_scheme: http   # -> s3_scheme
external_s3_scheme: http    # -> s3_scheme
```

The scheme is not cosmetic — it drives the deployment. `aistor_s3_scheme:
https` sets `disableAutoCert: false`, so the operator issues a self-signed cert;
pair it with `s3_tls_skip_verify: true` so `mc` (which every probe runs) gets
`--insecure`. SeaweedFS **asserts** the scheme is `http`: the chart can do TLS
but this repo doesn't wire the certs, and advertising https over a plaintext
gateway is worse than refusing. CI fails any contract that hardcodes a scheme
instead of deriving it from its input.

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

### `s3_backend: external` — an unmanaged store

Points the stack at an S3 store this playbook does not own: an existing MinIO,
StorageGRID, ECS, a Rook/Ceph RGW on another cluster, or real AWS S3. Nothing is
provisioned. The role asserts the store is fully specified, optionally creates
buckets (`external_s3_create_buckets`, off by default — most managed stores hand
you a key that cannot CreateBucket), then runs the **same** `mc_verify` probe
every other backend must pass, and publishes the contract.

```yaml
# group_vars/all/main.yml
s3_backend: external
external_s3_scheme: https
external_s3_host: s3.internal.example.com
external_s3_port: 443
external_s3_force_path_style: true    # false for real AWS S3
```

```yaml
# vaulted secrets.yml
external_s3_access_key: "..."
external_s3_secret_key: "..."
```

Buckets in `s3_buckets` must already exist (or set `external_s3_create_buckets`).

**Rook/Ceph** is deliberately *not* an in-repo backend. Single-node Rook needs a
raw block device the lab VM does not have, plus a pile of non-default tunables
(`mon.count: 1`, `failureDomain: osd`, replica size 1 with
`requireSafeReplicaSize: false`), and it competes with the BYOC pods for RAM.
Provision Ceph separately and consume its RGW endpoint through `external` —
same contract, none of the coupling.

### Adding a backend (e.g. Rook)

1. `roles/storage_<name>/` — deploy it, gate on ready, provision buckets and a
   key scoped to `s3_buckets`, then call `s3_credentials/publish` with whatever
   credentials it ended up with.
2. `vars/storage_<name>.yml` — publish the contract: `s3_namespace`,
   `s3_scheme`, `s3_host`, `s3_port`, `s3_endpoint`, `s3_endpoint_external`,
   `s3_force_path_style`. CI fails if a key is missing.
3. Set `s3_backend: <name>`. Nothing else changes.

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


## CI

`.github/workflows/ci.yml` runs on every push:

- **yamllint** + **ansible-lint** (`production` profile)
- **`ansible-playbook site.yml --syntax-check`** — catches undefined vars and
  dynamic role names that don't resolve
- **`ci/render_templates.py`** — renders every Jinja template against **every**
  storage backend contract with `StrictUndefined`, asserts the output is valid
  YAML/JSON, and fails if a template hardcodes a value the contract owns (e.g.
  an S3 port of 9000 when the active backend listens on 8333)
- **gitleaks** over the full history

CI has no inventory or secrets (both gitignored), so it materialises throwaway
ones from the committed `.example` files — which also proves the templates are
complete enough for a fresh clone to run.

Run the same checks locally before pushing:

```bash
yamllint . && ansible-lint && ansible-playbook site.yml --syntax-check \
  && python3 ci/render_templates.py
```
