#!/bin/sh
# =============================================================================
# Vault server entrypoint.
#
# Deliberately does not use the image's docker-entrypoint.sh: that wrapper injects
# VAULT_LOCAL_CONFIG into the environment, which is exactly the inline-secret
# pattern this deployment avoids. Configuration comes from the read-only mount at
# /vault/config, and Vault's own validation (it exits on an unknown directive)
# is the fail-closed check that an unverifiable configuration never serves.
# =============================================================================
set -eu

config_dir="${VAULT_CONFIG_DIR:-/vault/config}"
config_file="${config_dir}/vault.hcl"

if [ ! -r "$config_file" ]; then
    printf '{"status":"failed","service":"vault","code":"DEPENDENCY_UNAVAILABLE","message":"vault config is missing or unreadable","details":{"path":"%s"}}\n' \
        "$config_file" >&2
    exit 1
fi

if ! command -v vault >/dev/null 2>&1; then
    printf '{"status":"failed","service":"vault","code":"DEPENDENCY_UNAVAILABLE","message":"vault binary is not available in this image"}\n' >&2
    exit 1
fi

for directory in /vault/file /vault/logs; do
    if [ ! -w "$directory" ]; then
        printf '{"status":"failed","service":"vault","code":"DEPENDENCY_UNAVAILABLE","message":"vault data directory is not writable","details":{"path":"%s"}}\n' \
            "$directory" >&2
        exit 1
    fi
done

printf '{"status":"starting","service":"vault","config":"%s"}\n' "$config_file"

# `-config` (not the dev server): the store starts sealed and stays sealed until
# the operator supplies unseal material out of band.
exec vault server -config="$config_file"
