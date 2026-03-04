"""
Step 4: Generate Tables — Side Effect Frequencies and Sub-Analysis
===================================================================
Produces the main results tables from the paper:

  - Table 1: MedDRA Preferred Term frequencies grouped by System Organ Class
  - Appendix Table 1: Co-occurring PT pairs
  - Appendix Table 2: Semaglutide vs. tirzepatide descriptive comparison
  - Appendix Tables 4-5: HLT and HLGT frequencies

Inputs:
  - Post-level data with MedDRA concepts (output of Step 3)
  - MedDRA hierarchy files: pt.asc, llt.asc, hlt.asc, hlgt.asc, hlt_pt.asc,
    hlgt_hlt.asc, mdhier.asc (MedDRA v28.0)

NOTE: MedDRA files are available under license from https://www.meddra.org/
"""

import itertools
from ast import literal_eval

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency, fisher_exact
from statsmodels.stats.multitest import multipletests

# ============================================================================
# Configuration — adjust paths to your local setup
# ============================================================================

# Post-level data with MedDRA concepts (output of Step 3)
POSTS_FILE = "posts_with_meddra.csv"

# MedDRA ASCII directory
MEDDRA_DIR = "MedDRA_28_0_English/MedAscii"

# Optional: CSV mapping any unresolved MedDRA concept strings to PT codes.
# Columns: meddra_concept, pt_code
# Set to None if not needed.
MANUAL_MAPPINGS_FILE = None

# Output files
OUTPUT_TABLE1 = "table1_pt_by_soc.csv"
OUTPUT_COOCCURRENCE = "appendix_table1_cooccurrence.csv"
OUTPUT_SUBANALYSIS = "appendix_table2_sema_vs_tirz.csv"
OUTPUT_HLT = "appendix_table4_hlt_counts.csv"
OUTPUT_HLGT = "appendix_table5_hlgt_counts.csv"

# ============================================================================
# Load MedDRA hierarchy
# ============================================================================


def load_meddra(meddra_dir):
    """Load MedDRA hierarchy files and return lookup tables."""

    pt = pd.read_csv(
        f"{meddra_dir}/pt.asc", sep="$", header=None, usecols=[0, 1],
        names=["pt_code", "pt_term"], encoding="latin-1",
    )
    hlt = pd.read_csv(
        f"{meddra_dir}/hlt.asc", sep="$", header=None, usecols=[0, 1],
        names=["hlt_code", "hlt_term"], encoding="latin-1",
    )
    hlgt = pd.read_csv(
        f"{meddra_dir}/hlgt.asc", sep="$", header=None, usecols=[0, 1],
        names=["hlgt_code", "hlgt_term"], encoding="latin-1",
    )
    soc = pd.read_csv(
        f"{meddra_dir}/soc.asc", sep="$", header=None, usecols=[0, 1],
        names=["soc_code", "soc_term"], encoding="latin-1",
    )
    hlt_pt = pd.read_csv(
        f"{meddra_dir}/hlt_pt.asc", sep="$", header=None, usecols=[0, 1],
        names=["hlt_code", "pt_code"], encoding="latin-1",
    )
    hlgt_hlt = pd.read_csv(
        f"{meddra_dir}/hlgt_hlt.asc", sep="$", header=None, usecols=[0, 1],
        names=["hlgt_code", "hlt_code"], encoding="latin-1",
    )

    # Full hierarchy with primary SOC flag
    mdhier = pd.read_csv(
        f"{meddra_dir}/mdhier.asc",
        sep="$", header=None, encoding="latin-1", index_col=False,
        names=[
            "pt_code", "hlt_code", "hlgt_code", "soc_code",
            "pt_name", "hlt_name", "hlgt_name", "soc_name",
            "soc_abbrev", "null_field", "pt_soc_code", "primary_soc_fg",
        ],
    )
    # Keep only primary SOC assignments
    mdhier_primary = mdhier[mdhier["primary_soc_fg"] == "Y"].copy()

    return {
        "pt": pt,
        "hlt": hlt,
        "hlgt": hlgt,
        "soc": soc,
        "hlt_pt": hlt_pt,
        "hlgt_hlt": hlgt_hlt,
        "mdhier": mdhier_primary,
    }


# ============================================================================
# Data preparation
# ============================================================================


