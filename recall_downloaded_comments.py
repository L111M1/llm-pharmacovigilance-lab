"""Recall downloaded comment bodies with the same drug rules as post recall.

This is local-only: it never downloads data or calls the model. Raw comment
threads stay untouched. The selected comment IDs are consumed by
pharmacovigilance_pipeline.py when present beside recalled_posts.jsonl.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from download_reddit_data import (
    FUZZY_MIN_ALIAS_LENGTH,
    FUZZY_SIMILARITY_THRESHOLD,
    compile_recall_patterns,
    find_drug_matches,
    load_drug_aliases,
    targeted_output_lock,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir", required=True, type=Path,
        help="Completed targeted_comments directory with recalled_posts.jsonl.",
    )
    parser.add_argument(
        "--drug-alias-file", required=True, type=Path,
        help="The same alias JSON used for the original post recall.",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON in {path}:{number}: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"Expected JSON object in {path}:{number}")
            yield item


def recall_comments(input_dir: Path, alias_file: Path) -> dict[str, Any]:
    input_dir = input_dir.resolve()
    post_manifest = input_dir / "recalled_posts.jsonl"
    post_summary = input_dir / "recall_summary.json"
    if not post_manifest.is_file() or not post_summary.is_file():
        raise FileNotFoundError(
            "Input must contain recalled_posts.jsonl and recall_summary.json"
        )
    aliases = load_drug_aliases(alias_file.resolve())
    summary = json.loads(post_summary.read_text(encoding="utf-8"))
    if summary.get("parameters", {}).get("drug_aliases") != aliases:
        raise ValueError(
            "Alias file differs from saved post recall; use the original alias file"
        )
    patterns = compile_recall_patterns(aliases)
    exact_terms = frozenset(
        name.casefold()
        for drug, names in aliases.items()
        for name in names + [drug]
    )
    fuzzy_aliases = tuple(
        (drug, name.casefold())
        for drug, names in aliases.items()
        for name in set(names + [drug])
        if len(name.casefold()) >= FUZZY_MIN_ALIAS_LENGTH
        and name.casefold().isalpha()
    )
    fuzzy_cache: dict[str, tuple[dict[str, Any], ...]] = {}
    saved_post_count = int(
        summary.get("results", {}).get("selected_posts_written_to_manifest", -1)
    )
    post_count = 0
    comment_count = 0
    recalled_count = 0
    exact_count = 0
    fuzzy_count = 0
    only_fuzzy_count = 0
    by_subreddit: Counter[str] = Counter()
    by_drug: Counter[str] = Counter()
    seen_posts: set[str] = set()
    seen_comments: set[str] = set()
    manifest_path = input_dir / "recalled_comments.jsonl"
    temp_path = manifest_path.with_suffix(".jsonl.tmp")
    started = time.monotonic()

    with targeted_output_lock(input_dir):
        with temp_path.open("w", encoding="utf-8") as output:
            for post in read_jsonl(post_manifest):
                post_id = str(post.get("post_id", "")).strip()
                subreddit = str(post.get("subreddit", "")).strip()
                if not post_id or not subreddit or post_id in seen_posts:
                    raise ValueError(f"Invalid/duplicate post in manifest: {post_id!r}")
                seen_posts.add(post_id)
                post_count += 1
                comments_path = (
                    input_dir / subreddit / "comments"
                    / f"comments_for_{post_id}.jsonl"
                )
                state_path = comments_path.with_suffix(".jsonl.state.json")
                if not comments_path.is_file() or not state_path.is_file():
                    raise FileNotFoundError(
                        f"Missing completed comment thread: {comments_path}"
                    )
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if (
                    not state.get("complete")
                    or str(state.get("post_id")) != post_id
                    or str(state.get("subreddit", "")).casefold()
                    != subreddit.casefold()
                ):
                    raise RuntimeError(f"Comment thread not complete: {state_path}")
                thread_count = 0
                for comment in read_jsonl(comments_path):
                    thread_count += 1
                    comment_id = str(comment.get("id", "")).removeprefix("t1_").strip()
                    link_id = str(comment.get("link_id", "")).removeprefix("t3_").strip()
                    if not comment_id or comment_id in seen_comments:
                        raise ValueError(f"Invalid/duplicate comment ID: {comment_id!r}")
                    if link_id != post_id:
                        raise ValueError(
                            f"Comment {comment_id} belongs to {link_id}, not {post_id}"
                        )
                    seen_comments.add(comment_id)
                    comment_count += 1
                    matched, details = find_drug_matches(
                        str(comment.get("body") or ""),
                        aliases,
                        patterns,
                        exact_terms,
                        fuzzy_aliases,
                        fuzzy_cache,
                    )
                    if not matched:
                        continue
                    match_types = {item["match_type"] for item in details}
                    exact_count += "exact" in match_types
                    fuzzy_count += "fuzzy" in match_types
                    only_fuzzy_count += match_types == {"fuzzy"}
                    recalled_count += 1
                    by_subreddit[subreddit] += 1
                    by_drug.update(matched)
                    output.write(json.dumps({
                        "comment_id": comment_id,
                        "post_id": post_id,
                        "subreddit": subreddit,
                        "matched_target_drugs": list(matched),
                        "drug_match_details": list(details),
                    }, ensure_ascii=False) + "\n")
                if thread_count != int(state.get("count", -1)):
                    raise RuntimeError(
                        f"Comment count differs from state: {comments_path} "
                        f"({thread_count} vs {state.get('count')})"
                    )
                if post_count % 500 == 0 or post_count == saved_post_count:
                    print(
                        f"Recall comments: {post_count:,}/{saved_post_count:,} "
                        f"threads | {comment_count:,} comments scanned | "
                        f"{recalled_count:,} recalled | "
                        f"{time.monotonic() - started:.0f}s",
                        flush=True,
                    )
                if len(fuzzy_cache) >= 200_000:
                    fuzzy_cache.clear()
            output.flush()
            os.fsync(output.fileno())
        if post_count != saved_post_count:
            raise RuntimeError(
                f"Post manifest has {post_count:,} rows; summary expects "
                f"{saved_post_count:,}"
            )
        result = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "interpretation": (
                "Counts are text mentions, not confirmed personal drug use "
                "or adverse events. Comment recall checks the comment body "
                "only, not its parent or post context."
            ),
            "parameters": {
                "input_dir": str(input_dir),
                "post_manifest_sha256": sha256_file(post_manifest),
                "drug_aliases": aliases,
                "fuzzy_min_alias_length": FUZZY_MIN_ALIAS_LENGTH,
                "fuzzy_similarity_threshold": FUZZY_SIMILARITY_THRESHOLD,
            },
            "results": {
                "recalled_posts": post_count,
                "downloaded_comments_scanned": comment_count,
                "recalled_comments": recalled_count,
                "recalled_post_and_comment_records": post_count + recalled_count,
                "comment_recall_rate_percent": round(
                    100 * recalled_count / comment_count, 4
                ) if comment_count else 0.0,
                "comments_with_exact_match": exact_count,
                "comments_with_fuzzy_match": fuzzy_count,
                "comments_with_only_fuzzy_match": only_fuzzy_count,
            },
            "recalled_comments_by_subreddit": dict(sorted(by_subreddit.items())),
            "recalled_comments_by_target_drug": dict(sorted(by_drug.items())),
        }
        os.replace(temp_path, manifest_path)
        summary_path = input_dir / "combined_recall_summary.json"
        summary_tmp = summary_path.with_suffix(".json.tmp")
        with summary_tmp.open("w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(summary_tmp, summary_path)
    return result


def main() -> None:
    args = parse_args()
    result = recall_comments(args.input_dir, args.drug_alias_file)
    counts = result["results"]
    print(
        f"Done: {counts['recalled_posts']:,} recalled posts + "
        f"{counts['recalled_comments']:,} recalled comments = "
        f"{counts['recalled_post_and_comment_records']:,} candidate records."
    )
    print(f"Manifest: {args.input_dir.resolve() / 'recalled_comments.jsonl'}")
    print(f"Summary:  {args.input_dir.resolve() / 'combined_recall_summary.json'}")


if __name__ == "__main__":
    main()
