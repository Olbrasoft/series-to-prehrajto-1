#!/usr/bin/env python3
"""Daily external subtitle discovery and manually approved attachment.

State and binary files travel in one serialized Actions artifact; small manual
decisions live in plans/opensubtitles-reviews.jsonl. No subtitle is retimed.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from itertools import zip_longest
from pathlib import Path

import requests

from backfill_uploaded_subtitles import (find_profile_detail, load_latest_status, load_uploads,
                                        matching_upload_status, normalize_srt, pick_czech_track,
                                        upload_subtitle)
from opensubtitles_client import OpenSubtitles, ProviderStopped, timestamp, write_json
from prehrajto_upload import login
from resolve_stream import GOOGLEBOT_HEADERS, parse_html
from review_subtitles_with_whisper import apply_manual_review, collect, parse_cues, render_review
from verify_subtitle_delivery import verify

REPO = Path(__file__).resolve().parents[1]
ARTIFACT_PREFIX = "external-subtitle-state-"
WORKFLOW = "external-subtitles.yml"
PENDING = {"awaiting_whisper", "awaiting_manual_review"}
SUBMITTED = {"submission_pending", "submitted", "submission_unknown"}


def candidate_key(row):
    account = row["upload_account"]
    if account not in {"primary", "serialy"}:
        raise ValueError("Unknown attachment account")
    return f"{account}-{int(row['target_video_id'])}-{int(row['file_id'])}"


def evidence_digest(evidence):
    value = {key: evidence[key] for key in ("target_video_id", "subtitle_file_id", "subtitle_sha256", "target_duration_sec", "clips")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def approval(row, decision):
    """Validate recorded manual review and exact input bytes; never infer it."""
    if not decision or decision.get("upload_account") != row["upload_account"]:
        return False
    if int(decision.get("episode_id") or 0) != int(row["episode_id"]):
        return False
    if decision.get("evidence_sha256") != evidence_digest(row["evidence"]):
        return False
    reviewed = apply_manual_review(row["evidence"], decision)
    return reviewed["status"] == "sampled_content_and_timing_verified"


def transport_srt(path):
    """Normalize encoding/line endings only; never move or scale cue times."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1250")
    return normalize_srt(text.encode("utf-8"))


def target(row):
    response = requests.get(row["target_detail_url"], headers={**GOOGLEBOT_HEADERS, "Cache-Control": "no-cache"}, timeout=25)
    response.raise_for_status()
    resolved = parse_html(response.text, row["target_detail_url"])
    if resolved.video_id != int(row["target_video_id"]) or resolved.duration_sec != row["video_duration_sec"]:
        raise ValueError("Target identity or duration changed")
    return resolved


