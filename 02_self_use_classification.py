"""
Step 2: Self-Use Classification via OpenAI Batch API
=====================================================
Classifies each Reddit post/comment for whether the author personally discloses
taking semaglutide or tirzepatide, and extracts the specific medication(s)
mentioned.

Model: GPT-4o-mini (temperature=0)
API: OpenAI Batch API (/v1/chat/completions)

NOTE: Requires an OpenAI API key set as OPENAI_API_KEY environment variable.
"""

import json
import os
import time

import pandas as pd
from openai import OpenAI

# ============================================================================
# Configuration
# ============================================================================

MODEL = "gpt-4o-mini"
MAX_TOKENS = 2048
BATCH_INPUT_FILE = "batch_self_use_input.jsonl"
BATCH_OUTPUT_FILE = "batch_self_use_output.jsonl"

# ============================================================================
# System prompt for self-use classification
# ============================================================================

SYSTEM_PROMPT = """\
You are an expert researcher looking around reddit for posts/comments
describing self-disclosures of weight loss treatments experienced by the author.

## Problem Setting
> You are interested in self-reported effects of a treatment on a user who
took the treatment themselves. You want to be able to answer the following question from the text of the post or comment:
1. Is the user currently take a weight loss medication? Planning to take the medication is not the same as having taken it.
- yes 
- no
2. If they are not currently taking it, is the user planning to take a weight loss medication in the future?
- yes
- no
3. If they are not currently taking it, has the user taken a weight loss medication in the past?
- yes
- no

If yes to any of the above:
4. Which medication is being taken (if disclosed)
5. Dose of medication (if disclosed)
6. What is their age (if disclosed)
7. What is their gender (if disclosed)
8. When they started taking the medication. Can be the exact date, number of months, number of weeks, etc. (can be a date in the past if currently taking, date in the past if previously took, or null if not disclosed)
9. When they stopped taking the medication (if previously took the medication)
10. Starting Weight (if disclosed)
11. Current Weight (if disclosed)
12. Weight lost (if disclosed)
13. Goal Weight (if disclosed)
14. Other pre-existing medical conditions (if disclosed)
15. Side effects of weight loss medication (if disclosed)
16. Location, e.g. state, city, country (if disclosed)
17. Mental health symptoms before taking the medication (if disclosed)
18. Mental health symptoms while taking the medication (if disclosed)
19. Mental health symptoms after taking the medication (if disclosed)

respond in json format
{
  "currently_taking_weight_loss_medication",
  "planning_to_take_in_future",
  "took_in_past",
  "medication",
  "dose",
  "age",
  "gender",
  "start_date",
  "stop_date",
  "start_weight",
  "current_weight",
  "weight_lost",
  "goal_weight",
  "pre_existing_conditions",
  "side_effects",
  "location", 
  "mh_symptoms_before",
  "mh_symptoms_during",
  "mh_symptoms_after"
}

## Examples
posted in r/Mounjaro
posted on 2023-01-25
So I have been having a hard time finding 5mg in stock until this week, being delivered tomorrow, the last time I took my 5mg shot was on 1/12/23. Do you guys think if I take this shot tomorrow after skipping a week I will be sick?

{
  "currently_taking_weight_loss_medication": "yes",
  "planning_to_take_in_future": null,
  "took_in_past": null,
  "medication": "Mounjaro",
  "dose": "5mg",
  "age": null,
  "gender": null,
  "start_date": "before 2023-01-12",
  "start_weight": null,
  "current_weight": null,
  "weight_lost": null,
  "goal_weight": null,
  "pre_existing_conditions": null,
  "side_effects": null,
  "location": null, 
  "mh_symptoms_before": null,
  "mh_symptoms_during": null,
  "mh_symptoms_after": null
}

posted in r/Mounjaro
posted on 2023-01-08
I am on Mounjaro 10 mg and have another box of it in my fridge I was able to get with a successful coupon pick up. My insurance now covers Wegovy and I was able to get it. My doctor is having me start at 1.7 mg to stabilize and then go to 2.4mg. Did anyone have any success with weight loss on Wegovy compared to Mounjaro? I know Mounjaro is the best to get and I would like to use my coupon till I can't anymore but I can't afford the out of pocket cost. SW 231 CW 199 GW 175.

{
  "currently_taking_weight_loss_medication": "yes",
  "planning_to_take_in_future": null,
  "took_in_past": null,
  "medication": "Mounjaro, Wegovy",
  "dose": "10mg, 1.7mg (starting dose), 2.4mg (future dose)",
  "age": null,
  "gender": null,
  "start_date": null,
  "stop_date": null,
  "start_weight": "231",
  "current_weight": "199",
  "weight_lost": "32",
  "goal_weight": "175",
  "pre_existing_conditions": null,
  "side_effects": null,
  "location": null,
  "mh_symptoms_before": null,
  "mh_symptoms_during": null,
  "mh_symptoms_after": null
}"""


