"""
Step 3: Side Effect Extraction and MedDRA Mapping via RAG
==========================================================
Extracts self-reported side effects from Reddit posts and maps them to
MedDRA Preferred Terms using GPT-4.1-mini with retrieval-augmented
generation (RAG) over MedDRA terminology files.

Model: GPT-4.1-mini
API: OpenAI Responses API (Batch) with file_search tool

Pipeline:
  1. Upload MedDRA terminology files (.asc) to an OpenAI vector store
  2. For each self-use post, send the text to GPT-4.1-mini which searches
     the vector store to identify the most appropriate MedDRA PTs
  3. Parse structured JSON output: [{side_effect, medDRA_concept}, ...]

NOTE: Requires an OpenAI API key set as OPENAI_API_KEY environment variable.
      Requires MedDRA .asc files (available under license from MedDRA MSSO).
"""

import json
import os
import time
from pathlib import Path

import pandas as pd
from openai import OpenAI

# ============================================================================
# Configuration
# ============================================================================

MODEL = "gpt-4.1-mini"
BATCH_INPUT_FILE = "batch_rag_input.jsonl"
BATCH_OUTPUT_FILE = "batch_rag_output.jsonl"

# Path to MedDRA ASCII files (adjust to your local installation)
MEDDRA_DIR = Path("MedDRA_28_0_English/MedAscii")

# System prompt for side effect extraction.
# In the actual study, this prompt was stored as an OpenAI saved prompt
# (referenced by ID) so the model could use file_search to look up MedDRA
# terms on the fly. The prompt instructs the model to:
#   - Identify any self-reported side effects in the Reddit post
#   - Map each side effect to the most appropriate MedDRA Preferred Term
#   - Return structured JSON
SIDE_EFFECT_SYSTEM_PROMPT = """\
You are a pharmacovigilance assistant. Your task is to extract self-reported \
side effects from a Reddit post about a GLP-1 receptor agonist medication \
(semaglutide or tirzepatide) and map each side effect to the most appropriate \
MedDRA Preferred Term (PT).

Instructions:
- Only extract side effects that the author attributes to their own use of the medication.
- Do NOT extract side effects mentioned about other people, hypothetical effects, \
or effects of unrelated medications.
- Use the file_search tool to look up the correct MedDRA Preferred Term for each \
side effect. Search for the symptom or a related medical term.
- If no side effects are described, return an empty list.
- Map colloquial descriptions to standard MedDRA PTs (e.g., "sulfur burps" → \
"Eructation", "brain fog" → "Cognitive disorder").

Return a JSON object with the following schema:
{
  "side_effect_list": [
    {
      "side_effect": "<description from the post>",
      "medDRA_concept": "<MedDRA Preferred Term>"
    }
  ]
}"""

# JSON schema for structured output
SIDE_EFFECT_SCHEMA = {
    "type": "object",
    "properties": {
        "side_effect_list": {
            "type": "array",
            "description": "A list of side effects and their corresponding MedDRA concepts.",
            "items": {
                "type": "object",
                "properties": {
                    "side_effect": {
                        "type": "string",
                        "description": "Description of the side effect.",
                    },
                    "medDRA_concept": {
                        "type": "string",
                        "description": "The MedDRA concept associated with the side effect.",
                    },
                },
                "required": ["side_effect", "medDRA_concept"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["side_effect_list"],
    "additionalProperties": False,
}


# ============================================================================
# Vector Store Setup
# ============================================================================


def create_vector_store(client, meddra_dir=MEDDRA_DIR):
    """
    Upload MedDRA .asc files to an OpenAI vector store for RAG.

    The vector store allows the model to search MedDRA terminology
    (Preferred Terms, Low-Level Terms, High-Level Terms, etc.) during
    inference, ensuring accurate mapping of colloquial side effect
    descriptions to standardized terms.

    Returns:
        vector_store_id (str)
    """
    # Create vector store
    vs = client.vector_stores.create(name="MedDRA Terminology v28.0")
    print(f"Created vector store: {vs.id}")

    # Upload relevant MedDRA files
    asc_files = [
        "pt.asc",      # Preferred Terms
        "llt.asc",     # Low-Level Terms
        "hlt.asc",     # High-Level Terms
        "hlgt.asc",    # High-Level Group Terms
        "soc.asc",     # System Organ Classes
        "mdhier.asc",  # MedDRA hierarchy
    ]

    for fname in asc_files:
        fpath = meddra_dir / fname
        if fpath.exists():
            file_obj = client.files.create(file=open(fpath, "rb"), purpose="assistants")
            client.vector_stores.files.create(vector_store_id=vs.id, file_id=file_obj.id)
            print(f"  Uploaded {fname}")
        else:
            print(f"  WARNING: {fpath} not found, skipping")

    return vs.id


# ============================================================================
# Batch Creation
# ============================================================================


def create_batch_input(df, vector_store_id, output_path=BATCH_INPUT_FILE):
    """
    Create a JSONL file for the OpenAI Responses Batch API with file_search.

    Parameters:
        df: DataFrame with a 'message' column (self-use posts from Step 2)
        vector_store_id: ID of the OpenAI vector store with MedDRA terms
        output_path: path for the .jsonl output file
    """
    with open(output_path, "w") as f:
        for idx, row in df.iterrows():
            task = {
                "custom_id": f"task-{idx}",
                "method": "POST",
                "url": "/v1/responses",
                "body": {
                    "model": MODEL,
                    "instructions": SIDE_EFFECT_SYSTEM_PROMPT,
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": row["message"]}
                            ],
                        }
                    ],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "side_effects",
                            "strict": True,
                            "schema": SIDE_EFFECT_SCHEMA,
                        }
                    },
                    "tools": [
                        {
                            "type": "file_search",
                            "vector_store_ids": [vector_store_id],
                        }
                    ],
                    "max_output_tokens": 8753,
                },
            }
            f.write(json.dumps(task) + "\n")

    print(f"Wrote {len(df):,} tasks to {output_path}")


