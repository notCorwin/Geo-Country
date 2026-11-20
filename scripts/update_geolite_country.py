#!/usr/bin/env python3
"""Publish validated GeoLite Country releases without storing databases in Git."""
import datetime as dt
import email.utils
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import uuid

import maxminddb

ASSET = "GeoLite2-Country.mmdb"
PREFIX = "geolite-country-"
SOURCE = "https://download.maxmind.com/geoip/databases/GeoLite2-Country/download?suffix=tar.gz"
MARKER = "<!-- geolite-country-metadata:"


def command(args, *, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True)
    if result.returncode:
        # curl errors can contain presigned URLs; never print the captured output.
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode}); check credentials, network, and service status")
    return result.stdout


def gh(*args, input_text=None):
    return command(["gh", *args], input_text=input_text)


def api(path, *args, input_text=None):
    return json.loads(gh("api", path, *args, input_text=input_text))


def curl(netrc, *args):
    return command(["curl", "--silent", "--show-error", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "15", "--max-time", "180", "--netrc-file", str(netrc), *args])


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def metadata(release):
    try:
        body = release["body"] or ""
        data = json.loads(body.split(MARKER, 1)[1].split(" -->", 1)[0])
        if data["schema"] != 1:
            return None
        asset = next(a for a in release["assets"] if a["name"] == ASSET and a["state"] == "uploaded")
        if asset.get("digest") != "sha256:" + data["sha256"]:
            return None
        return data
    except (IndexError, KeyError, TypeError, ValueError, StopIteration):
        return None


def source_date(headers):
    # curl -I -L returns multiple header blocks; only the final response counts.
    blocks = re.split(r"\r?\n\r?\n", headers.strip())
    lines = blocks[-1].splitlines()
    values = [line.split(":", 1)[1].strip() for line in lines if line.lower().startswith("last-modified:")]
    if len(values) != 1:
        raise RuntimeError("MaxMind response is missing a unique Last-Modified header")
    return email.utils.parsedate_to_datetime(values[0]).astimezone(dt.timezone.utc)