class Pipeline:
    def __init__(self, directory):
        self.root = directory.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"version": 1, "candidates": {}, "episodes": {}}
        if self.state.get("version") != 1:
            raise ValueError("Unsupported external subtitle state")

    def save(self):
        write_json(self.path, self.state)

    def fingerprint(self):
        digest = hashlib.sha256()
        for path in (self.path, self.root / "provider/provider.json"):
            digest.update(path.read_bytes() if path.exists() else b"missing")
        return digest.hexdigest()

    def file(self, row):
        path = self.root / "provider" / f"{int(row['file_id'])}.srt"
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != row["subtitle_sha256"]:
            raise ValueError("Reviewed subtitle file missing or changed")
        return path

    def decisions(self, path):
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
        return {candidate_key({**r, "file_id": r["subtitle_file_id"]}): r for r in rows}

    def record_reviews(self, path):
        for key, row in self.state["candidates"].items():
            if row["status"] not in PENDING or not row.get("evidence"):
                continue
            decision = self.decisions(path).get(key)
            if decision:
                if approval(row, decision):
                    row["status"] = "approved"
                elif decision.get("status") in {"timing_mismatch", "inconclusive"}:
                    # Check identities even for rejection; a foreign review must
                    # not close or approve the current candidate.
                    if decision.get("evidence_sha256") == evidence_digest(row["evidence"]):
                        apply_manual_review(row["evidence"], decision)
                        row["status"] = "rejected"
                row["reviewed_at"] = decision.get("checked_at")
        self.save()

    def export(self):
        self.prune()
        waiting = []
        for row in self.state["candidates"].values():
            if row["status"] in PENDING:
                waiting.append({**row, "local_subtitle_file": str(self.file(row))})
        write_json(self.root / "review-candidates.json", waiting)
        evidence = [row["evidence"] for row in self.state["candidates"].values() if row["status"] in PENDING and row.get("evidence")]
        page = self.root / "manual-review.html"
        page.write_text(render_review(evidence))
        page.chmod(0o600)
        template = []
        for row in self.state["candidates"].values():
            if row["status"] == "awaiting_manual_review":
                template.append({"episode_id": row["episode_id"], "upload_account": row["upload_account"],
                                 "target_video_id": row["target_video_id"], "subtitle_file_id": row["file_id"],
                                 "subtitle_sha256": row["subtitle_sha256"], "target_duration_sec": row["video_duration_sec"],
                                 "evidence_sha256": evidence_digest(row["evidence"]), "status": "inconclusive",
                                 "semantic_content_confirmed": False, "timing_confirmed": False,
                                 "timing_adjustments_applied": False, "reviewer": "", "checked_at": "",
                                 "reviewed_anchors": []})
        write_json(self.root / "manual-review-template.json", template)
        write_json(self.root / "summary.json", {"checked_at": timestamp(), "counts": dict(Counter(r["status"] for r in self.state["candidates"].values()))})

    def prune(self):
        """Keep active evidence and a bounded recent cache, not all historic WAV/text."""
        terminal = [row for row in self.state["candidates"].values()
                    if row["status"] not in PENDING | SUBMITTED | {"approved"}]
        terminal.sort(key=lambda row: row.get("prepared_at", ""), reverse=True)
        for row in terminal[100:]:
            row.pop("evidence", None)
        for row in terminal:
            if row["status"] == "uploaded":
                row.pop("evidence", None)
        provider = self.root / "provider/provider.json"
        if provider.exists():
            data = json.loads(provider.read_text())
            recent = sorted(data["files"], key=lambda key: data["files"][key].get("downloaded_at", ""), reverse=True)[:300]
            active = {str(row["file_id"]) for row in self.state["candidates"].values()
                      if row["status"] in PENDING | SUBMITTED | {"approved"}}
            keep = set(recent) | active
            data["files"] = {key: value for key, value in data["files"].items() if key in keep}
            write_json(provider, data)
            for path in provider.parent.glob("*.srt"):
                if path.stem not in keep:
                    path.unlink()
        self.save()


def uploads_by_account():
    paths = list((REPO / "state").glob("uploaded-shard-*.json"))
    return {account: load_uploads(paths, account) for account in ("primary", "serialy")}