# ============================================================================
# Submit and retrieve batch
# ============================================================================


def submit_batch(client, input_path=BATCH_INPUT_FILE):
    """Upload the JSONL file and submit a batch job."""
    batch_file = client.files.create(file=open(input_path, "rb"), purpose="batch")
    batch = client.batches.create(
        input_file_id=batch_file.id,
        endpoint="/v1/responses",
        completion_window="24h",
    )
    print(f"Batch submitted: {batch.id}")
    return batch


def wait_for_batch(client, batch_id, poll_interval=60):
    """Poll until the batch completes."""
    while True:
        batch = client.batches.retrieve(batch_id)
        print(f"  Status: {batch.status} ({batch.request_counts})")
        if batch.status in ("completed", "failed", "expired"):
            return batch
        time.sleep(poll_interval)


def download_results(client, batch, output_path=BATCH_OUTPUT_FILE):
    """Download batch results to a JSONL file."""
    content = client.files.content(batch.output_file_id)
    with open(output_path, "wb") as f:
        f.write(content.read())
    print(f"Results saved to {output_path}")


# ============================================================================
# Parse results
# ============================================================================


def parse_batch_results(output_path=BATCH_OUTPUT_FILE):
    """
    Parse the RAG batch output into a DataFrame.

    Returns a DataFrame with columns:
        _task_index: original row index
        meddra_concepts: set of MedDRA PT names extracted
        side_effect_details: list of {side_effect, medDRA_concept} dicts
    """
    records = []
    with open(output_path) as f:
        for line in f:
            result = json.loads(line)
            custom_id = result["custom_id"]
            idx = int(custom_id.replace("task-", ""))

            try:
                # Navigate the Responses API output structure
                body = result["response"]["body"]
                # Find the text output
                for item in body.get("output", []):
                    if item.get("type") == "message":
                        for content in item.get("content", []):
                            if content.get("type") == "output_text":
                                parsed = json.loads(content["text"])
                                se_list = parsed.get("side_effect_list", [])
                                concepts = {
                                    entry["medDRA_concept"]
                                    for entry in se_list
                                    if entry.get("medDRA_concept")
                                }
                                records.append(
                                    {
                                        "_task_index": idx,
                                        "meddra_concepts": concepts,
                                        "side_effect_details": se_list,
                                    }
                                )
                                break
                        break
                else:
                    records.append(
                        {
                            "_task_index": idx,
                            "meddra_concepts": set(),
                            "side_effect_details": [],
                        }
                    )
            except (KeyError, json.JSONDecodeError):
                records.append(
                    {
                        "_task_index": idx,
                        "meddra_concepts": set(),
                        "side_effect_details": [],
                    }
                )

    return pd.DataFrame(records).sort_values("_task_index").reset_index(drop=True)


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    client = OpenAI()

    # Load self-use posts from Step 2
    df = pd.read_csv("self_use_posts.csv")
    print(f"Loaded {len(df):,} self-use posts")

    # Create vector store with MedDRA files (do this once, then reuse the ID)
    vector_store_id = create_vector_store(client)
    # Or reuse an existing vector store:
    # vector_store_id = "vs_your_vector_store_id_here"

    # Create batch input
    create_batch_input(df, vector_store_id)

    # Submit batch
    batch = submit_batch(client)

    # Wait for completion
    batch = wait_for_batch(client, batch.id)

    if batch.status == "completed":
        download_results(client, batch)
        results = parse_batch_results()

        # Merge with original data
        merged = pd.concat([df.reset_index(drop=True), results], axis=1)

        # Convert meddra_concepts sets to strings for CSV storage
        merged["meddra_concepts_str"] = merged["meddra_concepts"].apply(
            lambda s: str(list(s)) if isinstance(s, set) else "[]"
        )

        merged.to_csv("posts_with_meddra.csv", index=False)
        print(f"Saved {len(merged):,} rows to posts_with_meddra.csv")

        # ================================================================
        # IMPORTANT: Manual validation step before proceeding to Step 4
        # ================================================================
        # After running this pipeline, the extracted MedDRA concept strings
        # require manual review before proceeding to table generation.
        # Specifically:
        #   1. Some model-generated concept strings do not exactly match
        #      official MedDRA Preferred Term names and need manual mapping
        #      to the correct PT code.
        #   2. Ambiguous or overly broad mappings were reviewed by the
        #      study team and corrected.
        #   3. Concepts that could not be mapped to any MedDRA PT were
        #      reviewed and either mapped manually or excluded.
        # This manual validation was performed in multiple rounds as new
        # data batches were processed, producing supplementary mapping
        # files (CSVs with columns: meddra_concept, pt_code) that are
        # consumed by Step 4 (04_generate_tables.py) via the
        # MANUAL_MAPPINGS_FILE parameter.
    else:
        print(f"Batch failed with status: {batch.status}")