def validate_archive(archive, target):
    with tarfile.open(archive, "r:gz") as tar:
        entries = [m for m in tar.getmembers() if m.isfile() and Path(m.name).name == ASSET]
        if len(entries) != 1 or entries[0].size > 100_000_000:
            raise RuntimeError("MaxMind archive does not contain exactly one valid Country database")
        source = tar.extractfile(entries[0])
        with target.open("wb") as out:
            while chunk := source.read(1024 * 1024):
                out.write(chunk)
    with maxminddb.open_database(str(target)) as reader:
        info = reader.metadata()
        if info.database_type != "GeoLite2-Country" or info.ip_version != 6:
            raise RuntimeError("Downloaded MMDB has an unexpected type or IP version")
        # Exercise both trees without assuming that any specific IP is assigned a country.
        reader.get("1.1.1.1")
        reader.get("2606:4700:4700::1111")
        return info.build_epoch


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    account = os.environ.get("MAXMIND_ACCOUNT_ID", "")
    key = os.environ.get("MAXMIND_LICENSE_KEY", "")
    if not re.fullmatch(r"[0-9]+", account) or not key or re.search(r"[\s\"\\]", key):
        raise RuntimeError("Set valid MAXMIND_ACCOUNT_ID and MAXMIND_LICENSE_KEY repository secrets")
    base = f"repos/{repo}"
    if api(base).get("visibility") != "public":
        raise RuntimeError("This workflow requires a public GitHub repository for anonymous client downloads")
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        netrc = work / "netrc"
        netrc.write_text(f"machine download.maxmind.com login {account} password {key}\n")
        netrc.chmod(0o600)
        date = source_date(curl(netrc, "--head", SOURCE))
        all_pages = api(base + "/releases?per_page=100", "--paginate", "--slurp")
        releases = [r for page in all_pages for r in page]
        managed = [r for r in releases if r["tag_name"].startswith(PREFIX) and MARKER in (r.get("body") or "")]
        # Determine latest without assuming that the most recently listed release is latest.
        latest = None
        # /latest returns 404 for an empty repository; treat it as absent only when no
        # published releases exist, not on an authentication or API failure.
        if any(not r["draft"] and not r["prerelease"] for r in releases):
            latest = api(base + "/releases/latest")
        current = metadata(latest) if latest and any(r["id"] == latest["id"] for r in managed) else None
        if current and dt.datetime.fromisoformat(current["source_last_modified"]) >= date:
            print("Published database is current and its GitHub asset digest matches.")
            # A prior run may have published successfully but failed URL verification.
            # Verify before cleaning anything left by that run.
            if any(r["id"] != latest["id"] for r in managed):
                verify = work / "published.mmdb"
                curl(netrc, "--output", str(verify), f"https://github.com/{repo}/releases/latest/download/{ASSET}")
                if digest(verify) != current["sha256"]:
                    raise RuntimeError("Fixed download URL does not match the latest release; old releases were retained")
        else:
            archive = work / "country.tar.gz"
            curl(netrc, "--output", str(archive), SOURCE)
            db = work / ASSET
            build_epoch = validate_archive(archive, db)
            sha = digest(db)
            data = {"schema": 1, "source_last_modified": date.isoformat(), "build_epoch": build_epoch, "sha256": sha}
            notes = work / "notes.txt"
            notes.write_text(
                "This product includes GeoLite Data created by MaxMind, available from https://www.maxmind.com.\n\n"
                "Use and sharing are subject to the [GeoLite EULA](https://www.maxmind.com/en/geolite/eula) "
                "and its incorporated CC BY-SA terms. Recipients must keep data current and destroy old databases "
                "within 30 days of a new upstream release, and pass on substantially similar obligations when sharing.\n\n"
                f"Source Last-Modified: {date.isoformat()}\n\nSHA-256: `{sha}`\n\n"
                + MARKER + json.dumps(data, separators=(",", ":")) + " -->\n"
            )
            tag = PREFIX + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
            gh("release", "create", tag, str(db), "--draft", "--title", "GeoLite Country", "--notes-file", str(notes))
            release = api(base + "/releases/tags/" + tag)
            if not metadata(release):
                raise RuntimeError("Uploaded draft asset digest does not match; existing release was not changed")
            previous_latest = latest
            try:
                payload = json.dumps({"draft": False, "make_latest": "true"})
                api(base + "/releases/" + str(release["id"]), "--method", "PATCH", "--input", "-", input_text=payload)
                latest = api(base + "/releases/latest")
                if latest["id"] != release["id"] or not metadata(latest):
                    raise RuntimeError("New release is not the verified latest")
                verify = work / "published.mmdb"
                curl(netrc, "--output", str(verify), f"https://github.com/{repo}/releases/latest/download/{ASSET}")
                if digest(verify) != sha:
                    raise RuntimeError("Fixed download URL did not return the new database")
            except Exception as error:
                if previous_latest:
                    try:
                        payload = json.dumps({"make_latest": "true"})
                        api(base + "/releases/" + str(previous_latest["id"]), "--method", "PATCH", "--input", "-", input_text=payload)
                        restored = api(base + "/releases/latest")
                        if restored["id"] != previous_latest["id"]:
                            raise RuntimeError("Previous latest release could not be confirmed")
                        print("Restored the previous latest release; both releases were retained.", file=sys.stderr)
                    except Exception as rollback_error:
                        raise RuntimeError("Publication verification failed and restoring the previous latest release failed; both releases were retained") from rollback_error
                raise RuntimeError("Publication verification failed; releases were retained") from error
            print(f"Published and verified GeoLite Country (build epoch {build_epoch}, SHA-256 {sha}).")
        # Clean only releases carrying our marker and prefix, including interrupted drafts.
        pages = api(base + "/releases?per_page=100", "--paginate", "--slurp")
        for release in (r for page in pages for r in page):
            if release["id"] != latest["id"] and release["tag_name"].startswith(PREFIX) and MARKER in (release.get("body") or ""):
                gh("release", "delete", release["tag_name"], "--yes", "--cleanup-tag")
                print("Removed an older managed release.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, tarfile.TarError, json.JSONDecodeError) as error:
        print(f"GeoLite update failed: {error}", file=sys.stderr)
        sys.exit(1)