def prepare(pipeline, args):
    # Refresh the retained checkpoint daily even while manual review is pending.
    pipeline.state["last_daily_preparation"] = timestamp()
    pipeline.save()
    client = OpenSubtitles(pipeline.root / "provider", os.environ.get("OPENSUBTITLES_API_KEY", ""),
                          os.environ.get("OPENSUBTITLES_USER_AGENT", "OlbrasoftSubtitleCoverage2026 v0.1"))
    uploads = uploads_by_account()
    reports = load_latest_status(REPO / "reports/subtitle-backfill-status.jsonl")
    metadata = {}
    with gzip.open(REPO / "backlog/series-episodes.jsonl.gz", "rt") as source:
        for line in source:
            row = json.loads(line)
            metadata.setdefault(int(row["series_id"]), {k: row.get(k) for k in ("series_original_title", "imdb_id")})
    tasks = []
    for account, items in uploads.items():
        for eid, upload in items.items():
            previous = matching_upload_status(reports.get(eid), upload)
            if previous.get("status") != "alternate_search_pending" or not previous.get("detail_url"):
                continue
            tasks.append((account, upload, previous))
    # Fair account interleaving, stable rotation past prior negative searches.
    tasks.sort(key=lambda task: (str(pipeline.state["episodes"].get(f"{task[0]}-{task[1]['prehrajto_video_id']}", {}).get("checked_at", "")),
                                 int(task[1]["episode_id"])))
    account_tasks = [[task for task in tasks if task[0] == account] for account in ("primary", "serialy")]
    tasks = [task for pair in zip_longest(*account_tasks) for task in pair if task is not None]
    selected, deadline = 0, time.monotonic() + args.max_runtime
    for account, upload, previous in tasks:
        if selected >= args.search_limit or time.monotonic() >= deadline:
            break
        if sum(row["status"] in PENDING for row in pipeline.state["candidates"].values()) >= args.max_pending:
            break
        video_id = int(upload["prehrajto_video_id"])
        episode_identity = f"{account}-{video_id}"
        existing = [r for r in pipeline.state["candidates"].values() if r["upload_account"] == account and r["target_video_id"] == video_id]
        if any(r["status"] not in {"rejected", "duration_mismatch"} for r in existing):
            continue
        prior = pipeline.state["episodes"].get(episode_identity, {})
        if prior.get("retry_after") and timestamp() < prior["retry_after"]:
            continue
        row = {**upload, **metadata.get(int(upload.get("series_id") or 0), {}),
               "upload_account": account, "target_video_id": video_id,
               "target_detail_url": previous["detail_url"], "video_duration_sec": previous.get("target_duration")}
        selected += 1
        try:
            page = requests.get(row["target_detail_url"], headers=GOOGLEBOT_HEADERS, timeout=25)
            page.raise_for_status()
            resolved = parse_html(page.text, row["target_detail_url"])
            if resolved.video_id != video_id or not resolved.duration_sec:
                raise ValueError("Target identity or duration unconfirmed")
            if pick_czech_track(resolved):
                outcome = "already_has_tracks"
            else:
                row["video_duration_sec"] = resolved.duration_sec
                found = client.search(row)
                outcome = "no_candidate"
                for subtitle in found[:2]:
                    file_id = int(subtitle["files"][0]["file_id"])
                    item = {k: row.get(k) for k in ("episode_id", "series_id", "series_title", "series_original_title", "imdb_id", "season", "episode", "episode_code", "display_name", "upload_account", "target_video_id", "target_detail_url", "video_duration_sec")}
                    item.update(file_id=file_id, subtitle_url=subtitle.get("url"), feature_details=subtitle["feature_details"])
                    key = candidate_key(item)
                    if key in pipeline.state["candidates"]:
                        continue
                    path, digest = client.download(file_id)
                    cues = parse_cues(path, digest)
                    gap = resolved.duration_sec - max(cue["end"] for cue in cues)
                    item.update(subtitle_sha256=digest, end_gap_sec=round(gap, 3),
                                status="awaiting_whisper" if -3 <= gap <= 60 and len(cues) >= 20 else "duration_mismatch", prepared_at=timestamp())
                    pipeline.state["candidates"][key] = item
                    pipeline.save()
                    outcome = item["status"]
                    if item["status"] == "awaiting_whisper":
                        break
            pipeline.state["episodes"][episode_identity] = {"status": outcome, "checked_at": timestamp(),
                "retry_after": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat()}
        except ProviderStopped as error:
            print(f"preparation_paused reason={error}")
            break
        except Exception as error:
            pipeline.state["episodes"][episode_identity] = {"status": "temporary_failure", "checked_at": timestamp(), "error_type": type(error).__name__,
                "retry_after": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=6)).isoformat()}
        pipeline.save()
        time.sleep(1)
    pipeline.export()
    print(f"searched={selected} download_allowance={client.allowance()}")


