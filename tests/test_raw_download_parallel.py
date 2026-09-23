import argparse
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from download_reddit_data import (
    build_jobs,
    download_job,
    download_raw_jobs_parallel,
    inspect_raw_output,
    load_state,
    output_directory_lock,
)


def args_for(output_dir, subreddits):
    return argparse.Namespace(
        output_dir=Path(output_dir),
        subreddits=subreddits,
        kinds=["posts"],
        start_date="2026-09-21",
        end_date="2026-09-22",
        page_size=100,
        request_delay=0,
        community_workers=2,
    )


class RawDownloadParallelTests(unittest.TestCase):
    def test_communities_overlap_but_resume_skips_completed_slices(self):
        with TemporaryDirectory() as directory:
            args = args_for(directory, ["Cholesterol", "repatha"])
            jobs = build_jobs(args)
            barrier = threading.Barrier(2, timeout=5)
            calls = []
            calls_lock = threading.Lock()

            def page(job, cursor, _args, **_kwargs):
                with calls_lock:
                    calls.append((job.subreddit, cursor))
                barrier.wait()
                return []

            with patch("download_reddit_data.request_page", side_effect=page):
                download_raw_jobs_parallel(jobs, args)
            self.assertEqual({name for name, _ in calls}, {"Cholesterol", "repatha"})
            self.assertTrue(all(load_state(job)["complete"] for job in jobs))

            with patch("download_reddit_data.request_page") as request_page:
                download_raw_jobs_parallel(jobs, args)
            request_page.assert_not_called()

    def test_incomplete_page_resumes_without_duplicate_record(self):
        with TemporaryDirectory() as directory:
            args = args_for(directory, ["Cholesterol"])
            job = build_jobs(args)[0]
            stop_event = threading.Event()

            def first_page(_job, cursor, _args, **_kwargs):
                stop_event.set()
                return [{"id": "abc", "created_utc": cursor + 10}]

            with patch("download_reddit_data.request_page", side_effect=first_page):
                download_job(job, args, stop_event=stop_event)
            self.assertFalse(load_state(job)["complete"])
            self.assertEqual(load_state(job)["count"], 1)

            with patch("download_reddit_data.request_page", return_value=[]):
                download_job(job, args)
            self.assertTrue(load_state(job)["complete"])
            records = [json.loads(line) for line in job.output_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["id"] for record in records], ["abc"])

    def test_output_directory_rejects_second_process_lock(self):
        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with output_directory_lock(output_dir, ".raw_download.lock", "raw-data"):
                with self.assertRaises(RuntimeError):
                    with output_directory_lock(output_dir, ".raw_download.lock", "raw-data"):
                        pass

    def test_resume_repairs_torn_tail_and_recounts_uncheckpointed_record(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "posts.jsonl"
            path.write_bytes(b'{"id":"saved"}\n{"id":"torn"}')
            ids, count = inspect_raw_output(path, 200)
            self.assertEqual(ids, {"saved", "torn"})
            self.assertEqual(count, 2)
            self.assertEqual(path.read_bytes(), b'{"id":"saved"}\n{"id":"torn"}\n')

            path.write_bytes(b'{"id":"saved"}\n{"id":"torn"')
            ids, count = inspect_raw_output(path, 200)
            self.assertEqual(ids, {"saved"})
            self.assertEqual(count, 1)
            self.assertEqual(path.read_bytes(), b'{"id":"saved"}\n')


if __name__ == "__main__":
    unittest.main()
