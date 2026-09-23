import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from pharmacovigilance_pipeline import (
    build_appendix_tables,
    migrate_output_layout,
)


class AppendixTableTests(unittest.TestCase):
    def test_legacy_output_migration_preserves_contents_and_is_repeatable(self):
        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            source_files = {
                ".user_hash_salt": ("state", b"salt"),
                "extractions.jsonl": ("state", b'{"record_id":"a"}\n'),
                "cleaned_posts.csv": ("cleaning", b"record_id\na\n"),
                "adverse_events.csv": ("records", b"user_id\na\n"),
                "appendix_table_1_symptom_pairs_zh.csv": ("tables", b"pair\na\n"),
            }
            for name, (_, content) in source_files.items():
                (output_dir / name).write_bytes(content)
            migrate_output_layout(output_dir)
            migrate_output_layout(output_dir)
            for name, (folder, content) in source_files.items():
                self.assertFalse((output_dir / name).exists())
                self.assertEqual((output_dir / folder / name).read_bytes(), content)

    def test_user_dedup_and_exclusive_single_drug_denominators(self):
        exposures = pd.DataFrame(
            [
                {"user_id": "a", "exposure_group": "drug_a"},
                {"user_id": "a", "exposure_group": "drug_a"},
                {"user_id": "b", "exposure_group": "drug_a"},
                {"user_id": "b", "exposure_group": "drug_b"},
                {"user_id": "c", "exposure_group": "drug_b"},
                {"user_id": "d", "exposure_group": "drug_a + drug_b"},
            ]
        )
        events = pd.DataFrame(
            [
                {"user_id": "a", "proposed_meddra_pt": "Pollakiuria", "validated_meddra_pt": "", "meddra_pt_zh": "尿频"},
                {"user_id": "a", "proposed_meddra_pt": "Urinary frequency", "validated_meddra_pt": "", "meddra_pt_zh": "尿频"},
                {"user_id": "a", "proposed_meddra_pt": "Nausea", "validated_meddra_pt": "", "meddra_pt_zh": "恶心"},
                {"user_id": "b", "proposed_meddra_pt": "Nausea", "validated_meddra_pt": "", "meddra_pt_zh": "恶心"},
                {"user_id": "c", "proposed_meddra_pt": "Nausea", "validated_meddra_pt": "", "meddra_pt_zh": "恶心"},
                {"user_id": "d", "proposed_meddra_pt": "Nausea", "validated_meddra_pt": "", "meddra_pt_zh": "恶心"},
            ]
        )
        pairs, frequency = build_appendix_tables(
            exposures, events, ["drug_a", "drug_b"],
            {"drug_a": "甲药", "drug_b": "乙药"},
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(set(pairs.loc[0, ["共现症状1", "共现症状2"]]), {"尿频", "恶心"})
        self.assertEqual(pairs.loc[0, "报告用户数"], 1)
        self.assertEqual(pairs.loc[0, "占目标药物暴露用户比例（%）"], 25.0)
        self.assertFalse((pairs["共现症状1"] == pairs["共现症状2"]).any())
        counts = frequency.set_index("症状中文辅助释义")
        self.assertEqual(counts.loc["尿频", "甲药（n=1）：人数"], 1)
        self.assertEqual(counts.loc["恶心", "甲药（n=1）：人数"], 1)
        self.assertEqual(counts.loc["恶心", "乙药（n=1）：人数"], 1)
        self.assertEqual(counts.loc["恶心", "甲药：比例（%）"], 100.0)


if __name__ == "__main__":
    unittest.main()
