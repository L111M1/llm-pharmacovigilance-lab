"""Download subreddit posts and comments from the Arctic Shift API.

The downloader is intentionally separate from the DeepSeek analysis pipeline.
It downloads raw, public Reddit records in resumable yearly slices and shows
continuous terminal progress. Nothing is downloaded when ``--dry-run`` is used.

Default research window: [2020-09-22, 2026-09-22), in UTC.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import ssl
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import BadStatusLine, IncompleteRead, RemoteDisconnected
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE = "https://arctic-shift.photon-reddit.com/api"
DEFAULT_START_DATE = "2020-09-22"
DEFAULT_END_DATE = "2026-09-22"
DEFAULT_SUBREDDITS = [
    "diabetes",
    "diabetes_t2",
    "type2diabetes",
    "diabetesuk",
    "Heartfailure",
    "kidneydisease",
    "ChronicKidneyDisease",
    "IgANephropathy",
]
DEFAULT_DRUG_ALIASES = {
    "dapagliflozin": [
        "dapagliflozin",
        "dapaglifozin",
        "farxiga",
        "forxiga",
        "xigduo",
        "qtern",
    ],
    "empagliflozin": [
        "empagliflozin",
        "empaglifozin",
        "jardiance",
        "jardance",
        "synjardy",
        "glyxambi",
        "trijardy",
    ],
    "canagliflozin": [
        "canagliflozin",
        "canaglifozin",
        "invokana",
        "invokamet",
    ],
    "ertugliflozin": [
        "ertugliflozin",
        "ertuglifozin",
        "steglatro",
        "segluromet",
        "steglujan",
    ],
}
CLASS_TERMS = ["sglt2", "sglt-2", "sglt 2", "gliflozin", "gliflozins"]
FUZZY_SIMILARITY_THRESHOLD = 0.80
FUZZY_MIN_ALIAS_LENGTH = 7
WORD_PATTERN = re.compile(r"[a-z]+", re.IGNORECASE)
FIELDS = {
    "posts": [
        "id",
        "author",
        "created_utc",
        "retrieved_on",
        "subreddit",
        "title",
        "selftext",
        "score",
        "num_comments",
        "link_flair_text",
        "url",
    ],
    "comments": [
        "id",
        "author",
        "created_utc",
        "retrieved_on",
        "subreddit",
        "body",
        "link_id",
        "parent_id",
        "score",
    ],
}


@dataclass(frozen=True)
class DownloadJob:
    index: int
    total: int
    subreddit: str
    kind: str
    start_epoch: int
    end_epoch: int
    output_path: Path


@dataclass(frozen=True)
class RecalledPost:
    post_id: str
    subreddit: str
    created_utc: int
    num_comments: int
    matched_drugs: tuple[str, ...]
    match_details: tuple[dict[str, Any], ...]
    class_only: bool
    source_path: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download Reddit posts/comments from Arctic Shift with visible "
            "progress and resumable state files."
        )
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/reddit/raw"),
        help="Raw-data directory (default: data/reddit/raw).",
    )
    parser.add_argument(
        "--subreddits",
        nargs="+",
        default=DEFAULT_SUBREDDITS,
        help="Subreddit names without r/.",
    )
    parser.add_argument(
        "--start-date",
        default=DEFAULT_START_DATE,
        help="Inclusive UTC start date, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        default=DEFAULT_END_DATE,
        help="Exclusive UTC end date, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--kinds",
        nargs="+",
        choices=("posts", "comments"),
        default=["posts", "comments"],
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="API records per request, from 1 to 100 (default: 100).",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.8,
        help="Minimum delay between successful requests in seconds.",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--max-retries",
        type=int,
        default=0,
        help=(
            "Retries for transient errors; 0 retries indefinitely "
            "(default: 0)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the download plan without making network requests.",
    )
    parser.add_argument(
        "--targeted-comments-from-posts",
        type=Path,
        help=(
            "Scan downloaded post JSONL files below this directory, recall "
            "drug-related posts, and download only their comment threads."
        ),
    )
    parser.add_argument(
        "--drug-alias-file",
        type=Path,
        help=(
            "Optional JSON object mapping canonical drug names to aliases. "
            "The built-in SGLT2 alias map is used when omitted."
        ),
    )
    parser.add_argument(
        "--include-class-only",
        action="store_true",
        help="Also recall posts that mention only SGLT2/gliflozin class terms.",
    )
    parser.add_argument(
        "--max-recalled-posts",
        type=int,
        help="Optional cap for a targeted-comment test run.",
    )
    return parser.parse_args()


def parse_utc_date(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc
    return parsed.replace(tzinfo=timezone.utc)


def format_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def date_label(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def yearly_slices(start: datetime, end: datetime) -> list[tuple[int, int]]:
    if start >= end:
        raise ValueError("--start-date must be earlier than --end-date")
    slices = []
    cursor = start
    while cursor < end:
        next_year = datetime(cursor.year + 1, 1, 1, tzinfo=timezone.utc)
        slice_end = min(next_year, end)
        slices.append((int(cursor.timestamp()), int(slice_end.timestamp())))
        cursor = slice_end
    return slices


def normalize_subreddits(values: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for value in values:
        name = value.strip()
        if name.lower().startswith("r/"):
            name = name[2:]
        if not name:
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(name)
    if not normalized:
        raise ValueError("At least one subreddit is required")
    return normalized


def build_jobs(args: argparse.Namespace) -> list[DownloadJob]:
    start = parse_utc_date(args.start_date)
    end = parse_utc_date(args.end_date)
    slices = yearly_slices(start, end)
    specs = []
    for subreddit in normalize_subreddits(args.subreddits):
        for kind in args.kinds:
            for slice_start, slice_end in slices:
                filename = (
                    f"{kind}_{date_label(slice_start)}_"
                    f"{date_label(slice_end)}.jsonl"
                )
                output_path = (
                    args.output_dir.resolve()
                    / subreddit
                    / kind
                    / filename
                )
                specs.append(
                    (subreddit, kind, slice_start, slice_end, output_path)
                )
    total = len(specs)
    return [
        DownloadJob(index + 1, total, *spec)
        for index, spec in enumerate(specs)
    ]


def state_path_for(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".state.json")


def load_state(job: DownloadJob) -> dict[str, Any]:
    state_path = state_path_for(job.output_path)
    if not state_path.exists():
        if job.output_path.exists() and job.output_path.stat().st_size > 0:
            raise RuntimeError(
                f"Found data without a resume state: {job.output_path}. "
                "Move it aside or restore its matching .state.json file."
            )
        return {
            "cursor": job.start_epoch,
            "count": 0,
            "pages": 0,
            "complete": False,
        }
    with state_path.open("r", encoding="utf-8") as stream:
        state = json.load(stream)
    if (
        state.get("subreddit") != job.subreddit
        or state.get("kind") != job.kind
        or state.get("start_epoch") != job.start_epoch
        or state.get("end_epoch") != job.end_epoch
    ):
        raise RuntimeError(
            f"State parameters do not match the current job: {state_path}"
        )
    if int(state.get("count", 0)) > 0 and (
        not job.output_path.exists() or job.output_path.stat().st_size == 0
    ):
        raise RuntimeError(
            f"Resume state refers to missing data: {state_path}. "
            "Restore the JSONL file or move the state file aside."
        )
    return state


def save_state(job: DownloadJob, state: dict[str, Any]) -> None:
    state_path = state_path_for(job.output_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "subreddit": job.subreddit,
        "kind": job.kind,
        "start_epoch": job.start_epoch,
        "end_epoch": job.end_epoch,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **state,
    }
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, state_path)


def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("data", [])
    elif isinstance(payload, list):
        items = payload
    else:
        raise TypeError("API response must be a JSON object or array")
    if not isinstance(items, list):
        raise TypeError("API response field 'data' must be an array")
    return [item for item in items if isinstance(item, dict)]


def retry_wait_seconds(error: HTTPError, attempt: int) -> float:
    reset = error.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(float(reset), 1.0)
        except ValueError:
            pass
    retry_after = error.headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), 1.0)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                return max(
                    retry_at.timestamp() - datetime.now(timezone.utc).timestamp(),
                    1.0,
                )
            except (TypeError, ValueError):
                pass
    return min(2 ** min(attempt, 6) + random.random(), 60.0)


def visible_wait(seconds: float, reason: str) -> None:
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(" " * 100, end="\r", flush=True)
            return
        print(
            f"Waiting {remaining:5.0f}s before retry: {reason[:60]}",
            end="\r",
            flush=True,
        )
        time.sleep(min(1.0, remaining))


def request_items(
    endpoint: str,
    params: dict[str, Any],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    url = f"{endpoint}?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "reddit-pharmacovigilance-research/1.0",
        },
    )
    attempt = 0
    while True:
        try:
            with urlopen(request, timeout=args.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return extract_items(payload)
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable:
                raise
            attempt += 1
            if args.max_retries and attempt > args.max_retries:
                raise
            wait = retry_wait_seconds(exc, attempt - 1)
            visible_wait(wait, f"HTTP {exc.code}")
        except (
            URLError,
            TimeoutError,
            ConnectionError,
            RemoteDisconnected,
            BadStatusLine,
            IncompleteRead,
            ssl.SSLError,
            json.JSONDecodeError,
        ) as exc:
            attempt += 1
            if args.max_retries and attempt > args.max_retries:
                raise
            wait = min(
                2 ** min(attempt - 1, 6) + random.random(),
                60.0,
            )
            visible_wait(wait, type(exc).__name__)


def request_page(
    job: DownloadJob,
    cursor: int,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    endpoint = f"{API_BASE}/{job.kind}/search"
    params = {
        "subreddit": job.subreddit,
        "after": cursor,
        "before": job.end_epoch,
        "sort": "asc",
        "limit": args.page_size,
        "fields": ",".join(FIELDS[job.kind]),
    }
    return request_items(endpoint, params, args)


def created_epoch(item: dict[str, Any]) -> int | None:
    try:
        return int(float(item["created_utc"]))
    except (KeyError, TypeError, ValueError):
        return None


def load_recent_ids(path: Path, limit: int) -> set[str]:
    """Read only a rolling tail of IDs to make page-level resume idempotent."""
    recent: deque[str] = deque(maxlen=limit)
    if not path.exists() or path.stat().st_size == 0:
        return set()
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_id = str(item.get("id", "")).strip()
            if item_id:
                recent.append(item_id)
    return set(recent)


def print_progress(
    job: DownloadJob,
    state: dict[str, Any],
    started_at: float,
    initial_count: int,
) -> None:
    cursor = min(int(state["cursor"]), job.end_epoch)
    duration = max(job.end_epoch - job.start_epoch, 1)
    percent = 100 * (cursor - job.start_epoch) / duration
    elapsed = max(time.monotonic() - started_at, 0.001)
    session_records = max(int(state["count"]) - initial_count, 0)
    rate = session_records / elapsed
    print(
        f"[{job.index:03d}/{job.total:03d}] "
        f"r/{job.subreddit:<22} {job.kind:<8} "
        f"{percent:6.2f}% | {int(state['count']):>9,} records | "
        f"{rate:7.1f} rec/s | {format_epoch(cursor)}",
        end="\r",
        flush=True,
    )


def download_job(job: DownloadJob, args: argparse.Namespace) -> None:
    state = load_state(job)
    if state.get("complete") and job.output_path.exists():
        print(
            f"[{job.index:03d}/{job.total:03d}] SKIP complete: "
            f"r/{job.subreddit} {job.kind} "
            f"{date_label(job.start_epoch)}..{date_label(job.end_epoch)}"
        )
        return

    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    job.output_path.touch(exist_ok=True)
    started_at = time.monotonic()
    initial_count = int(state.get("count", 0))
    recent_ids = load_recent_ids(job.output_path, args.page_size * 2)
    cursor = max(int(state.get("cursor", job.start_epoch)), job.start_epoch)
    state.update(complete=False, cursor=cursor)
    print_progress(job, state, started_at, initial_count)

    while cursor < job.end_epoch:
        items = request_page(job, cursor, args)
        valid = []
        page_epochs = []
        for item in items:
            epoch = created_epoch(item)
            if epoch is None:
                continue
            if job.start_epoch <= epoch < job.end_epoch:
                page_epochs.append(epoch)
                item_id = str(item.get("id", "")).strip()
                if item_id and item_id in recent_ids:
                    continue
                valid.append(item)

        if not items:
            cursor = job.end_epoch
        elif not page_epochs:
            raise RuntimeError(
                "API returned records without usable created_utc values"
            )
        else:
            next_cursor = max(page_epochs) + 1
            if next_cursor <= cursor:
                raise RuntimeError(
                    f"Pagination did not advance beyond {format_epoch(cursor)}"
                )
            cursor = min(next_cursor, job.end_epoch)

        if valid:
            with job.output_path.open("a", encoding="utf-8") as stream:
                for item in valid:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        recent_ids = {
            str(item.get("id", "")).strip()
            for item in items
            if str(item.get("id", "")).strip()
        }

        state["cursor"] = cursor
        state["count"] = int(state.get("count", 0)) + len(valid)
        state["pages"] = int(state.get("pages", 0)) + 1
        state["complete"] = cursor >= job.end_epoch
        save_state(job, state)
        print_progress(job, state, started_at, initial_count)
        if cursor < job.end_epoch and args.request_delay:
            time.sleep(args.request_delay)

    print()


def print_plan(jobs: list[DownloadJob]) -> None:
    print(f"Planned jobs: {len(jobs)}")
    for job in jobs:
        print(
            f"[{job.index:03d}/{job.total:03d}] "
            f"r/{job.subreddit} {job.kind} "
            f"{date_label(job.start_epoch)}..{date_label(job.end_epoch)} "
            f"-> {job.output_path}"
        )


def load_drug_aliases(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return DEFAULT_DRUG_ALIASES
    with path.resolve().open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("--drug-alias-file must contain a non-empty JSON object")
    aliases = {}
    for canonical, raw_aliases in payload.items():
        if not isinstance(canonical, str) or not isinstance(raw_aliases, list):
            raise ValueError(
                "Drug alias JSON must map each canonical name to an alias list"
            )
        values = [
            str(value).strip()
            for value in raw_aliases
            if str(value).strip()
        ]
        canonical = canonical.strip().casefold()
        if canonical and values:
            aliases[canonical] = values
    if not aliases:
        raise ValueError("Drug alias JSON does not contain usable aliases")
    return aliases


def compile_recall_patterns(
    aliases: dict[str, list[str]],
) -> dict[str, re.Pattern[str]]:
    patterns = {}
    for drug, names in aliases.items():
        alternatives = sorted(
            {name.casefold() for name in names + [drug]},
            key=len,
            reverse=True,
        )
        patterns[drug] = re.compile(
            r"(?<![a-z0-9])(?:"
            + "|".join(re.escape(name) for name in alternatives)
            + r")(?![a-z0-9])",
            re.IGNORECASE,
        )
    return patterns


def damerau_levenshtein_distance(left: str, right: str) -> int:
    """Return edit distance, counting adjacent transposition as one edit."""
    left = left.casefold()
    right = right.casefold()
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    rows = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for index in range(len(left) + 1):
        rows[index][0] = index
    for index in range(len(right) + 1):
        rows[0][index] = index

    for left_index in range(1, len(left) + 1):
        for right_index in range(1, len(right) + 1):
            substitution_cost = int(
                left[left_index - 1] != right[right_index - 1]
            )
            rows[left_index][right_index] = min(
                rows[left_index - 1][right_index] + 1,
                rows[left_index][right_index - 1] + 1,
                rows[left_index - 1][right_index - 1] + substitution_cost,
            )
            if (
                left_index > 1
                and right_index > 1
                and left[left_index - 1] == right[right_index - 2]
                and left[left_index - 2] == right[right_index - 1]
            ):
                rows[left_index][right_index] = min(
                    rows[left_index][right_index],
                    rows[left_index - 2][right_index - 2] + 1,
                )
    return rows[-1][-1]


def normalized_similarity(left: str, right: str) -> tuple[float, int]:
    """Return normalized similarity and its underlying edit distance."""
    distance = damerau_levenshtein_distance(left, right)
    longest = max(len(left), len(right))
    similarity = 1.0 if longest == 0 else 1.0 - distance / longest
    return similarity, distance


def find_drug_matches(
    text: str,
    aliases: dict[str, list[str]],
    patterns: dict[str, re.Pattern[str]],
    exact_terms: frozenset[str] | None = None,
    fuzzy_aliases: tuple[tuple[str, str], ...] | None = None,
    fuzzy_cache: dict[str, tuple[dict[str, Any], ...]] | None = None,
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    """Find exact aliases first, then controlled fuzzy token matches."""
    details: dict[str, dict[str, Any]] = {}
    if exact_terms is None:
        exact_terms = frozenset(
            name.casefold()
            for drug, names in aliases.items()
            for name in names + [drug]
        )

    for drug, pattern in patterns.items():
        match = pattern.search(text)
        if match is None:
            continue
        matched_text = match.group(0)
        details[drug] = {
            "canonical_drug": drug,
            "matched_text": matched_text,
            "matched_alias": matched_text.casefold(),
            "match_type": "exact",
            "edit_distance": 0,
            "similarity": 1.0,
        }

    if fuzzy_aliases is None:
        fuzzy_aliases = tuple(
            (drug, name.casefold())
            for drug, names in aliases.items()
            for name in set(names + [drug])
            if len(name.casefold()) >= FUZZY_MIN_ALIAS_LENGTH
            and name.casefold().isalpha()
        )
    if fuzzy_cache is None:
        fuzzy_cache = {}
    tokens = {
        match.group(0).casefold()
        for match in WORD_PATTERN.finditer(text)
        if len(match.group(0)) >= FUZZY_MIN_ALIAS_LENGTH
    }
    fuzzy_by_drug: dict[str, dict[str, Any]] = {}
    for token in tokens:
        if token in exact_terms:
            continue
        cached = fuzzy_cache.get(token)
        if cached is None:
            candidates: list[dict[str, Any]] = []
            for drug, alias in fuzzy_aliases:
                longest = max(len(token), len(alias))
                minimum_distance = abs(len(token) - len(alias))
                if 1.0 - minimum_distance / longest < FUZZY_SIMILARITY_THRESHOLD:
                    continue
                similarity, distance = normalized_similarity(token, alias)
                if similarity + 1e-12 < FUZZY_SIMILARITY_THRESHOLD:
                    continue
                candidates.append(
                    {
                        "canonical_drug": drug,
                        "matched_text": token,
                        "matched_alias": alias,
                        "match_type": "fuzzy",
                        "edit_distance": distance,
                        "similarity": round(similarity, 4),
                    }
                )
            cached = tuple(candidates)
            fuzzy_cache[token] = cached
        candidates = [
            item for item in cached
            if item["canonical_drug"] not in details
        ]
        if not candidates:
            continue
        best_similarity = max(item["similarity"] for item in candidates)
        best = [
            item for item in candidates
            if item["similarity"] == best_similarity
        ]
        if len({item["canonical_drug"] for item in best}) != 1:
            continue
        candidate = min(
            best,
            key=lambda item: (item["edit_distance"], item["matched_alias"]),
        )
        drug = candidate["canonical_drug"]
        current = fuzzy_by_drug.get(drug)
        if current is None or (
            candidate["similarity"], -candidate["edit_distance"]
        ) > (current["similarity"], -current["edit_distance"]):
            fuzzy_by_drug[drug] = candidate

    details.update(fuzzy_by_drug)
    ordered_details = tuple(details[drug] for drug in sorted(details))
    return tuple(item["canonical_drug"] for item in ordered_details), ordered_details


def print_recall_scan_progress(
    processed_bytes: int,
    total_bytes: int,
    rows_read: int,
    recalled_count: int,
    current_file: Path,
    final: bool = False,
) -> None:
    """Render an in-place progress bar while scanning local post files."""
    fraction = min(processed_bytes / max(total_bytes, 1), 1.0)
    width = 28
    completed = int(width * fraction)
    bar = "#" * completed + "-" * (width - completed)
    try:
        label = f"{current_file.parent.parent.name}/{current_file.name}"
    except IndexError:
        label = current_file.name
    if len(label) > 48:
        label = "..." + label[-45:]
    message = (
        f"Recall [{bar}] {fraction * 100:6.2f}% | "
        f"{rows_read:>9,} rows | {recalled_count:>6,} recalled | {label}"
    )
    print(message.ljust(125), end="\n" if final else "\r", flush=True)


def recall_posts(
    posts_root: Path,
    subreddits: list[str],
    start_epoch: int,
    end_epoch: int,
    aliases: dict[str, list[str]],
    include_class_only: bool,
) -> tuple[list[RecalledPost], dict[str, int]]:
    posts_root = posts_root.resolve()
    if not posts_root.exists():
        raise FileNotFoundError(posts_root)
    allowed = {name.casefold() for name in subreddits}
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
    class_pattern = re.compile(
        r"(?<![a-z0-9])(?:"
        + "|".join(re.escape(term) for term in CLASS_TERMS)
        + r")(?![a-z0-9])",
        re.IGNORECASE,
    )
    recalled: dict[str, RecalledPost] = {}
    rows_read = 0
    in_scope_ids: set[str] = set()
    post_files = sorted(
        path
        for path in posts_root.rglob("*.jsonl")
        if path.parent.name.casefold() == "posts"
    )
    if not post_files:
        raise FileNotFoundError(f"No post JSONL files found below {posts_root}")

    total_bytes = sum(path.stat().st_size for path in post_files)
    processed_bytes = 0
    last_progress_update = 0.0

    for path in post_files:
        with path.open("rb") as stream:
            for line_number, line in enumerate(stream, start=1):
                processed_bytes += len(line)
                if not line.strip():
                    continue
                rows_read += 1
                now = time.monotonic()
                if now - last_progress_update >= 0.5:
                    print_recall_scan_progress(
                        processed_bytes,
                        total_bytes,
                        rows_read,
                        len(recalled),
                        path,
                    )
                    last_progress_update = now
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON in {path} line {line_number}: {exc}"
                    ) from exc
                post_id = str(item.get("id", "")).removeprefix("t3_").strip()
                subreddit = str(item.get("subreddit", "")).strip()
                epoch = created_epoch(item)
                if (
                    not post_id
                    or subreddit.casefold() not in allowed
                    or epoch is None
                    or not start_epoch <= epoch < end_epoch
                ):
                    continue
                in_scope_ids.add(post_id)
                text = "\n".join(
                    [
                        str(item.get("title", "")),
                        str(item.get("selftext", "")),
                    ]
                )
                matched, match_details = find_drug_matches(
                    text,
                    aliases,
                    patterns,
                    exact_terms,
                    fuzzy_aliases,
                    fuzzy_cache,
                )
                class_only = not matched and bool(class_pattern.search(text))
                if not matched and not (include_class_only and class_only):
                    continue
                try:
                    num_comments = max(int(item.get("num_comments", 0)), 0)
                except (TypeError, ValueError):
                    num_comments = 0
                recalled.setdefault(
                    post_id,
                    RecalledPost(
                        post_id=post_id,
                        subreddit=subreddit,
                        created_utc=epoch,
                        num_comments=num_comments,
                        matched_drugs=matched,
                        match_details=match_details,
                        class_only=class_only,
                        source_path=str(path),
                    ),
                )
        print_recall_scan_progress(
            processed_bytes,
            total_bytes,
            rows_read,
            len(recalled),
            path,
        )
    print_recall_scan_progress(
        processed_bytes,
        total_bytes,
        rows_read,
        len(recalled),
        post_files[-1],
        final=True,
    )
    posts = sorted(
        recalled.values(),
        key=lambda post: (
            post.subreddit.casefold(),
            post.created_utc,
            post.post_id,
        ),
    )
    return posts, {
        "post_files_scanned": len(post_files),
        "rows_read": rows_read,
        "unique_posts_in_scope": len(in_scope_ids),
        "recalled_posts_before_limit": len(posts),
    }


def write_recall_manifest(posts: list[RecalledPost], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "recalled_posts.jsonl"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for post in posts:
            stream.write(
                json.dumps(
                    {
                        "post_id": post.post_id,
                        "subreddit": post.subreddit,
                        "created_utc": post.created_utc,
                        "num_comments": post.num_comments,
                        "matched_target_drugs": list(post.matched_drugs),
                        "drug_match_details": list(post.match_details),
                        "class_only": post.class_only,
                        "source_path": post.source_path,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    os.replace(temporary, path)
    return path


def write_recall_summary(
    posts: list[RecalledPost],
    scan_stats: dict[str, int],
    output_dir: Path,
    posts_root: Path,
    subreddits: list[str],
    start_epoch: int,
    end_epoch: int,
    aliases: dict[str, list[str]],
    include_class_only: bool,
) -> Path:
    """Persist recall counts and matching diagnostics for later reporting."""
    by_subreddit: dict[str, dict[str, int]] = {}
    by_drug: Counter[str] = Counter()
    by_drug_set: Counter[str] = Counter()
    matched_terms: Counter[tuple[Any, ...]] = Counter()
    posts_with_exact = 0
    posts_with_fuzzy = 0
    posts_with_only_fuzzy = 0

    for post in posts:
        subreddit = by_subreddit.setdefault(
            post.subreddit,
            {"recalled_posts": 0, "reported_comments": 0},
        )
        subreddit["recalled_posts"] += 1
        subreddit["reported_comments"] += post.num_comments
        by_drug.update(post.matched_drugs)
        group = " + ".join(post.matched_drugs) if post.matched_drugs else "class_only"
        by_drug_set[group] += 1

        match_types = {
            str(detail.get("match_type", "")) for detail in post.match_details
        }
        if "exact" in match_types:
            posts_with_exact += 1
        if "fuzzy" in match_types:
            posts_with_fuzzy += 1
        if match_types == {"fuzzy"}:
            posts_with_only_fuzzy += 1
        for detail in post.match_details:
            matched_terms[
                (
                    detail.get("canonical_drug", ""),
                    str(detail.get("matched_text", "")).casefold(),
                    detail.get("matched_alias", ""),
                    detail.get("match_type", ""),
                    detail.get("edit_distance", 0),
                    detail.get("similarity", 0),
                )
            ] += 1

    in_scope = int(scan_stats.get("unique_posts_in_scope", 0))
    total_recalled = int(scan_stats.get("recalled_posts_before_limit", len(posts)))
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "interpretation": (
            "Recall counts represent posts containing candidate target-drug "
            "mentions. They do not represent confirmed personal use, adverse "
            "events, exposure regimens, or clinical incidence."
        ),
        "parameters": {
            "posts_root": str(posts_root.resolve()),
            "start_date_inclusive": date_label(start_epoch),
            "end_date_exclusive": date_label(end_epoch),
            "subreddits": subreddits,
            "include_class_only": include_class_only,
            "fuzzy_min_alias_length": FUZZY_MIN_ALIAS_LENGTH,
            "fuzzy_similarity_threshold": FUZZY_SIMILARITY_THRESHOLD,
            "drug_aliases": aliases,
        },
        "scan": scan_stats,
        "results": {
            "recalled_posts_before_limit": total_recalled,
            "selected_posts_written_to_manifest": len(posts),
            "recall_rate_percent": round(100 * total_recalled / in_scope, 4)
            if in_scope
            else 0.0,
            "reported_comments_for_selected_posts": sum(
                post.num_comments for post in posts
            ),
            "posts_with_exact_match": posts_with_exact,
            "posts_with_fuzzy_match": posts_with_fuzzy,
            "posts_with_only_fuzzy_match": posts_with_only_fuzzy,
            "class_only_posts": sum(post.class_only for post in posts),
            "posts_with_multiple_target_drug_mentions": sum(
                len(post.matched_drugs) > 1 for post in posts
            ),
        },
        "by_subreddit": dict(sorted(by_subreddit.items())),
        "by_target_drug": dict(sorted(by_drug.items())),
        "by_recalled_drug_mention_set": dict(sorted(by_drug_set.items())),
        "matched_terms": [
            {
                "canonical_drug": key[0],
                "matched_text": key[1],
                "matched_alias": key[2],
                "match_type": key[3],
                "edit_distance": key[4],
                "similarity": key[5],
                "post_count": count,
            }
            for key, count in sorted(
                matched_terms.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "recall_summary.json"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return path


def print_recall_summary(
    posts: list[RecalledPost], scan_stats: dict[str, int]
) -> None:
    by_subreddit: dict[str, int] = {}
    by_drug: dict[str, int] = {}
    for post in posts:
        by_subreddit[post.subreddit] = by_subreddit.get(post.subreddit, 0) + 1
        for drug in post.matched_drugs:
            by_drug[drug] = by_drug.get(drug, 0) + 1
    total_recalled = int(scan_stats.get("recalled_posts_before_limit", len(posts)))
    print(
        f"Posts in scope: {int(scan_stats.get('unique_posts_in_scope', 0)):,}; "
        f"recalled before limit: {total_recalled:,}; "
        f"selected: {len(posts):,}; "
        f"reported comments for selected posts: "
        f"{sum(post.num_comments for post in posts):,}"
    )
    for subreddit, count in sorted(by_subreddit.items(), key=lambda pair: pair[0].casefold()):
        print(f"  r/{subreddit}: {count:,} posts")
    if by_drug:
        print("Drug matches:")
        for drug, count in sorted(by_drug.items()):
            print(f"  {drug}: {count:,} posts")


def targeted_output_path(output_dir: Path, post: RecalledPost) -> Path:
    return (
        output_dir
        / post.subreddit
        / "comments"
        / f"comments_for_{post.post_id}.jsonl"
    )


def load_targeted_state(
    output_path: Path,
    post: RecalledPost,
    start_epoch: int,
    end_epoch: int,
) -> dict[str, Any]:
    state_path = state_path_for(output_path)
    if not state_path.exists():
        if output_path.exists() and output_path.stat().st_size > 0:
            raise RuntimeError(
                f"Found targeted comments without state: {output_path}"
            )
        return {
            "post_id": post.post_id,
            "subreddit": post.subreddit,
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
            "cursor": max(post.created_utc, start_epoch),
            "count": 0,
            "pages": 0,
            "complete": False,
        }
    with state_path.open("r", encoding="utf-8") as stream:
        state = json.load(stream)
    expected = {
        "post_id": post.post_id,
        "subreddit": post.subreddit,
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
    }
    if any(state.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"Targeted state parameters do not match: {state_path}")
    if int(state.get("count", 0)) > 0 and (
        not output_path.exists() or output_path.stat().st_size == 0
    ):
        raise RuntimeError(f"Targeted state refers to missing data: {state_path}")
    return state


def save_targeted_state(output_path: Path, state: dict[str, Any]) -> None:
    state_path = state_path_for(output_path)
    payload = {
        **state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = state_path.with_suffix(state_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, state_path)


def download_targeted_post_comments(
    post: RecalledPost,
    index: int,
    total: int,
    output_dir: Path,
    start_epoch: int,
    end_epoch: int,
    args: argparse.Namespace,
) -> None:
    output_path = targeted_output_path(output_dir, post)
    state = load_targeted_state(output_path, post, start_epoch, end_epoch)
    if state.get("complete") and output_path.exists():
        print(
            f"[{index:04d}/{total:04d}] SKIP r/{post.subreddit} "
            f"post {post.post_id}: {int(state.get('count', 0)):,} comments"
        )
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.touch(exist_ok=True)
    cursor = max(int(state["cursor"]), post.created_utc, start_epoch)
    recent_ids = load_recent_ids(output_path, args.page_size * 2)

    while cursor < end_epoch:
        items = request_items(
            f"{API_BASE}/comments/search",
            {
                "link_id": post.post_id,
                "after": cursor,
                "before": end_epoch,
                "sort": "asc",
                "limit": args.page_size,
                "fields": ",".join(FIELDS["comments"]),
            },
            args,
        )
        valid = []
        page_epochs = []
        for item in items:
            epoch = created_epoch(item)
            if epoch is None or not start_epoch <= epoch < end_epoch:
                continue
            page_epochs.append(epoch)
            item_id = str(item.get("id", "")).strip()
            if item_id and item_id in recent_ids:
                continue
            valid.append(item)

        if not items:
            cursor = end_epoch
        elif not page_epochs:
            raise RuntimeError(
                f"Comment thread {post.post_id} returned unusable timestamps"
            )
        else:
            next_cursor = max(page_epochs) + 1
            if next_cursor <= cursor:
                raise RuntimeError(
                    f"Comment pagination stalled for post {post.post_id}"
                )
            cursor = min(next_cursor, end_epoch)

        if valid:
            with output_path.open("a", encoding="utf-8") as stream:
                for item in valid:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        recent_ids = {
            str(item.get("id", "")).strip()
            for item in items
            if str(item.get("id", "")).strip()
        }
        state["cursor"] = cursor
        state["count"] = int(state.get("count", 0)) + len(valid)
        state["pages"] = int(state.get("pages", 0)) + 1
        state["complete"] = cursor >= end_epoch
        save_targeted_state(output_path, state)
        print(
            f"[{index:04d}/{total:04d}] r/{post.subreddit:<22} "
            f"post {post.post_id:<8} | "
            f"{int(state['count']):>5,} comments | "
            f"page {int(state['pages']):>3}",
            end="\r",
            flush=True,
        )
        if cursor < end_epoch and args.request_delay:
            time.sleep(args.request_delay)
    print()


def run_targeted_comment_mode(args: argparse.Namespace) -> None:
    start_epoch = int(parse_utc_date(args.start_date).timestamp())
    end_epoch = int(parse_utc_date(args.end_date).timestamp())
    if start_epoch >= end_epoch:
        raise ValueError("--start-date must be earlier than --end-date")
    subreddits = normalize_subreddits(args.subreddits)
    aliases = load_drug_aliases(args.drug_alias_file)
    posts, scan_stats = recall_posts(
        args.targeted_comments_from_posts,
        subreddits,
        start_epoch,
        end_epoch,
        aliases,
        args.include_class_only,
    )
    if args.max_recalled_posts is not None:
        if args.max_recalled_posts < 1:
            raise ValueError("--max-recalled-posts must be at least 1")
        posts = posts[: args.max_recalled_posts]
    print_recall_summary(posts, scan_stats)
    if args.dry_run:
        print("Dry run: no manifest or comments were written.")
        return
    output_dir = args.output_dir.resolve()
    manifest = write_recall_manifest(posts, output_dir)
    summary = write_recall_summary(
        posts,
        scan_stats,
        output_dir,
        args.targeted_comments_from_posts,
        subreddits,
        start_epoch,
        end_epoch,
        aliases,
        args.include_class_only,
    )
    print(f"Recall manifest: {manifest}")
    print(f"Recall summary:  {summary}")
    try:
        for index, post in enumerate(posts, start=1):
            download_targeted_post_comments(
                post,
                index,
                len(posts),
                output_dir,
                start_epoch,
                end_epoch,
                args,
            )
    except KeyboardInterrupt:
        print("\nStopped by user. Rerun the same command to resume.")
        raise SystemExit(130)
    print("All targeted comment threads completed.")


def main() -> None:
    args = parse_args()
    if not 1 <= args.page_size <= 100:
        raise ValueError("--page-size must be between 1 and 100")
    if args.request_delay < 0:
        raise ValueError("--request-delay cannot be negative")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.max_retries < 0:
        raise ValueError("--max-retries cannot be negative")
    if args.targeted_comments_from_posts is not None:
        run_targeted_comment_mode(args)
        return

    jobs = build_jobs(args)
    if args.dry_run:
        print_plan(jobs)
        return

    print(
        f"Starting {len(jobs)} jobs. Press Ctrl+C to stop safely; "
        "run the same command to resume."
    )
    try:
        for job in jobs:
            download_job(job, args)
    except KeyboardInterrupt:
        print("\nStopped by user. Completed pages are saved; rerun to resume.")
        raise SystemExit(130)
    print("All download jobs completed.")


if __name__ == "__main__":
    main()