def load_and_prepare_data(posts_file, meddra_tables, manual_mappings_file=None):
    """
    Load post-level data, aggregate to user level, and map MedDRA concept
    strings to PT codes using the MedDRA hierarchy.
    """
    data = pd.read_csv(posts_file)
    pt_df = meddra_tables["pt"]

    # Parse meddra_concepts from string representation to sets
    def parse_concepts(x):
        if pd.isna(x) or (isinstance(x, str) and not x.strip()):
            return set()
        try:
            return set(literal_eval(x))
        except (ValueError, SyntaxError):
            return set()

    # Use the string column if present, otherwise the set column
    concept_col = "meddra_concepts_str" if "meddra_concepts_str" in data.columns else "meddra_concepts"
    data["meddra_concepts"] = data[concept_col].apply(parse_concepts)

    # Parse medications
    data["medication"] = (
        data["medication"]
        .fillna("")
        .str.split(",")
        .apply(lambda lst: {m.strip() for m in lst if m.strip()})
    )

    # ---- Aggregate to user level ----
    user_level = (
        data.groupby("user_id")
        .agg({
            "meddra_concepts": lambda col: set().union(*col),
            "medication": lambda col: set().union(*col),
        })
        .reset_index()
    )

    # ---- Map concept strings → PT codes ----
    # Build name→code lookup from MedDRA PT table
    pt_name_to_code = dict(
        zip(pt_df["pt_term"].str.lower(), pt_df["pt_code"])
    )

    # Load manual mappings if provided
    manual_map = {}
    if manual_mappings_file:
        manual_df = pd.read_csv(manual_mappings_file)
        manual_map = dict(
            zip(manual_df["meddra_concept"].str.lower(), manual_df["pt_code"])
        )

    def concepts_to_pt_codes(concept_set):
        codes = set()
        for concept in concept_set:
            key = concept.strip().lower()
            if key in pt_name_to_code:
                codes.add(pt_name_to_code[key])
            elif key in manual_map:
                codes.add(manual_map[key])
            # else: concept could not be mapped (logged during development)
        return codes

    user_level["pt_codes"] = user_level["meddra_concepts"].apply(concepts_to_pt_codes)

    # ---- Map PT codes → HLT, HLGT, SOC codes via hierarchy ----
    mdhier = meddra_tables["mdhier"]
    pt2soc = mdhier.groupby("pt_code")["soc_code"].apply(set).to_dict()
    pt2hlt = mdhier.groupby("pt_code")["hlt_code"].apply(set).to_dict()
    pt2hlgt = mdhier.groupby("pt_code")["hlgt_code"].apply(set).to_dict()

    def map_codes(pt_set, lookup):
        result = set()
        for pt_code in pt_set:
            if pt_code in lookup:
                result.update(lookup[pt_code])
        return result

    user_level["soc_codes"] = user_level["pt_codes"].apply(lambda s: map_codes(s, pt2soc))
    user_level["hlt_codes"] = user_level["pt_codes"].apply(lambda s: map_codes(s, pt2hlt))
    user_level["hlgt_codes"] = user_level["pt_codes"].apply(lambda s: map_codes(s, pt2hlgt))

    # Exclude users whose only PT is "Weight decreased" (PT code 10047895)
    WEIGHT_DECREASED = 10047895
    user_level = user_level[
        ~(user_level["pt_codes"].apply(lambda s: s == {WEIGHT_DECREASED}))
    ]

    # Keep only users with at least one PT
    user_level = user_level[user_level["pt_codes"].apply(len) > 0]

    return user_level, pt2soc, pt2hlt, pt2hlgt


# ============================================================================
# Table 1: PT frequencies grouped by SOC
# ============================================================================


