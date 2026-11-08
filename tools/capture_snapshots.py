"""Capture real browser screenshots of the evidence-backed local result views."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

VIEWPORTS = {
    "01-run-and-tests": (1600, 1320),
    "02-publication": (1600, 910),
    "03-offer-ranking": (1600, 1110),
    "04-benefit-lifecycle": (1600, 1020),
    "05-failure-and-recovery": (1600, 1100),
    "06-ledger-and-replay": (1600, 1370),
    "07-execution-boundary": (1600, 1050),
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _browser_path(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise ValueError("--browser must name a local Chrome/Chromium executable")
        return explicit.resolve()
    choices = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    choices.extend(
        Path(value)
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
        if (value := shutil.which(name))
    )
    for path in choices:
        if path.is_file():
            return path.resolve()
    raise ValueError(
        "Screenshot capture needs a local Chrome/Chromium browser; pass --browser /path/to/executable"
    )


def capture_snapshots(
    results_dir: Path, browser: Path | None = None, replace: bool = False
) -> None:
    """Capture retained HTML with isolated browser profiles and bind image hashes."""
    from .record_results import verify_saved_run
    from .results_render import render_results

    results_dir = results_dir.resolve()
    evidence_manifest = results_dir / "evidence/evidence_manifest.json"
    manifest = json.loads(evidence_manifest.read_text())
    verify_saved_run(evidence_manifest.parent)
    rendered_path = results_dir / "render_manifest.json"
    rendered = json.loads(rendered_path.read_text())
    expected_files = {"index.html"} | {f"views/{slug}.html" for slug in VIEWPORTS}
    if (
        rendered.get("run_id") != manifest["run_id"]
        or rendered.get("evidence_manifest_sha256") != _sha(evidence_manifest)
        or rendered.get("source_tree_sha256") != manifest["source"]["tree_sha256"]
        or set(rendered.get("files", {})) != expected_files
        or any(_sha(results_dir / name) != digest for name, digest in rendered["files"].items())
    ):
        raise ValueError(
            "Rendered views do not match this evidence. Refresh with python3 -m tools.results_render --evidence <results>/evidence"
        )
    rendered_hash = _sha(rendered_path)
    destination = results_dir / "snapshots"
    if destination.exists() and not replace:
        raise FileExistsError(
            "Snapshots already exist; use --replace to regenerate this results directory"
        )
    executable = _browser_path(browser)
    version = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, check=True, timeout=15
    ).stdout.strip()
    snapshots = []
    with tempfile.TemporaryDirectory(prefix="festive-snapshots-") as staging:
        stage = Path(staging)
        for slug, (width, height) in VIEWPORTS.items():
            view = results_dir / "views" / f"{slug}.html"
            if not view.is_file():
                raise FileNotFoundError(f"Missing rendered result view: {view.name}")
            output = stage / f"{slug}.png"
            with tempfile.TemporaryDirectory(prefix="festive-chrome-") as profile:
                command = [
                    str(executable),
                    "--headless",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--hide-scrollbars",
                    "--force-device-scale-factor=1",
                    f"--user-data-dir={profile}",
                    f"--window-size={width},{height}",
                    "--virtual-time-budget=1200",
                    f"--screenshot={output}",
                    view.as_uri(),
                ]
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                if result.returncode or not output.is_file() or output.stat().st_size < 10000:
                    raise RuntimeError(
                        f"Browser did not produce a complete screenshot for {slug}: exit {result.returncode}"
                    )
            snapshots.append(
                {
                    "section": slug,
                    "run_id": manifest["run_id"],
                    "html": f"views/{slug}.html",
                    "html_sha256": _sha(view),
                    "png": f"snapshots/{slug}.png",
                    "png_sha256": _sha(output),
                    "viewport": {"width": width, "height": height, "device_scale_factor": 1},
                    "command": command,
                    "command_template": [
                        str(executable),
                        "--headless",
                        "--disable-gpu",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--hide-scrollbars",
                        "--force-device-scale-factor=1",
                        "--user-data-dir=<temporary isolated profile>",
                        f"--window-size={width},{height}",
                        "--virtual-time-budget=1200",
                        f"--screenshot=<results>/snapshots/{slug}.png",
                        f"file://<results>/views/{slug}.html",
                    ],
                }
            )
        snapshot_manifest = {
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "kind": "browser screenshots of reports rendered from actual retained local evidence; not Databricks or live checkout UI",
            "browser_version": version,
            "evidence_manifest_sha256": _sha(evidence_manifest),
            "render_manifest_sha256": rendered_hash,
            "source_tree_sha256": manifest["source"]["tree_sha256"],
            "snapshots": snapshots,
        }
        (stage / "snapshot_manifest.json").write_text(
            json.dumps(snapshot_manifest, indent=2) + "\n"
        )
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(stage, destination)
    # README gains image links only after this evidence set actually has snapshots.
    # The view bytes and their binding manifest stay deterministic.
    render_results(evidence_manifest.parent, results_dir)
    if _sha(rendered_path) != rendered_hash:
        raise RuntimeError("Rendered views changed while screenshot capture completed")
    print(f"Captured {len(snapshots)} browser snapshots for {manifest['run_id']}: {destination}")


def main() -> None:
    """Regenerate screenshot artifacts without running tests or financial events."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results", type=Path, default=Path(__file__).resolve().parents[1] / "docs/results"
    )
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    capture_snapshots(args.results, args.browser, args.replace)


if __name__ == "__main__":
    main()
