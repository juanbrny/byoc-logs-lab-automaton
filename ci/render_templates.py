#!/usr/bin/env python3
"""Render every Jinja template against every storage backend contract.

Catches, without a cluster: undefined vars, malformed YAML/JSON output, and
templates that silently hardcode a value the contract is supposed to own
(e.g. an S3 port of 9000 when the active backend listens on 8333).
"""
import glob
import json
import sys

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

REPO_VARS = yaml.safe_load(open("group_vars/all/main.yml"))
BACKENDS = [p.split("storage_")[1][:-4] for p in glob.glob("vars/storage_*.yml")]

BASE = dict(
    node_ip="10.0.0.10",
    lab_node_name="ci-node",
    playbook_dir=".",
    datadog_api_key="ci", aistor_license="ci",
    s3_access_key="CIACCESSKEY0000000AB",
    s3_secret_key="cisecretkey000000000000000000000000000AB",
    aistor_root_password="ci", seaweedfs_admin_secret_key="ci",
)


def role_defaults(role):
    try:
        return yaml.safe_load(open(f"roles/{role}/defaults/main.yml")) or {}
    except FileNotFoundError:
        return {}


def resolve(env, mapping):
    """group_vars/contract values are themselves Jinja — resolve to fixpoint."""
    for _ in range(10):
        changed = False
        for k, v in list(mapping.items()):
            if isinstance(v, str) and "{{" in v:
                new = env.from_string(v).render(**mapping)
                if new != v:
                    mapping[k], changed = new, True
        if not changed:
            break
    return mapping


def main():
    env = Environment(loader=FileSystemLoader("."), undefined=StrictUndefined)
    failures = []

    for backend in sorted(BACKENDS):
        contract = yaml.safe_load(open(f"vars/storage_{backend}.yml"))
        v = {**REPO_VARS, **BASE, **contract}
        for role in ("storage_" + backend, "byoc_logs", "cnpg", "datadog_agent",
                     "k3s", "verify", "preflight"):
            v = {**role_defaults(role), **v}
        v = resolve(env, v)

        print(f"\n--- backend: {backend}  ({v['s3_endpoint']})")

        # the contract must not be silently bypassed by a hardcoded endpoint
        if f":{v['s3_port']}" not in v["s3_endpoint"]:
            failures.append(f"{backend}: s3_endpoint does not use s3_port")

        paths = glob.glob("roles/*/templates/*.j2") + glob.glob("templates/*.j2")
        for path in sorted(paths):
            # a role's templates only render under its own backend
            if "/storage_" in path and f"/storage_{backend}/" not in path:
                continue
            try:
                out = env.get_template(path).render(**v)
                if path.endswith(".json.j2"):
                    json.loads(out)
                    kind = "JSON ok"
                elif path.endswith(".sh.j2"):
                    kind = "shell ok"
                else:
                    yaml.safe_load(out)
                    kind = "YAML ok"
                # a template that bakes in another backend's port is a bug
                bad_port = "9000" if v["s3_port"] != 9000 else "8333"
                if bad_port in out and "s3_port" not in open(path).read():
                    failures.append(f"{backend}: {path} hardcodes port {bad_port}")
                print(f"    {path}: {kind}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{backend}: {path}: {exc}")
                print(f"    {path}: FAIL — {exc}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("\nAll templates render for all backends.")


if __name__ == "__main__":
    main()