def whisper(pipeline, args):
    from faster_whisper import WhisperModel
    rows = [r for r in pipeline.state["candidates"].values() if r["status"] == "awaiting_whisper"][:args.limit]
    if not rows:
        return
    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=6)
    (pipeline.root / "audio").mkdir(exist_ok=True)
    for row in rows:
        candidate = {**row, "local_subtitle_file": str(pipeline.file(row))}
        evidence = collect(candidate, model, argparse.Namespace(model=args.model, device="cpu", clip_seconds=30, extract_timeout=120), pipeline.root)
        # Keep transcriptions/times but not hundreds of MB of WAV in artifacts.
        for clip in evidence["clips"]:
            clip.pop("local_audio", None)
        row["evidence"] = evidence
        row["status"] = "awaiting_manual_review" if evidence["status"] == "awaiting_manual_review" else "whisper_incomplete"
        if sum(c.get("detected_language") == "cs" and c.get("language_probability", 0) >= .9 for c in evidence["clips"]) >= 2:
            row["status"] = "czech_audio_detected"
        pipeline.save()
    pipeline.export()


def credentials(account):
    prefix = "UPLOAD_PREHRAJTO" if account == "primary" else "SERIALY_PREHRAJTO"
    email, password = os.environ.get(prefix + "_EMAIL"), os.environ.get(prefix + "_PASSWORD")
    if not email or not password:
        raise ValueError(f"Missing {account} upload credentials")
    with contextlib.redirect_stdout(io.StringIO()):
        return login(email, password)


def verification(pipeline):
    for row in pipeline.state["candidates"].values():
        if row["status"] not in SUBMITTED:
            continue
        try:
            proof = verify(row["target_detail_url"], row["target_video_id"], transport_srt(pipeline.file(row)))
            row.update(status="uploaded", server_proof=proof, verified_at=timestamp())
        except Exception as error:
            row["verification_error_type"] = type(error).__name__
        pipeline.save()


def plan_upload(pipeline, args):
    pipeline.record_reviews(args.reviews)
    verification(pipeline)
    uploads, decisions, sessions, planned = uploads_by_account(), pipeline.decisions(args.reviews), {}, []
    active_videos = {(r["upload_account"], r["target_video_id"]) for r in pipeline.state["candidates"].values() if r["status"] in SUBMITTED | {"uploaded"}}
    for key, row in pipeline.state["candidates"].items():
        if len(planned) >= args.limit or row["status"] != "approved":
            continue
        account, video = row["upload_account"], row["target_video_id"]
        if (account, video) in active_videos:
            continue
        current = uploads[account].get(int(row["episode_id"]))
        if not current or int(current.get("prehrajto_video_id") or 0) != video:
            row["status"] = "target_replaced"
            continue
        if not approval(row, decisions.get(key)):
            raise ValueError("Missing exact manual approval")
        path = pipeline.file(row)
        parse_cues(path, row["subtitle_sha256"])
        resolved = target(row)
        if pick_czech_track(resolved):
            row["status"] = "already_has_tracks"
            continue
        if account not in sessions:
            sessions[account] = credentials(account)
        info = find_profile_detail(sessions[account], current)
        if not info:
            row["status"] = "target_not_on_account"
            continue
        if info.get("deleted"):
            row["status"] = "target_deleted"
            continue
        if info.get("processing") or info.get("subtitle_count", 0):
            continue
        # Mark BEFORE POST. The workflow persists this exact manifest in a
        # checkpoint artifact before it starts the submit command.
        row.update(status="submission_pending", profile_page=int(info["page"]), planned_at=timestamp())
        planned.append(key)
        active_videos.add((account, video))
    pipeline.save()
    write_json(pipeline.root / "upload-plan.json", {"candidates": planned, "created_at": timestamp()})
    pipeline.export()
    print(f"planned_uploads={len(planned)}")