# ============================================================================
# Build batch input
# ============================================================================


def build_user_message(row):
    """Format a Reddit post into the user message for the classifier."""
    return f"posted in r/{row['subreddit']}\nposted on {row['date']}\n{row['message']}"


def create_batch_input(df, output_path=BATCH_INPUT_FILE):
    """
    Create a JSONL file for the OpenAI Batch API.

    Parameters:
        df: DataFrame with columns [subreddit, date, message] (one row per post)
        output_path: path for the .jsonl output file
    """
    with open(output_path, "w") as f:
        for idx, row in df.iterrows():
            task = {
                "custom_id": f"task-{idx}",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": MODEL,
                    "max_tokens": MAX_TOKENS,
                    "temperature": 0,
                    "top_p": 1,
                    "frequency_penalty": 0,
                    "presence_penalty": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": build_user_message(row)},
                    ],
                },
            }
            f.write(json.dumps(task) + "\n")

    print(f"Wrote {len(df):,} tasks to {output_path}")


# ============================================================================
# Submit and retrieve batch
# ============================================================================


def submit_batch(client, input_path=BATCH_INPUT_FILE):
    """Upload the JSONL file and submit a batch job. Returns the batch object."""
    batch_file = client.files.create(file=open(input_path, "rb"), purpose="batch")
    batch = client.batches.create(
        input_file_id=batch_file.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
    )
    print(f"Batch submitted: {batch.id}")
    return batch


def wait_for_batch(client, batch_id, poll_interval=60):
    """Poll until the batch completes, then return the batch object."""
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
    Parse the batch output JSONL into a DataFrame.

    Returns a DataFrame with one row per post, including the parsed JSON
    fields from the model's response.
    """
    records = []
    with open(output_path) as f:
        for line in f:
            result = json.loads(line)
            custom_id = result["custom_id"]
            idx = int(custom_id.replace("task-", ""))

            try:
                content = result["response"]["body"]["choices"][0]["message"]["content"]
                parsed = json.loads(content)
            except (KeyError, json.JSONDecodeError):
                parsed = {}

            parsed["_task_index"] = idx
            records.append(parsed)

    return pd.DataFrame(records).sort_values("_task_index").reset_index(drop=True)


def filter_self_use(results_df):
    """
    Keep only posts where the user disclosed currently taking, previously
    taking, or planning to take the medication (i.e., self-use).

    The primary criterion is currently_taking_weight_loss_medication == 'yes'.
    Posts where the user took the medication in the past are also included.
    """
    mask = (
        (results_df["currently_taking_weight_loss_medication"].str.lower() == "yes")
        | (results_df["took_in_past"].str.lower() == "yes")
    )
    return results_df[mask].copy()


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    client = OpenAI()  # uses OPENAI_API_KEY env var

    # Load filtered posts from Step 1
    df = pd.read_csv("filtered_posts.csv")
    print(f"Loaded {len(df):,} posts")

    # Create batch input
    create_batch_input(df)

    # Submit batch
    batch = submit_batch(client)

    # Wait for completion
    batch = wait_for_batch(client, batch.id)

    if batch.status == "completed":
        # Download and parse results
        download_results(client, batch)
        results = parse_batch_results()

        # Merge with original data
        merged = pd.concat([df.reset_index(drop=True), results], axis=1)

        # Filter to self-use posts
        self_use = filter_self_use(merged)
        print(f"Self-use posts: {len(self_use):,} / {len(merged):,}")

        self_use.to_csv("self_use_posts.csv", index=False)
        print("Saved self_use_posts.csv")
    else:
        print(f"Batch failed with status: {batch.status}")
