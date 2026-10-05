#!/usr/bin/env bash
# Check for container updates and mail a summary to hyperion.
#
# Runs on the VPS (where all the monitored containers live) and mails isaboo@hyperion
# over the tailnet when something is out of date. Silent when everything is current,
# so a quiet run costs one notification and nothing else.
#
# "Needs update" means: the image tag exists locally but the registry reports a
# different digest for that tag. Comparing by digest rather than by tag string is
# what makes this correct — `ghcr.io/x/y:latest` looks identical whether or not it
# was rebuilt upstream.
set -uo pipefail

# Path to notify.py. Defaults to the sibling script in whatever directory this
# file lives in, so the same checkout works on hyperion and on the VPS — do not
# hardcode one host's path here.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTIFY="${HELM_NOTIFY:-$SCRIPT_DIR/notify.py}"

# Registry credentials, if any, for pulling digests of private images. Public
# images need none.
DIGEST_AUTH=()
if [[ -n "${REGISTRY_AUTH_FILE:-}" && -r "${REGISTRY_AUTH_FILE}" ]]; then
    DIGEST_AUTH=(--authfile "${REGISTRY_AUTH_FILE}")
fi

# Return "<image>\t<local digest>" for every running container.
running_containers() {
    docker ps --format '{{.Names}}|{{.Image}}|{{.ID}}'
}

# Container images built on this host rather than pulled from a registry
# (helm, the *-proxy images). There is nothing upstream to compare against, so
# they are expected to be UNKNOWN — they are NOT a failure.
is_local_build() {
    case "$1" in
        helm|helm-*|*-proxy) return 0 ;;
        *) return 1 ;;
    esac
}

# Best-effort remote CONFIG digest for an image ref, or empty when unknown.
#
# This must return the *config* digest, because that is what
# `docker inspect --format {{.Image}}` reports. Two traps, both hit while
# building this:
#
#   1. `.Descriptor.digest` is the MANIFEST digest, a different value. For
#      caddy:2 the running image ID is sha256:2d8b1708... but the manifest
#      digest is sha256:86d4376a... — compare those and every container looks
#      permanently out of date. The config digest is nested one level deeper,
#      at `.OCIManifest.config.digest` (sha256:5e0a314e... for the same tag).
#
#   2. The index carries one entry per platform, so the host architecture has
#      to match or an arm64 digest gets compared against an amd64 image ID.
#      Entries with unknown/unknown platform are attestation manifests
#      (SBOM, signatures) and must never be selected.
HUB_RATELIMIT_SEEN=0