def submit(pipeline, args):
    plan = json.loads((pipeline.root / "upload-plan.json").read_text())
    # GitHub uses a durable artifact, local runs require an explicit durable
    # checkpoint file copied before invoking submit.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        artifact_id = os.environ.get("CHECKPOINT_ARTIFACT_ID")
        if not artifact_id:
            raise ValueError("No durable checkpoint artifact")
        repo = os.environ["GITHUB_REPOSITORY"]
        artifact = json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/actions/artifacts/{artifact_id}"], text=True))
        if (artifact.get("expired") or artifact.get("workflow_run", {}).get("id") != int(os.environ["GITHUB_RUN_ID"])
                or not artifact.get("name", "").startswith(ARTIFACT_PREFIX)):
            raise ValueError("Invalid durable checkpoint artifact")
    elif not args.checkpoint or not args.checkpoint.exists() or args.checkpoint.read_bytes() != pipeline.path.read_bytes():
        raise ValueError("Local submission requires an exact durable checkpoint copy")
    decisions, uploads, sessions = pipeline.decisions(args.reviews), uploads_by_account(), {}
    for key in plan["candidates"]:
        row = pipeline.state["candidates"][key]
        if row["status"] != "submission_pending":
            continue
        account, video = row["upload_account"], row["target_video_id"]
        current = uploads[account].get(int(row["episode_id"]))
        if not current or int(current.get("prehrajto_video_id") or 0) != video or not approval(row, decisions.get(key)):
            raise ValueError("Target or approval changed since checkpoint")
        content = transport_srt(pipeline.file(row))
        if pick_czech_track(target(row)):
            row["status"] = "already_has_tracks"
            pipeline.save()
            continue
        if account not in sessions:
            sessions[account] = credentials(account)
        # Reconfirm ownership and pending subtitle counts immediately before POST.
        info = find_profile_detail(sessions[account], current)
        if not info or info.get("deleted") or info.get("processing") or info.get("subtitle_count", 0):
            continue
        try:
            response = upload_subtitle(sessions[account], video, int(info["page"]), content, str(int(time.time()))[-6:])
            row.update(status="submitted" if response.status_code == 200 else "submission_unknown", http_status=response.status_code, submitted_at=timestamp())
        except requests.RequestException as error:
            row.update(status="submission_unknown", error_type=type(error).__name__)
        pipeline.save()
        print(f"video={video} account={account} status={row['status']}")
    verification(pipeline)
    pipeline.export()


def seed(pipeline, args):
    """Recover the tested pilot file choices; approval still needs fresh evidence."""
    client = OpenSubtitles(pipeline.root / "provider", os.environ.get("OPENSUBTITLES_API_KEY", ""),
                          os.environ.get("OPENSUBTITLES_USER_AGENT", "OlbrasoftSubtitleCoverage2026 v0.1"))
    uploads = uploads_by_account()
    for row in json.loads(args.seed_file.read_text()):
        key = candidate_key(row)
        if key in pipeline.state["candidates"]:
            continue
        current = uploads[row["upload_account"]].get(int(row["episode_id"]))
        if not current or int(current.get("prehrajto_video_id") or 0) != row["target_video_id"]:
            continue
        if pick_czech_track(target(row)):
            continue
        found = client.search(row)
        if not any(int(s["files"][0]["file_id"]) == row["file_id"] for s in found):
            raise ValueError("Seed subtitle identity no longer confirmed")
        path, digest = client.download(row["file_id"])
        if digest != row["subtitle_sha256"]:
            raise ValueError("Seed subtitle bytes changed")
        parse_cues(path, digest)
        pipeline.state["candidates"][key] = {**row, "display_name": current["display_name"], "status": "awaiting_whisper", "prepared_at": timestamp()}
        pipeline.save()
    pipeline.export()