def generate_table1(user_level, meddra_tables, pt2soc):
    """
    Generate Table 1: MedDRA Preferred Terms grouped by primary System Organ
    Class, with user counts and percentages.
    """
    n_users = len(user_level)
    pt_df = meddra_tables["pt"]
    soc_df = meddra_tables["soc"]

    # Count users per PT
    pt_counts = (
        user_level.explode("pt_codes", ignore_index=True)
        .dropna(subset=["pt_codes"])
        .value_counts(subset=["pt_codes"])
        .rename("n_users")
        .reset_index()
        .rename(columns={"pt_codes": "pt_code"})
    )
    pt_counts = pt_counts.merge(pt_df, how="left", on="pt_code")
    pt_counts["percent_users"] = np.round(100 * pt_counts["n_users"] / n_users, 1)

    # Count users per SOC
    soc_counts = (
        user_level.explode("soc_codes", ignore_index=True)
        .dropna(subset=["soc_codes"])
        .value_counts(subset=["soc_codes"])
        .rename("n_users")
        .reset_index()
        .rename(columns={"soc_codes": "soc_code"})
    )
    soc_counts = soc_counts.merge(soc_df, how="left", on="soc_code")
    soc_counts["soc_percent"] = np.round(100 * soc_counts["n_users"] / n_users, 1)

    # Join PT → SOC
    map_rows = pd.DataFrame([
        {"pt_code": pt, "soc_code": soc}
        for pt, soc_set in pt2soc.items()
        for soc in soc_set
    ])
    pt_with_soc = pt_counts.merge(map_rows, on="pt_code", how="left")
    soc_lookup = soc_counts[["soc_code", "soc_term", "n_users"]].rename(
        columns={"n_users": "soc_n_users"}
    )
    pt_with_soc = pt_with_soc.merge(soc_lookup, on="soc_code", how="left")

    # Sort: SOCs by descending user count, PTs within SOC by descending count
    pt_with_soc = pt_with_soc.sort_values(
        ["soc_n_users", "n_users"], ascending=[False, False]
    ).reset_index(drop=True)

    # Filter to PTs with ≥ 0.5% prevalence (and SOCs with ≥ 1.0%)
    pt_with_soc = pt_with_soc[pt_with_soc["percent_users"] >= 0.5]

    return pt_with_soc, pt_counts, soc_counts


# ============================================================================
# HLT and HLGT frequency tables
# ============================================================================


def generate_hlt_hlgt_tables(user_level, meddra_tables):
    """Generate HLT and HLGT frequency tables (Appendix Tables 4-5)."""
    n_users = len(user_level)

    hlt_counts = (
        user_level.explode("hlt_codes", ignore_index=True)
        .dropna(subset=["hlt_codes"])
        .value_counts(subset=["hlt_codes"])
        .rename("n_users")
        .reset_index()
        .rename(columns={"hlt_codes": "hlt_code"})
    )
    hlt_counts = hlt_counts.merge(meddra_tables["hlt"], how="left", on="hlt_code")
    hlt_counts["percent_users"] = np.round(100 * hlt_counts["n_users"] / n_users, 1)

    hlgt_counts = (
        user_level.explode("hlgt_codes", ignore_index=True)
        .dropna(subset=["hlgt_codes"])
        .value_counts(subset=["hlgt_codes"])
        .rename("n_users")
        .reset_index()
        .rename(columns={"hlgt_codes": "hlgt_code"})
    )
    hlgt_counts = hlgt_counts.merge(meddra_tables["hlgt"], how="left", on="hlgt_code")
    hlgt_counts["percent_users"] = np.round(100 * hlgt_counts["n_users"] / n_users, 1)

    return hlt_counts, hlgt_counts


# ============================================================================
# Co-occurrence analysis (Appendix Table 1)
# ============================================================================


def generate_cooccurrence(user_level, meddra_tables, top_n=20):
    """
    Compute co-occurrence of PT pairs among users.

    Returns the top_n most frequent PT pairs.
    """
    pt_df = meddra_tables["pt"]
    code_to_name = dict(zip(pt_df["pt_code"], pt_df["pt_term"]))

    pair_counts = {}
    for _, row in user_level.iterrows():
        codes = sorted(row["pt_codes"])
        for a, b in itertools.combinations(codes, 2):
            pair = (a, b)
            pair_counts[pair] = pair_counts.get(pair, 0) + 1

    pairs_df = pd.DataFrame([
        {
            "pt_code_1": k[0],
            "pt_code_2": k[1],
            "pt_term_1": code_to_name.get(k[0], ""),
            "pt_term_2": code_to_name.get(k[1], ""),
            "n_users": v,
        }
        for k, v in pair_counts.items()
    ])
    pairs_df = pairs_df.sort_values("n_users", ascending=False).head(top_n)
    pairs_df["percent_users"] = np.round(
        100 * pairs_df["n_users"] / len(user_level), 1
    )

    return pairs_df


