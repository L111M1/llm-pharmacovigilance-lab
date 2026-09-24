"""Resumable keyword recall of Reddit posts and comments from Arctic Shift."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import BadStatusLine, IncompleteRead, RemoteDisconnected
from pathlib import Path
from threading import Event
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE = "https://arctic-shift.photon-reddit.com/api"
FUZZY_THRESHOLD = 0.80
MIN_FUZZY_ALIAS_LENGTH = 7
COMMENT_OR_BATCH = 8
DEFAULT_DRUG_ALIASES = {
    "dapagliflozin": ["dapagliflozin", "dapaglifozin", "farxiga", "forxiga", "xigduo", "qtern"],
    "empagliflozin": ["empagliflozin", "empaglifozin", "jardiance", "jardance", "synjardy", "glyxambi", "trijardy"],
    "canagliflozin": ["canagliflozin", "canaglifozin", "invokana", "invokamet"],
    "ertugliflozin": ["ertugliflozin", "ertuglifozin", "steglatro", "segluromet", "steglujan"],
}
SAFETY_TERMS = (
    "side effect", "side effects", "side-effect", "side-effects",
    "sideeffect", "sideeffects", "side affect",
    "side affects", "adverse effect", "adverse effects", "adverse reaction",
    "adverse reactions", "adverse event", "adverse events", "drug reaction",
    "medication reaction", "negative effect", "negative effects", "symptom",
    "symptoms", "intolerance",
)
KEY_NEIGHBORS = {
    "a": "sq", "b": "vn", "c": "xv", "d": "sf", "e": "wr", "f": "dg",
    "g": "fh", "h": "gj", "i": "uo", "j": "hk", "k": "jl", "l": "k",
    "m": "n", "n": "bm", "o": "ip", "p": "o", "q": "wa", "r": "et",
    "s": "ad", "t": "ry", "u": "yi", "v": "cb", "w": "qe", "x": "zc",
    "y": "tu", "z": "x",
}
VOWEL_CONFUSIONS = {"a": "e", "e": "a", "i": "e", "o": "u", "u": "o"}
FIELDS = {
    "posts": "id,author,created_utc,subreddit,title,selftext,num_comments",
    "comments": "id,author,created_utc,subreddit,body,link_id,parent_id,score",
}


@dataclass(frozen=True)
class SearchJob:
    kind: str
    subreddit: str
    label: str
    terms: tuple[dict[str, str], ...]
    start_epoch: int
    end_epoch: int
    output_path: Path

    @property
    def query(self) -> str:
        separator = " OR " if self.kind == "comments" else ""
        return separator.join(item["term"] for item in self.terms)

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "subreddit": self.subreddit, "label": self.label,
            "terms": list(self.terms), "start_epoch": self.start_epoch,
            "end_epoch": self.end_epoch,
        }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_jsonl(path: Path, *, repair_tail: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r+b" if repair_tail else "rb") as stream:
        size = path.stat().st_size
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if repair_tail and stream.tell() == size and not line.endswith(b"\n"):
                    stream.seek(offset)
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
                    break
                raise RuntimeError(f"Invalid JSONL at {path}:{offset}") from exc
            rows.append(row)
            if repair_tail and not line.endswith(b"\n"):
                stream.seek(0, os.SEEK_END)
                stream.write(b"\n")
                stream.flush()
                os.fsync(stream.fileno())
    return rows


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def normalize_subreddits(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for raw in values:
        name = raw.removeprefix("r/").strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
    if not result:
        raise ValueError("At least one subreddit is required")
    return result


def load_aliases(path: Path | None) -> dict[str, list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8")) if path else DEFAULT_DRUG_ALIASES
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Drug alias file must be a nonempty JSON object")
    result = {}
    for drug, names in payload.items():
        if not isinstance(names, list) or not names:
            raise ValueError(f"Alias list missing for {drug}")
        canonical = str(drug).strip().casefold()
        aliases = list(dict.fromkeys(str(name).strip().casefold() for name in names))
        if not canonical or not all(name and name.isalpha() for name in aliases):
            raise ValueError(f"Invalid drug aliases for {drug}")
        result[canonical] = aliases
    return result


def damerau_distance(left: str, right: str) -> int:
    left, right = left.casefold(), right.casefold()
    rows = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for i in range(len(left) + 1):
        rows[i][0] = i
    for j in range(len(right) + 1):
        rows[0][j] = j
    for i in range(1, len(left) + 1):
        for j in range(1, len(right) + 1):
            rows[i][j] = min(
                rows[i-1][j] + 1, rows[i][j-1] + 1,
                rows[i-1][j-1] + (left[i-1] != right[j-1]),
            )
            if i > 1 and j > 1 and left[i-1] == right[j-2] and left[i-2] == right[j-1]:
                rows[i][j] = min(rows[i][j], rows[i-2][j-2] + 1)
    return rows[-1][-1]


def similarity(left: str, right: str) -> float:
    return 1.0 - damerau_distance(left, right) / max(len(left), len(right), 1)


def generated_typos(
    alias: str, *, compact: bool = False, min_length: int = MIN_FUZZY_ALIAS_LENGTH,
) -> list[dict[str, str]]:
    alias = alias.casefold()
    if len(alias) < min_length or not alias.isalpha():
        return []
    center = (len(alias) - 1) / 2
    interior = range(1, len(alias) - 1)
    omissions = sorted(interior, key=lambda i: (
        alias[i] != alias[i-1], alias[i] not in "aeiou", abs(i-center), i
    ))
    omit_count, swap_count, substitute_count = (2, 1, 1) if compact else (3, 3, 3)
    selected = list(dict.fromkeys([*omissions[:omit_count], len(alias)-1]))
    if not compact:
        selected = selected[:4]
    swaps = sorted(
        (i for i in range(1, len(alias)-2) if alias[i] != alias[i+1]),
        key=lambda i: (abs(i+0.5-center), i),
    )
    substitutions = []
    for i in interior:
        options = VOWEL_CONFUSIONS.get(alias[i], "") + KEY_NEIGHBORS.get(alias[i], "")
        for rank, replacement in enumerate(dict.fromkeys(options)):
            if replacement != alias[i]:
                substitutions.append((rank, abs(i-center), i, replacement))
    substitutions.sort()
    candidates = (
        [(alias[:i] + alias[i+1:], "omission") for i in selected]
        + [(alias[:i] + alias[i+1] + alias[i] + alias[i+2:], "transpose")
           for i in swaps[:swap_count]]
        + [(alias[:i] + replacement + alias[i+1:], "substitution")
           for _, _, i, replacement in substitutions[:substitute_count]]
    )
    result, seen = [], {alias}
    for term, kind in candidates:
        if term not in seen and similarity(term, alias) >= FUZZY_THRESHOLD:
            seen.add(term)
            result.append({"term": term, "source": alias, "kind": kind})
    return result


def drug_terms(aliases: dict[str, list[str]], *, compact: bool) -> dict[str, list[dict[str, str]]]:
    per_drug = {}
    for drug, names in aliases.items():
        terms = {}
        for alias in dict.fromkeys([drug, *names]):
            terms[alias] = {"term": alias, "source": alias, "kind": "exact"}
            for variant in generated_typos(alias, compact=compact):
                terms.setdefault(variant["term"], variant)
        per_drug[drug] = terms
    owners: dict[str, set[str]] = {}
    for drug, terms in per_drug.items():
        for term in terms:
            owners.setdefault(term, set()).add(drug)
    return {
        drug: [item for term, item in terms.items() if len(owners[term]) == 1]
        for drug, terms in per_drug.items()
    }


def safety_terms() -> list[dict[str, str]]:
    terms: dict[str, dict[str, str]] = {}
    for phrase in SAFETY_TERMS:
        terms[phrase] = {"term": phrase, "source": phrase, "kind": "exact"}
        # The post search API does not promise OR semantics. Restrict expansion
        # of the broad safety vocabulary to two likely typo forms per token.
        words = phrase.split()
        for index, word in enumerate(words):
            for typo in generated_typos(word, compact=True, min_length=5)[:2]:
                variant = " ".join([*words[:index], typo["term"], *words[index+1:]])
                terms.setdefault(variant, {"term": variant, "source": phrase,
                                           "kind": typo["kind"]})
    return list(terms.values())


def build_jobs(
    kind: str, root: Path, subreddits: list[str], start: datetime, end: datetime,
    aliases: dict[str, list[str]],
) -> list[SearchJob]:
    # Start with the whole interval. Split only a range that the API rejects
    # (HTTP 422), instead of issuing thousands of empty year/keyword requests.
    slices = [(int(start.timestamp()), int(end.timestamp()))]
    if slices[0][0] >= slices[0][1]:
        raise ValueError("--start-date must precede --end-date")
    specs: list[tuple[str, tuple[dict[str, str], ...]]] = []
    if kind == "comments":
        for drug, terms in drug_terms(aliases, compact=False).items():
            for i in range(0, len(terms), COMMENT_OR_BATCH):
                specs.append((drug, tuple(terms[i:i+COMMENT_OR_BATCH])))
    else:
        for drug, terms in drug_terms(aliases, compact=True).items():
            specs.extend((drug, (item,)) for item in terms)
        specs.extend(("safety", (item,)) for item in safety_terms())
    jobs = []
    for subreddit in subreddits:
        for label, terms in specs:
            for start_epoch, end_epoch in slices:
                material = json.dumps([kind, subreddit, label, terms, start_epoch, end_epoch],
                                      ensure_ascii=False, sort_keys=True)
                key = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
                path = root / f"direct_{kind}" / "search" / f"{key}.jsonl"
                jobs.append(SearchJob(kind, subreddit, label, terms,
                                      start_epoch, end_epoch, path))
    return jobs


def epoch(item: dict[str, Any]) -> int | None:
    try:
        return int(float(item["created_utc"]))
    except (KeyError, TypeError, ValueError):
        return None


def request_items(
    job: SearchJob, cursor: int, boundary: int, args: argparse.Namespace,
    stop: Event,
) -> list[dict[str, Any]]:
    params = {
        "subreddit": job.subreddit,
        "query" if job.kind == "posts" else "body": job.query,
        "after": max(job.start_epoch - 1, cursor - 1),
        "before": boundary,
        "sort": "asc",
        "limit": 100,
        "fields": FIELDS[job.kind],
    }
    request = Request(
        f"{API_BASE}/{job.kind}/search?{urlencode(params)}",
        headers={"Accept": "application/json", "User-Agent": "reddit-pharmacovigilance-research/2.0"},
    )
    for attempt in range(args.max_retries + 1):
        if stop.is_set():
            raise InterruptedError("Search interrupted")
        try:
            with urlopen(request, timeout=args.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            items = payload.get("data", []) if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                raise ValueError("API response has no item array")
            return [item for item in items if isinstance(item, dict)]
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt >= args.max_retries:
                raise
            wait = min(2 ** attempt + random.random(), 30)
        except (URLError, TimeoutError, ConnectionError, RemoteDisconnected,
                BadStatusLine, IncompleteRead, ssl.SSLError,
                json.JSONDecodeError) as exc:
            if attempt >= args.max_retries:
                raise
            wait = min(2 ** attempt + random.random(), 30)
        if stop.wait(wait):
            raise InterruptedError("Search interrupted")
    raise RuntimeError("Unreachable retry state")


def state_path(job: SearchJob) -> Path:
    return job.output_path.with_suffix(job.output_path.suffix + ".state.json")


def load_state(job: SearchJob) -> dict[str, Any]:
    path = state_path(job)
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if any(state.get(key) != value for key, value in job.identity.items()):
            raise RuntimeError(f"Search checkpoint parameters changed: {path}")
    else:
        if job.output_path.exists() and job.output_path.stat().st_size:
            raise RuntimeError(f"Search data without checkpoint: {job.output_path}")
        state = {**job.identity, "ranges": [[job.start_epoch, job.end_epoch]],
                 "count": 0, "pages": 0, "complete": False}
        atomic_json(path, state)
    return state


def search_job(job: SearchJob, args: argparse.Namespace, stop: Event) -> int:
    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(job)
    seen = {str(item.get("id", "")) for item in read_jsonl(job.output_path, repair_tail=True)}
    if state["complete"]:
        if len(seen) != int(state["count"]):
            raise RuntimeError(f"Completed search data and state disagree: {job.output_path}")
        return 0
    if len(seen) != int(state["count"]):
        state["count"] = len(seen)
        atomic_json(state_path(job), state)
    added = 0
    while state["ranges"]:
        if stop.is_set():
            return added
        cursor, boundary = map(int, state["ranges"][0])
        try:
            items = request_items(job, cursor, boundary, args, stop)
        except HTTPError as exc:
            if exc.code != 422 or boundary - cursor <= 1:
                raise
            midpoint = cursor + (boundary - cursor) // 2
            state["ranges"][:1] = [[cursor, midpoint], [midpoint, boundary]]
            atomic_json(state_path(job), state)
            continue
        valid, timestamps = [], []
        for item in items:
            created = epoch(item)
            if created is None or not cursor <= created < boundary:
                continue
            timestamps.append(created)
            ident = str(item.get("id", "")).strip()
            if ident and ident not in seen:
                seen.add(ident)
                valid.append(item)
        if valid:
            with job.output_path.open("a", encoding="utf-8") as stream:
                for item in valid:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            added += len(valid)
        if len(items) < 100:
            state["ranges"].pop(0)
        elif not timestamps:
            raise RuntimeError(f"Unusable full search page: {job.output_path}")
        else:
            last = max(timestamps)
            if timestamps.count(last) > 1:
                raise RuntimeError(
                    f"Full page ties at second {last}; cannot prove no records "
                    f"were skipped: {job.output_path}"
                )
            next_cursor = last + 1
            if next_cursor <= cursor:
                raise RuntimeError(f"Search pagination stalled: {job.output_path}")
            state["ranges"][0][0] = next_cursor
        state["count"] = len(seen)
        state["pages"] = int(state["pages"]) + 1
        state["complete"] = not state["ranges"]
        atomic_json(state_path(job), state)
        if stop.wait(args.request_delay):
            return added
    return added


def matched_term(text: str, job: SearchJob) -> dict[str, str] | None:
    for spec in job.terms:
        term = spec["term"]
        if not re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text, re.I):
            continue
        if spec["kind"] == "exact" or similarity(term, spec["source"]) >= FUZZY_THRESHOLD:
            return spec
    return None


def journal_key(entry: dict[str, Any]) -> tuple[str, str]:
    return str(entry["kind"]), str(entry["record"]["id"])


def consolidate(
    jobs: list[SearchJob], root: Path, kind: str, config: dict[str, Any],
) -> None:
    legacy_ids = set()
    if kind == "comments":
        for path in root.glob("*/comments/comments_for_*.jsonl"):
            legacy_ids.update(str(row.get("id", "")) for row in read_jsonl(path))
    else:
        manifest = root / "recalled_posts.jsonl"
        legacy_ids.update(str(row.get("post_id", "")) for row in read_jsonl(manifest))

    journal = root / "direct_records.jsonl"
    prior_entries = read_jsonl(journal, repair_tail=True)
    prior_keys = {journal_key(entry) for entry in prior_entries}
    verified: dict[str, dict[str, Any]] = {}
    candidates = skipped_legacy = rejected = 0
    for job in jobs:
        state = load_state(job)
        if not state["complete"]:
            raise RuntimeError(f"Search job incomplete: {job.output_path}")
        items = read_jsonl(job.output_path)
        if len({str(item.get("id", "")) for item in items}) != int(state["count"]):
            raise RuntimeError(f"Search data and checkpoint disagree: {job.output_path}")
        for item in items:
            candidates += 1
            ident = str(item.get("id", "")).strip()
            if not ident:
                rejected += 1
                continue
            if ident in legacy_ids:
                skipped_legacy += 1
                continue
            text = (str(item.get("title") or "") + "\n" + str(item.get("selftext") or "")
                    if kind == "posts" else str(item.get("body") or ""))
            spec = matched_term(text, job)
            if spec is None:
                rejected += 1
                continue
            if ident not in verified:
                verified[ident] = {**item, "matched_target_drugs": [],
                                   "matched_safety_terms": []}
            entry = verified[ident]
            key = "matched_safety_terms" if job.label == "safety" else "matched_target_drugs"
            label = spec["source"] if job.label == "safety" else job.label
            if label not in entry[key]:
                entry[key].append(label)

    rows = sorted(verified.values(), key=lambda row: (epoch(row) or 0, str(row["id"])))
    for row in rows:
        row["matched_target_drugs"].sort()
        row["matched_safety_terms"].sort()
    mode_dir = root / f"direct_{kind}"
    atomic_jsonl(mode_dir / f"verified_{kind}.jsonl", rows)

    added_to_journal = 0
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as stream:
        for row in rows:
            key = kind.removesuffix("s"), str(row["id"])
            if key in prior_keys:
                continue
            stream.write(json.dumps({"kind": key[0], "record": row}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            prior_keys.add(key)
            added_to_journal += 1
    atomic_json(mode_dir / "manifest.json", {
        **config, "complete": True,
        "candidate_rows_across_queries": candidates,
        "skipped_legacy_rows": skipped_legacy,
        "rejected_or_unverifiable_rows": rejected,
        "unique_verified_records": len(rows),
        "added_to_journal_this_run": added_to_journal,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    print(
        f"{kind}: {candidates:,} query hits; {skipped_legacy:,} already in "
        f"legacy data; {len(rows):,} unique verified; "
        f"{added_to_journal:,} appended to {journal}"
    )


@contextmanager
def output_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".keyword_recall.lock"
    with path.open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Another keyword recall process is using {root}") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def run(args: argparse.Namespace) -> None:
    kind = "posts" if args.direct_posts else "comments"
    root = args.output_dir.resolve()
    aliases = load_aliases(args.drug_alias_file.resolve() if args.drug_alias_file else None)
    subreddits = normalize_subreddits(args.subreddits)
    start, end = parse_date(args.start_date), parse_date(args.end_date)
    jobs = build_jobs(kind, root, subreddits, start, end, aliases)
    config = {
        "mode": f"direct_{kind}_v2",
        "subreddits": subreddits,
        "start_date_inclusive": args.start_date,
        "end_date_exclusive": args.end_date,
        "drug_aliases": aliases,
        "safety_terms": list(SAFETY_TERMS) if kind == "posts" else [],
        "fuzzy_min_alias_length": MIN_FUZZY_ALIAS_LENGTH,
        "fuzzy_similarity_threshold": FUZZY_THRESHOLD,
        "search_jobs": len(jobs),
        "search_plan_sha256": hashlib.sha256(json.dumps(
            [job.identity for job in jobs], ensure_ascii=False, sort_keys=True
        ).encode("utf-8")).hexdigest(),
    }
    print(
        f"Direct {kind} recall: {len(jobs):,} community/keyword jobs; "
        f"{args.search_workers} workers; output {root / ('direct_' + kind)}"
    )
    if args.dry_run:
        print("Dry run: no network requests or files written")
        return
    mode_dir = root / f"direct_{kind}"
    config_path = mode_dir / "search_config.json"
    with output_lock(root):
        if config_path.exists():
            if json.loads(config_path.read_text(encoding="utf-8")) != config:
                raise RuntimeError("Search settings changed; choose a new --output-dir")
        else:
            search_dir = mode_dir / "search"
            if search_dir.exists() and any(search_dir.iterdir()):
                raise RuntimeError("Search files exist without search_config.json")
            atomic_json(config_path, config)
        atomic_json(mode_dir / "manifest.json", {**config, "complete": False})
        stop = Event()
        failures = []
        done = added = 0
        executor = ThreadPoolExecutor(max_workers=args.search_workers)
        futures = {executor.submit(search_job, job, args, stop): job for job in jobs}
        try:
            for future in as_completed(futures):
                try:
                    added += future.result()
                except InterruptedError:
                    if not stop.is_set():
                        raise
                except Exception as exc:
                    failures.append((futures[future], exc))
                done += 1
                if done % 10 == 0 or done == len(jobs):
                    print(f"[{done:,}/{len(jobs):,}] new query hits: {added:,}; "
                          f"incomplete: {len(failures):,}")
        except KeyboardInterrupt:
            stop.set()
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            print("Stopped safely; rerun the same command to resume")
            raise SystemExit(130)
        else:
            executor.shutdown(wait=True)
        if failures:
            job, error = failures[0]
            raise RuntimeError(
                f"{len(failures)} search jobs incomplete; rerun to resume. "
                f"First: r/{job.subreddit} {job.query}: {error}"
            ) from error
        consolidate(jobs, root, kind, config)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--direct-posts", action="store_true",
                      help="Search title/body by drug and safety keywords; no community-wide dump")
    mode.add_argument("--direct-comments", action="store_true",
                      help="Search comment bodies by drug aliases and typos; no thread download")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--subreddits", nargs="+", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--drug-alias-file", type=Path,
                        help="JSON canonical drug -> correct names/brands; defaults to Study A")
    parser.add_argument("--search-workers", type=int, default=2)
    parser.add_argument("--request-delay", type=float, default=0.8)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.search_workers <= 8:
        parser.error("--search-workers must be 1..8")
    if args.request_delay < 0 or args.timeout <= 0 or args.max_retries < 0:
        parser.error("Invalid delay, timeout or retry setting")
    run(args)