def publish_proofs(pipeline, repo):
    """Append verified results through the contents API with optimistic retries."""
    pending = [row for row in pipeline.state["candidates"].values()
               if row["status"] == "uploaded" and not row.get("report_published")]
    if not pending:
        return
    endpoint = f"repos/{repo}/contents/reports/opensubtitles-verified.jsonl"
    for _ in range(6):
        response = subprocess.run(["gh", "api", endpoint + "?ref=main"], capture_output=True, text=True)
        sha, text = None, ""
        if response.returncode == 0:
            data = json.loads(response.stdout)
            sha = data["sha"]
            text = (base64.b64decode(data["content"]).decode() if data.get("encoding") == "base64" else
                    subprocess.check_output(["gh", "api", endpoint + "?ref=main", "-H", "Accept: application/vnd.github.raw+json"], text=True))
        elif "404" not in response.stderr:
            raise RuntimeError("Cannot read verified subtitle ledger")
        records = {(r["upload_account"], int(r["prehrajto_video_id"])): r
                   for r in (json.loads(line) for line in text.splitlines() if line.strip())}
        original = dict(records)
        for row in pending:
            proof = row["server_proof"]
            if not proof.get("matches_prepared_file") or proof.get("cue_count", 0) <= 0:
                raise ValueError("No exact server delivery proof")
            records[(row["upload_account"], row["target_video_id"])] = {
                "episode_id": row["episode_id"], "upload_account": row["upload_account"],
                "prehrajto_video_id": row["target_video_id"], "status": "uploaded",
                "source_lane": "external", "checked_at": proof["checked_at"],
                "detail_url": row["target_detail_url"], "subtitle_language": "cs",
                "srt_sha256": proof["srt_sha256"], "cue_count": proof["cue_count"],
            }
        if records != original:
            content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for _, row in sorted(records.items()))
            payload = {"message": "chore: record verified external subtitle attachments", "branch": "main",
                       "content": base64.b64encode(content.encode()).decode()}
            if sha:
                payload["sha"] = sha
            result = subprocess.run(["gh", "api", "--method", "PUT", endpoint, "--input", "-"],
                                    input=json.dumps(payload), capture_output=True, text=True)
            if result.returncode:
                if "409" in result.stderr or "422" in result.stderr:
                    time.sleep(2)
                    continue
                raise RuntimeError("Cannot publish verified subtitle ledger")
        for row in pending:
            row["report_published"] = True
        pipeline.save()
        return
    raise RuntimeError("Verified subtitle ledger changed too often")


def restore(pipeline, repo):
    workflow = json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/actions/workflows/{WORKFLOW}"], text=True))
    artifacts = []
    # Snapshots are created only when state changes and retained for seven days.
    for name in (ARTIFACT_PREFIX + "checkpoint", ARTIFACT_PREFIX + "final"):
        data = json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/actions/artifacts?name={name}&per_page=10"], text=True))
        for artifact in data["artifacts"]:
            if artifact["expired"] or artifact.get("workflow_run", {}).get("head_branch") != "main":
                continue
            run = json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/actions/runs/{artifact['workflow_run']['id']}"], text=True))
            if run["workflow_id"] == workflow["id"]:
                artifacts.append(artifact)
    if artifacts:
        selected = max(artifacts, key=lambda a: a["created_at"])
        subprocess.run(["gh", "run", "download", str(selected["workflow_run"]["id"]), "--repo", repo,
                        "--name", selected["name"], "--dir", str(pipeline.root)], check=True)
        print(f"restored_artifact={selected['id']}")