# ============================================================================
# Sub-analysis: Semaglutide vs. Tirzepatide (Appendix Table 2)
# ============================================================================

# Canonical medication name sets — representative sample.
# NOTE: The full study used exhaustive lists of 300+ semaglutide variants and
# 250+ tirzepatide variants (including all misspellings, compound formulations,
# and brand-name variations extracted by the GPT-4o-mini classifier in Step 2).
# We manually reviewed every unique medication string produced by the model to
# ensure complete coverage. The lists below are illustrative; in practice, every
# extracted name was checked and classified.
SEMAGLUTIDE_NAMES = {
    "ozempic", "wegovy", "rybelsus", "semaglutide",
    # Common misspellings (sample)
    "ozempick", "ozenpic", "ozeampic", "ozzempic", "ozempik", "osempic",
    "ozepmic", "ozeempic", "ozempec",
    "wegovi", "weegovy", "wegowvy", "wgovy", "wigovy", "weggovy", "weegovi",
    "rybelsos", "ryblesus", "rybeslus", "rybelsu", "ribelsus", "rybelssus",
    "rybelsis", "ryeblsus", "ribelsis",
    "semaglutid", "semagludite", "semaglitude", "semagltide", "semaglutied",
    "samaglutide", "semaglutde", "semagultide", "semglutide", "semaglitide",
    # Compound / compounded
    "compounded semaglutide", "compound semaglutide",
    # Oral
    "oral semaglutide",
}

TIRZEPATIDE_NAMES = {
    "mounjaro", "zepbound", "tirzepatide",
    # Common misspellings (sample)
    "mounjoro", "mounjro", "mounjrao", "mounjarro", "monjaro", "mounjard",
    "moonjaro", "mounjaroo", "munjaro",
    "zepbond", "zepboud", "zepboun", "zebpound", "zepound", "zeppound",
    "zebound", "zepboudn", "zepboind",
    "tirzeptide", "tirzepetide", "tirzapatide", "tirzepatid", "trizepatide",
    "tirzapatiede", "tirzeptid", "tirzepatiede", "tirzepatyde",
    # Compound / compounded
    "compounded tirzepatide", "compound tirzepatide",
}


def classify_user_drug(medication_set):
    """
    Classify a user as semaglutide-only, tirzepatide-only, both, or other
    based on the set of medications they mentioned across all posts.
    """
    meds_lower = {m.strip().lower() for m in medication_set}
    has_sema = bool(meds_lower & SEMAGLUTIDE_NAMES)
    has_tirz = bool(meds_lower & TIRZEPATIDE_NAMES)

    if has_sema and has_tirz:
        return "both"
    elif has_sema:
        return "semaglutide"
    elif has_tirz:
        return "tirzepatide"
    else:
        return "other"


