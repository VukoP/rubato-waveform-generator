# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Repository policy: workflow hardening, blob size and the changelog extractor."""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
HOSTED_RUNNERS = {
    "ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04", "ubuntu-24.04-arm", "ubuntu-22.04-arm",
    "macos-latest", "macos-26", "macos-15", "macos-14", "macos-13",
    "windows-latest", "windows-2025", "windows-2022", "windows-11-arm",
}
MAX_BLOB_BYTES = 5 * 1024 * 1024
SHA_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
FORBIDDEN_TRIGGERS = ("pull_request_target", "workflow_run", "issue_comment")
INJECTION = re.compile(r"\$\{\{\s*(github\.(event|head_ref|ref_name)|inputs)")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True)) or {}


def _permissions_write(perms) -> set:
    if perms == "write-all":
        return {"write-all"}
    return {k for k, v in perms.items() if v == "write"} if isinstance(perms, dict) else set()


def workflow_violations(workflow: dict) -> list[str]:
    """Policy violations of one parsed workflow; empty when it is clean."""
    found = []
    triggers = _triggers(workflow)
    for trigger in FORBIDDEN_TRIGGERS:
        if trigger in (triggers if isinstance(triggers, (dict, list, str)) else ()):
            found.append(f"uses {trigger}")
    if "permissions" not in workflow:
        found.append("no top-level permissions")
    elif "write-all" in str(workflow["permissions"]) or _permissions_write(workflow["permissions"]):
        found.append("top-level permissions grant write access")
    for name, job in (workflow.get("jobs") or {}).items():
        if "uses" in job:
            # A reusable-workflow call: it has no runner or timeout of its own.
            uses = str(job["uses"])
            if not uses.startswith("./") and not SHA_PIN.match(uses):
                found.append(f"{name}: '{uses}' is not pinned to a full commit SHA")
            if job.get("secrets") == "inherit":
                found.append(f"{name}: secrets: inherit")
            if _permissions_write(job.get("permissions")):
                found.append(f"{name}: write permission on a called workflow")
            continue
        runs_on = job.get("runs-on")
        labels = runs_on if isinstance(runs_on, list) else [runs_on]
        for label in labels:
            if "self-hosted" in str(label):
                found.append(f"{name}: self-hosted runner")
            elif not isinstance(label, str):
                found.append(f"{name}: unreadable runs-on {label!r}")
            elif label.startswith("${{"):
                # A matrix expression: every value in the matrix is checked below.
                for entry in (job.get("strategy") or {}).get("matrix", {}).get("include", []):
                    if entry.get("os") not in HOSTED_RUNNERS:
                        found.append(f"{name}: runner {entry.get('os')!r} is not allow-listed")
            elif label not in HOSTED_RUNNERS:
                found.append(f"{name}: runner {label!r} is not allow-listed")
        if "timeout-minutes" not in job:
            found.append(f"{name}: no timeout-minutes")
        writes = _permissions_write(job.get("permissions"))
        if writes and not (name == "publish" and writes == {"contents"}):
            found.append(f"{name}: write permission {sorted(writes)} outside the publish job")
        for step in job.get("steps") or []:
            uses = step.get("uses")
            if uses and not uses.startswith("./") and not SHA_PIN.match(uses):
                found.append(f"{name}: '{uses}' is not pinned to a full commit SHA")
            if INJECTION.search(str(step.get("run", ""))):
                found.append(f"{name}: untrusted context interpolated into a run script")
    if "self-hosted" in yaml.safe_dump(workflow):
        found.append("mentions self-hosted")
    return found


def test_workflows_exist():
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "release.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_policy(path):
    assert workflow_violations(_load(path)) == []


def test_contents_write_only_in_release_publish():
    for path in WORKFLOWS:
        for name, job in (_load(path).get("jobs") or {}).items():
            writes = "contents" in _permissions_write(job.get("permissions"))
            assert writes == (path.name == "release.yml" and name == "publish"), f"{path.name}:{name}"


def test_checker_rejects_violations(tmp_path):
    good = _load(ROOT / ".github" / "workflows" / "ci.yml")
    assert workflow_violations(good) == []
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "on: pull_request_target\n"
        "jobs:\n"
        "  x:\n"
        "    runs-on: [self-hosted, linux]\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: echo ${{ github.event.pull_request.title }}\n"
        "      - run: echo ${{github.head_ref}} ${{ inputs.name }} ${{ github.ref_name }}\n"
        "  y:\n"
        "    uses: some/workflow/.github/workflows/w.yml@v1\n"
        "    secrets: inherit\n")
    found = " | ".join(workflow_violations(_load(bad)))
    for expected in ("pull_request_target", "self-hosted", "no top-level permissions", "no timeout-minutes",
                     "not pinned", "untrusted context", "y: secrets: inherit",
                     "y: 'some/workflow/.github/workflows/w.yml@v1' is not pinned"):
        assert expected in found
    for trigger in ("workflow_run", "issue_comment"):
        assert f"uses {trigger}" in " | ".join(workflow_violations({"on": {trigger: {}}, "permissions": {}}))
    local = {"on": "push", "permissions": {"contents": "read"}, "jobs": {"ci": {"uses": "./.github/workflows/ci.yml"}}}
    assert workflow_violations(local) == []
    local["jobs"]["ci"]["permissions"] = {"contents": "write"}
    assert "write permission on a called workflow" in " | ".join(workflow_violations(local))


def test_no_large_tracked_blobs():
    listing = subprocess.run(["git", "ls-files", "-s"], check=False, cwd=ROOT,
                             capture_output=True, text=True)
    if listing.returncode != 0:
        pytest.skip("not a git checkout")
    entries = [line.split(maxsplit=3) for line in listing.stdout.splitlines()]
    sizes = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectsize)"], check=False, cwd=ROOT, capture_output=True,
        text=True,
        input="".join(entry[1] + "\n" for entry in entries)).stdout.split()
    big = [(entry[3], int(size)) for entry, size in zip(entries, sizes) if int(size) > MAX_BLOB_BYTES]
    assert big == []


# ------------------------------------------------------------ changelog_section.py

def _changelog_module():
    spec = importlib.util.spec_from_file_location("changelog_section", ROOT / "scripts" / "changelog_section.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHANGELOG = """# Changelog

## [Unreleased]

- Nothing yet.

## [1.1.0] - 2026-12-01

### Added
- Second.

## [1.0.0] - 2026-11-03

### Added
- First.
"""


def test_changelog_section_extracts_one_version():
    module = _changelog_module()
    assert module.changelog_section(CHANGELOG, "v1.1.0") == "## [1.1.0] - 2026-12-01\n\n### Added\n- Second.\n"
    assert module.changelog_section(CHANGELOG, "1.0.0").endswith("- First.\n")
    assert module.changelog_section(CHANGELOG, "v1.1") is None
    assert module.changelog_section(CHANGELOG, "v2.0.0") is None


def test_changelog_cli(tmp_path, capsys):
    module = _changelog_module()
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    assert module.main(["v1.0.0", "--file", str(path)]) == 0
    assert capsys.readouterr().out.startswith("## [1.0.0] - 2026-11-03")
    assert module.main(["v9.9.9", "--file", str(path)]) == 1
    assert "no section for v9.9.9" in capsys.readouterr().err
    assert module.main(["v1.0.0", "--file", str(tmp_path / "missing.md")]) == 1