def import_pilot(pipeline, directory, reviews):
    candidates = json.loads((directory / "duration-qualified-candidates.json").read_text())
    raw = json.loads((directory / "audio-review/whisper-evidence.json").read_text())
    manual = json.loads((directory / "manual-review-results.json").read_text())
    decisions, files = [], {}
    for row in candidates:
        evidence = next(e for e in raw if int(e["episode_id"]) == int(row["episode_id"]))
        evidence.pop("manual_review", None)
        # Artifact paths must not carry user-local audio locations.
        for clip in evidence["clips"]:
            clip.pop("local_audio", None)
        review = next(r for r in manual if int(r["episode_id"]) == int(row["episode_id"]))
        if review["status"] == "sampled_content_and_timing_verified":
            apply_manual_review(evidence, review)
        content = Path(row["local_subtitle_file"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != row["subtitle_sha256"]:
            raise ValueError("Pilot file changed")
        dest = pipeline.root / "provider" / f"{row['file_id']}.srt"
        dest.parent.mkdir(exist_ok=True)
        dest.write_bytes(content)
        files[str(row["file_id"])] = {"sha256": row["subtitle_sha256"], "downloaded_at": timestamp()}
        item = {**row, "evidence": evidence, "status": "awaiting_manual_review"}
        item.pop("local_subtitle_file", None)
        if sum(c.get("detected_language") == "cs" and c.get("language_probability", 0) >= .9 for c in evidence["clips"]) >= 2:
            item["status"] = "czech_audio_detected"
        pipeline.state["candidates"][candidate_key(item)] = item
        decisions.append({**review, "upload_account": row["upload_account"], "evidence_sha256": evidence_digest(evidence)})
    reviews.parent.mkdir(parents=True, exist_ok=True)
    reviews.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in decisions))
    quota = json.loads((directory / "quota-live-proof.json").read_text())
    write_json(pipeline.root / "provider/provider.json", {"files": files, "quota": quota})
    pipeline.record_reviews(reviews)
    pipeline.export()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore", "prepare", "whisper", "apply-reviews", "plan-upload", "submit", "verify", "status", "import-pilot", "seed", "checkpoint", "needs-save", "publish-proofs"))
    parser.add_argument("--directory", type=Path, default=REPO / ".cache/external-subtitles")
    parser.add_argument("--reviews", type=Path, default=REPO / "plans/opensubtitles-reviews.jsonl")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--search-limit", type=int, default=300)
    parser.add_argument("--max-pending", type=int, default=90)
    parser.add_argument("--max-runtime", type=int, default=3600)
    parser.add_argument("--model", default="small")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--pilot-directory", type=Path)
    parser.add_argument("--seed-file", type=Path, default=REPO / "plans/opensubtitles-seed.json")
    args = parser.parse_args()
    os.umask(0o077)
    pipeline = Pipeline(args.directory)
    if args.command == "restore":
        restore(pipeline, args.repo)
        # Reload after downloading; the initial object predates the snapshot.
        pipeline = Pipeline(args.directory)
        (pipeline.root / ".initial-fingerprint").write_text(pipeline.fingerprint())
    elif args.command == "prepare":
        prepare(pipeline, args)
    elif args.command == "whisper":
        whisper(pipeline, args)
    elif args.command == "apply-reviews":
        pipeline.record_reviews(args.reviews)
        pipeline.export()
    elif args.command == "plan-upload":
        plan_upload(pipeline, args)
        if os.environ.get("GITHUB_OUTPUT"):
            plan = json.loads((pipeline.root / "upload-plan.json").read_text())
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write(f"has_uploads={'true' if plan['candidates'] else 'false'}\n")
    elif args.command == "submit":
        submit(pipeline, args)
    elif args.command == "verify":
        verification(pipeline)
        pipeline.export()
    elif args.command == "import-pilot":
        import_pilot(pipeline, args.pilot_directory, args.reviews)
    elif args.command == "seed":
        seed(pipeline, args)
    elif args.command == "publish-proofs":
        publish_proofs(pipeline, args.repo)
    elif args.command == "checkpoint":
        if not args.checkpoint:
            parser.error("--checkpoint is required")
        shutil.copyfile(pipeline.path, args.checkpoint)
        with args.checkpoint.open("rb") as handle:
            os.fsync(handle.fileno())
    elif args.command == "needs-save":
        initial = pipeline.root / ".initial-fingerprint"
        changed = not initial.exists() or initial.read_text() != pipeline.fingerprint()
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write(f"changed={'true' if changed else 'false'}\n")
        print(f"state_changed={changed}")
    else:
        pipeline.export()
        print((pipeline.root / "summary.json").read_text())


if __name__ == "__main__":
    main()