def generate_subanalysis(user_level, meddra_tables):
    """
    Descriptive comparison of PT frequencies between users who exclusively
    mentioned semaglutide vs. tirzepatide.

    Also performs exploratory chi-squared / Fisher's exact tests with
    FDR correction (Benjamini-Hochberg, alpha=0.05).
    """
    pt_df = meddra_tables["pt"]

    user_level = user_level.copy()
    user_level["drug_class"] = user_level["medication"].apply(classify_user_drug)

    sema_users = user_level[user_level["drug_class"] == "semaglutide"]
    tirz_users = user_level[user_level["drug_class"] == "tirzepatide"]

    n_sema = len(sema_users)
    n_tirz = len(tirz_users)
    print(f"Semaglutide-only users: {n_sema:,}")
    print(f"Tirzepatide-only users: {n_tirz:,}")

    # Count users per PT for each drug class
    def count_pts(subset):
        return (
            subset.explode("pt_codes", ignore_index=True)
            .dropna(subset=["pt_codes"])
            .value_counts(subset=["pt_codes"])
            .rename("n_users")
            .reset_index()
            .rename(columns={"pt_codes": "pt_code"})
        )

    sema_counts = count_pts(sema_users).rename(columns={"n_users": "n_sema"})
    tirz_counts = count_pts(tirz_users).rename(columns={"n_users": "n_tirz"})

    comparison = sema_counts.merge(tirz_counts, on="pt_code", how="outer").fillna(0)
    comparison["n_sema"] = comparison["n_sema"].astype(int)
    comparison["n_tirz"] = comparison["n_tirz"].astype(int)
    comparison = comparison.merge(pt_df, on="pt_code", how="left")

    comparison["pct_sema"] = np.round(100 * comparison["n_sema"] / n_sema, 1)
    comparison["pct_tirz"] = np.round(100 * comparison["n_tirz"] / n_tirz, 1)

    # ---- Statistical testing (exploratory) ----
    def test_association(row):
        a = int(row["n_sema"])
        b = n_sema - a
        c = int(row["n_tirz"])
        d = n_tirz - c
        table = [[a, b], [c, d]]
        grand = a + b + c + d
        exp_a = (a + b) * (a + c) / grand if grand > 0 else 0

        if min(exp_a, (c + d) * (a + c) / grand if grand > 0 else 0) < 5:
            odds_ratio, p_value = fisher_exact(table, alternative="two-sided")
            test_used = "fisher"
        else:
            chi2, p_value, dof, expected = chi2_contingency(table)
            odds_ratio = (a * d) / (b * c) if b > 0 and c > 0 else float("nan")
            test_used = "chi2"

        return pd.Series({
            "p_value": p_value,
            "odds_ratio": odds_ratio,
            "test_used": test_used,
        })

    stats = comparison.apply(test_association, axis=1)
    comparison = pd.concat([comparison, stats], axis=1)

    # FDR correction (Benjamini-Hochberg)
    reject, pvals_corrected, _, _ = multipletests(
        comparison["p_value"], alpha=0.05, method="fdr_bh"
    )
    comparison["p_value_corrected"] = pvals_corrected
    comparison["significant_fdr"] = reject

    comparison = comparison.sort_values("p_value_corrected")
    return comparison


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    # Load MedDRA hierarchy
    meddra = load_meddra(MEDDRA_DIR)
    print("Loaded MedDRA hierarchy")

    # Load and prepare user-level data
    user_level, pt2soc, pt2hlt, pt2hlgt = load_and_prepare_data(
        POSTS_FILE, meddra, MANUAL_MAPPINGS_FILE
    )
    n_users = len(user_level)
    print(f"Users with ≥1 side effect (excl. weight decreased only): {n_users:,}")
    print(
        f"Mean PTs per user: {user_level['pt_codes'].apply(len).mean():.1f} "
        f"(SD {user_level['pt_codes'].apply(len).std():.1f})"
    )

    # ---- Table 1: PT frequencies by SOC ----
    table1, pt_counts, soc_counts = generate_table1(user_level, meddra, pt2soc)
    table1.to_csv(OUTPUT_TABLE1, index=False)
    print(f"\nTable 1 saved to {OUTPUT_TABLE1}")
    print(f"  {len(table1)} PTs with ≥0.5% prevalence across "
          f"{table1['soc_term'].nunique()} SOCs")

    # ---- Appendix Tables 4-5: HLT and HLGT ----
    hlt_counts, hlgt_counts = generate_hlt_hlgt_tables(user_level, meddra)
    hlt_counts.to_csv(OUTPUT_HLT, index=False)
    hlgt_counts.to_csv(OUTPUT_HLGT, index=False)
    print(f"HLT counts saved to {OUTPUT_HLT}")
    print(f"HLGT counts saved to {OUTPUT_HLGT}")

    # ---- Appendix Table 1: Co-occurrence ----
    cooccurrence = generate_cooccurrence(user_level, meddra, top_n=20)
    cooccurrence.to_csv(OUTPUT_COOCCURRENCE, index=False)
    print(f"\nCo-occurrence table saved to {OUTPUT_COOCCURRENCE}")
    print(f"  Top pair: {cooccurrence.iloc[0]['pt_term_1']} & "
          f"{cooccurrence.iloc[0]['pt_term_2']} "
          f"({cooccurrence.iloc[0]['n_users']:,} users, "
          f"{cooccurrence.iloc[0]['percent_users']}%)")

    # ---- Appendix Table 2: Semaglutide vs. Tirzepatide ----
    subanalysis = generate_subanalysis(user_level, meddra)
    subanalysis.to_csv(OUTPUT_SUBANALYSIS, index=False)
    print(f"\nSub-analysis saved to {OUTPUT_SUBANALYSIS}")
    n_sig = subanalysis["significant_fdr"].sum()
    print(f"  {n_sig} PTs significant after FDR correction")

    print("\nDone.")
