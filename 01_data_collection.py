"""
Step 1: Data Collection and Filtering
======================================
Collects Reddit posts and comments from relevant subreddits mentioning
semaglutide or tirzepatide (including common misspellings), then applies
quality filters.

Data source: Pushshift / Arctic Shift (https://github.com/ArthurHeitmann/arctic_shift)
Period: January 2015 – June 2025 (posts meeting criteria appeared May 2019 – June 2025)

NOTE: This script documents the collection and filtering logic. The actual data
retrieval depends on access to the Pushshift/Arctic Shift data archives or a
local database mirror. Adapt the data loading section to your data source.
"""

import pandas as pd
import re

# ============================================================================
# Configuration
# ============================================================================

SUBREDDITS = [
    "mounjaro",
    "ozempic",
    "fasting",
    "intermittentfasting",
    "keto",
    "loseit",
    "Semaglutide",
    "SuperMorbidlyObese",
    "PlusSize",
]

# Drug names and common misspellings used to identify relevant posts
DRUG_TERMS = [
    # Ozempic (semaglutide injection)
    "ozempic", "ozempick", "ozenpic", "ozeampic", "ozzempic",
    "ozempik", "osempic", "ozepmic", "ozeempic", "ozempec",
    # Mounjaro (tirzepatide injection)
    "mounjaro", "mounjoro", "mounjro", "mounjrao", "mounjarro",
    "monjaro", "mounjard", "moonjaro", "mounjaroo", "munjaro",
    # Semaglutide (generic)
    "semaglutide", "semaglutid", "semagludite", "semaglitude",
    "semagltide", "semaglutied", "samaglutide", "semaglutde",
    "semagultide", "semglutide", "semaglitide",
    # Tirzepatide (generic)
    "tirzepatide", "tirzeptide", "tirzepetide", "tirzapatide",
    "tirzepatid", "trizepatide", "tirzapatiede", "tirzeptid",
    "tirzepatiede", "tirzepatyde",
    # Wegovy (semaglutide injection)
    "wegovy", "wegovi", "weegovy", "wegowvy", "wgovy",
    "wigovy", "weggovy", "weegovi",
    # Rybelsus (oral semaglutide)
    "rybelsus", "rybelsos", "ryblesus", "rybeslus", "rybelsu",
    "ribelsus", "rybelssus", "rybelsis", "ryeblsus", "ribelsis",
    # Zepbound (tirzepatide injection)
    "zepbound", "zepbond", "zepboud", "zepboun", "zebpound",
    "zepound", "zeppound", "zebound", "zepboudn", "zepboind",
]

MIN_WORD_COUNT = 10
MIN_ALPHA_RATIO = 0.50

# ============================================================================
# Data Loading (adapt to your data source)
# ============================================================================


def load_reddit_data(subreddits, start_date, end_date):
    """
    Load Reddit posts and comments from the specified subreddits and date range.

    This is a placeholder — replace with your actual data loading logic
    (e.g., reading from Pushshift/Arctic Shift NDJSON files, a database, etc.).

    Expected columns for comments: user_id, subreddit, message, date
    Expected columns for posts:    user_id, subreddit, title, message, date

    Returns:
        comments_df (pd.DataFrame), posts_df (pd.DataFrame)
    """
    raise NotImplementedError(
        "Replace this function with your data loading logic. "
        "See Pushshift (https://github.com/pushshift/api) or "
        "Arctic Shift (https://github.com/ArthurHeitmann/arctic_shift)."
    )


# ============================================================================
# Filtering
# ============================================================================


def filter_comments(df):
    """Apply quality filters to comments DataFrame."""
    filtered = df.copy()

    # Remove deleted/removed content
    filtered = filtered[~filtered["message"].isin(["[removed]", "[deleted]"])]
    filtered = filtered.dropna(subset=["message"])

    # Minimum word count
    filtered = filtered[
        filtered["message"].str.split().str.len() >= MIN_WORD_COUNT
    ]

    # Must contain spaces (not a single token)
    filtered = filtered[
        filtered["message"].str[:2048].str.contains(" ")
    ]

    # At least 50% alphabetic characters
    filtered = filtered[
        filtered["message"].apply(
            lambda x: sum(c.isalpha() for c in x) / max(len(x), 1) >= MIN_ALPHA_RATIO
        )
    ]

    # Exclude bot accounts
    filtered = filtered[
        ~filtered["user_id"].str.contains("bot", case=False, na=False)
    ]

    return filtered


def filter_posts(df):
    """Apply quality filters to posts (submissions) DataFrame."""
    filtered = df.copy()

    # Remove deleted/removed content
    filtered = filtered[~filtered["message"].isin(["[removed]", "[deleted]"])]
    filtered = filtered.dropna(subset=["title"])
    filtered = filtered.dropna(subset=["message"])

    # Title must contain spaces
    filtered = filtered[
        filtered["title"].str[:2048].str.contains(" ")
    ]

    # Combine title and message for length/quality checks
    filtered["combined_title_message"] = filtered["title"] + " " + filtered["message"]

    # Minimum word count
    filtered = filtered[
        filtered["combined_title_message"].str.split().str.len() >= MIN_WORD_COUNT
    ]

    # At least 50% alphabetic characters
    filtered = filtered[
        filtered["combined_title_message"].apply(
            lambda x: sum(c.isalpha() for c in x) / max(len(x), 1) >= MIN_ALPHA_RATIO
        )
    ]

    # Exclude bot accounts
    filtered = filtered[
        ~filtered["user_id"].str.contains("bot", case=False, na=False)
    ]

    return filtered


def filter_drug_mentions(df, text_column="message"):
    """Keep only rows that mention at least one drug name."""
    pattern = "|".join(re.escape(term) for term in DRUG_TERMS)
    return df[df[text_column].str.contains(pattern, case=False, na=False)]


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    # Load data (replace with your actual date range / data source)
    comments, posts = load_reddit_data(
        subreddits=SUBREDDITS,
        start_date="2015-01-01",
        end_date="2025-06-30",
    )

    print(f"Raw comments: {len(comments):,}")
    print(f"Raw posts:    {len(posts):,}")

    # Apply quality filters
    comments = filter_comments(comments)
    posts = filter_posts(posts)
    print(f"After quality filters — comments: {len(comments):,}, posts: {len(posts):,}")

    # Keep only posts/comments mentioning a drug
    comments = filter_drug_mentions(comments, text_column="message")
    posts = filter_drug_mentions(posts, text_column="combined_title_message")
    print(f"After drug-mention filter — comments: {len(comments):,}, posts: {len(posts):,}")
    print(f"Total posts + comments: {len(comments) + len(posts):,}")

    # Save for next step
    all_posts = pd.concat(
        [
            comments[["user_id", "subreddit", "message", "date"]],
            posts[["user_id", "subreddit", "combined_title_message", "date"]].rename(
                columns={"combined_title_message": "message"}
            ),
        ],
        ignore_index=True,
    )
    all_posts.to_csv("filtered_posts.csv", index=False)
    print(f"Saved {len(all_posts):,} rows to filtered_posts.csv")
