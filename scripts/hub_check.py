#!/usr/bin/env python3
"""Compare a running container's image against Docker Hub WITHOUT an account.

Why this exists: `docker manifest inspect` against registry-1.docker.io is
subject to Docker Hub's anonymous pull rate limit (100 pulls / 6h / IP), which
on the VPS is exhausted. Using it means the update check silently degrades to
"unknown" for every Hub image.

The fix is to use hub.docker.com's public metadata API instead. It is a
different service from the registry, is not under the pull-rate limit, and
needs no credentials.

The subtlety: the two sides are NOT the same kind of digest.
  - hub.docker.com's tag `digest` field  -> the MANIFEST digest
  - `docker inspect --format {{.Image}}` -> the CONFIG digest (image ID)
For caddy:2 those are 3422ce6d... and 2d8b1708... respectively, so a direct
string comparison is meaningless.

The reliable comparison is the RepoDigest: the local image records which
manifest digest it was pulled from, which IS comparable to the Hub API value.
"""
import json
import subprocess
import sys
import urllib.error
import urllib.request

HUB_API = "https://hub.docker.com/v2/repositories"


def hub_tag_digest(repo, tag):
    """Manifest digest for a Docker Hub tag. None if not found."""
    url = f"{HUB_API}/{repo}/tags/{tag}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r).get("digest")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"  hub api HTTP {e.code} for {repo}:{tag}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  hub api error for {repo}:{tag}: {e}", file=sys.stderr)
        return None


def local_manifest_digest(image_ref):
    """Manifest digest the local image was pulled from, via RepoDigests.

    e.g. caddy:2 -> sha256:86d4376a... (matches what the Hub API reports).
    Falls back to the image ID when RepoDigests is empty (locally built).
    """
    r = subprocess.run(
        ["docker", "image", "inspect", image_ref, "--format", "{{json .RepoDigests}}"],
        capture_output=True, text=True, timeout=30)
    if r.returncode == 0 and r.stdout.strip():
        try:
            digests = json.loads(r.stdout)
            for d in digests or []:
                if "@" in d:
                    return d.split("@", 1)[1]
        except Exception:
            pass
    r2 = subprocess.run(["docker", "image", "inspect", image_ref, "--format", "{{.Id}}"],
                        capture_output=True, text=True, timeout=30)
    return r2.stdout.strip() or None


def split_ref(ref):
    """'ghcr.io/kareadita/kavita:latest' -> (registry, repo, tag)."""
    tag = "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        ref, tag = ref.rsplit(":", 1)
    parts = ref.split("/")
    if len(parts) >= 2 and ("." in parts[0] or ":" in parts[0]):
        registry = parts[0]
        repo = "/".join(parts[1:])
    else:
        registry = "docker.io"
        repo = ref if "/" in ref else f"library/{ref}"
    return registry, repo, tag


if __name__ == "__main__":
    for ref in sys.argv[1:]:
        reg, repo, tag = split_ref(ref)
        local = local_manifest_digest(ref)
        print(f"\n{ref}")
        print(f"  registry   : {reg}")
        print(f"  repo:tag   : {repo}:{tag}")
        print(f"  local digest: {local}")
        if reg == "docker.io":
            remote = hub_tag_digest(repo, tag)
            print(f"  hub  digest : {remote}")
            if not remote:
                print("  => UNKNOWN (tag not found in Hub API)")
            elif not local:
                print("  => UNKNOWN (no local digest)")
            elif local == remote:
                print("  => CURRENT")
            else:
                print("  => UPDATE AVAILABLE")
        else:
            print("  => (non-Hub registry; use docker manifest inspect)")