# Is this ref served by Docker Hub (including the bare "caddy:2" shorthand)?
is_hub_ref() {
    local ref="$1" first
    if [[ "$ref" == */* ]]; then
        first="${ref%%/*}"
        # A leading segment containing a dot or colon is a registry hostname.
        # Anything else means the ref is already relative to docker.io.
        [[ "$first" == *.* || "$first" == *:* ]] || return 0
        case "$first" in
            docker.io|index.docker.io|registry-1.docker.io) return 0 ;;
            *) return 1 ;;
        esac
    fi
    return 0
}

# Strip the registry prefix and any :tag, leaving "owner/repo" and "tag".
# Sets the globals HUB_REPO and HUB_TAG.
split_ref() {
    local ref="$1" name="$ref" first
    if [[ "$ref" == */* ]]; then
        first="${ref%%/*}"
        if [[ "$first" == *.* || "$first" == *:* ]]; then
            name="${ref#*/}"
        fi
    fi
    HUB_TAG="latest"
    if [[ "$name" == *:* ]]; then
        HUB_TAG="${name##*:}"
        name="${name%:*}"
    fi
    # Hub canonicalizes single-segment names to library/<name>.
    [[ "$name" != */* ]] && name="library/$name"
    HUB_REPO="$name"
}

# Docker Hub images are checked through hub.docker.com's public metadata API
# rather than `docker manifest inspect`. Reason: the registry endpoint is
# subject to Docker Hub's anonymous pull rate limit (100 pulls / 6h / source
# IP), which the VPS exhausts, and that silently turns every Hub image into
# "unknown". hub.docker.com is a separate metadata service, is not under that
# limit, and needs no account — so the check works with no Hub credentials.
#
# The comparison is manifest-digest to manifest-digest, via the local image's
# RepoDigests entry. Do NOT compare against `docker inspect {{.Image}}`: that
# is the CONFIG digest, a different value (for caddy:2, 2d8b1708... vs the
# manifest's 3422ce6d...), so comparing them reports every image as stale.
hub_tag_digest() {
    local repo="$1" tag="$2"
    timeout 20 python3 - "$repo" "$tag" <<'PYEOF' 2>/dev/null
import json, sys, urllib.request
repo, tag = sys.argv[1], sys.argv[2]
url = f"https://hub.docker.com/v2/repositories/{repo}/tags/{tag}"
try:
    with urllib.request.urlopen(url, timeout=15) as r:
        print(json.load(r).get("digest") or "")
except Exception:
    print("")
PYEOF
}

# The manifest digest this local image was pulled from, which IS comparable to
# what hub.docker.com reports. Empty for locally-built images (no RepoDigests).
local_manifest_digest() {
    docker image inspect "$1" --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null \
        | grep -m1 '@' | sed 's/.*@//'
}

remote_digest() {
    local ref="$1"
    # An image with an explicit digest pinned is never going to drift.
    [[ "$ref" == *"@sha256:"* ]] && return 1

    if is_hub_ref "$ref"; then
        split_ref "$ref"
        hub_tag_digest "$HUB_REPO" "$HUB_TAG"
        return 0
    fi

    # Non-Hub registries (ghcr.io, codeberg.org, quay.io) have no anonymous
    # pull limit, so the registry API is fine for them.
    local out
    out="$(timeout 20 docker manifest inspect --verbose "$ref" "${DIGEST_AUTH[@]}" 2>&1)"
    if printf '%s' "$out" | grep -qi 'toomanyrequests\|rate limit'; then
        HUB_RATELIMIT_SEEN=1
        return 1
    fi
    printf '%s' "$out" | ARCH="$DOCKER_ARCH" python3 -c '
import json, os, sys
arch = os.environ.get("ARCH", "")
try:
    entries = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(entries, list):
    entries = [entries]
for e in entries:
    desc = e.get("Descriptor") or {}
    plat = desc.get("platform") or {}
    oci = e.get("OCIManifest") or {}
    cfg = (oci.get("config") or {}).get("digest")
    if not cfg:
        continue
    if plat.get("architecture") == arch and plat.get("os") == "linux":
        print(cfg)
        sys.exit(0)
sys.exit(1)
'
}

# The digest of the image a container is actually running.
container_digest() {
    local cid="$1"
    docker inspect --format '{{.Image}}' "$cid" 2>/dev/null
}

# Host architecture in OCI naming ("amd64", not uname's "x86_64"). Getting this
# wrong makes every multi-arch image fail the platform match below, which
# silently turns into "unknown" rather than an error.
DOCKER_ARCH="$(docker version --format '{{.Server.Arch}}' 2>/dev/null)"

updates=()
checked=0
unknown=0
local_builds=0
# Registry images we could not verify, reported by name. A bare count is not
# actionable — "stale" and "rate-limited" need very different responses, so
# keep the names and say why.
unknown_names=()

while IFS='|' read -r name ref cid; do
    [[ -z "${name:-}" ]] && continue
    checked=$((checked + 1))

    if is_local_build "$ref"; then
        local_builds=$((local_builds + 1))
        continue
    fi

    remote=$(remote_digest "$ref")
    # Which local digest is comparable depends on how remote_digest resolved it:
    #   - Hub images resolve via hub.docker.com, which reports the MANIFEST
    #     digest, so the local side must be RepoDigests.
    #   - Other registries resolve via `docker manifest inspect --verbose`,
    #     where this script extracts the CONFIG digest, so the local side must
    #     be the image ID.
    # Mixing these up compares a config digest to a manifest digest and reports
    # every image as stale.
    local_val=""
    if is_hub_ref "$ref"; then
        local_val=$(local_manifest_digest "$ref")
    else
        local_val=$(container_digest "$cid")
    fi
    # Fall back to the container name if the .Image ref string didn't resolve.
    if [[ -z "$local_val" ]]; then
        local_val=$(local_manifest_digest "$name")
    fi
    local_digest="$local_val"

    if [[ -z "$remote" || -z "$local_digest" ]]; then
        unknown=$((unknown + 1))
        unknown_names+=("$name"$'\t'"$ref")
        continue
    fi

    if [[ "$local_digest" != "$remote" ]]; then
        updates+=("$name"$'\t'"$ref")
    fi
done < <(running_containers)

report_unknown() {
    [[ $unknown -eq 0 ]] && return 0
    echo "check-docker-updates: WARNING $unknown of $checked registry image(s) could NOT be" >&2
    echo "  verified — treat them as unverified, NOT as up to date:" >&2
    for u in "${unknown_names[@]}"; do
        echo "    ${u%%$'\t'}"$'\t'"${u##*$'\t'}" >&2
    done
    # Docker Hub's anonymous pull limit is per source IP and is the usual
    # cause. ghcr.io and quay.io images are unaffected, so a partial failure
    # like this points at Hub specifically.
    if [[ $HUB_RATELIMIT_SEEN -eq 1 ]]; then
        echo "  Cause: Docker Hub anonymous pull rate limit for this host's IP." >&2
        echo "  Fix:   'docker login' on the VPS raises the limit substantially." >&2
    fi
}

if [[ ${#updates[@]} -eq 0 ]]; then
    echo "check-docker-updates: $checked container(s) seen, no updates found" >&2
    if [[ $local_builds -gt 0 ]]; then
        echo "  ($local_builds locally-built image(s) skipped — nothing to compare against)" >&2
    fi
    report_unknown
    exit 0
fi

{
    echo "The following containers have a newer image available:"
    echo
    printf '  %-24s %s\n' "CONTAINER" "IMAGE"
    for u in "${updates[@]}"; do
        printf '  %-24s %s\n' "${u%%$'\t'*}" "${u##*$'\t'}"
    done
    echo
    echo "Apply with:  cd ~/helm && docker compose pull && docker compose up -d"
} > /tmp/helm-docker-update-body.txt

subject="Docker updates available on vps (${#updates[@]} of $checked)"

# Don't mail an empty report if the mail path itself is broken -- that would
# loop. Failure here is logged to stderr and exit code, not mailed.
if ! python3 "$NOTIFY" "$subject" "$(cat /tmp/helm-docker-update-body.txt)"; then
    echo "check-docker-updates: could not send notification" >&2
    exit 1
fi

echo "check-docker-updates: ${#updates[@]} update(s) found and mailed" >&2
[[ $unknown -gt 0 ]] && \
    echo "check-docker-updates: $unknown container(s) couldn't be checked (no registry digest)" >&2
exit 0