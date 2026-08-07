"""data_engineering — a real data pipeline as ONE Dagster+ Serverless code location.

The straight-data-engineering counterpart to `agentic_tour_serverless/` —
no LLMs, no API keys, no vended products. Everything meaningful in this
one file. Deploy with:

    uvx --with pex --from dagster-cloud-cli dagster-cloud serverless \\
        deploy-python-executable . \\
        --location-name data-engineering \\
        --module-name data_engineering.definitions \\
        --python-version 3.12

Beyond the Dagster+ token itself (needed for any deploy), no vended-
product credentials required at deploy or runtime. Assets fetch from
the public Hacker News API (Firebase-backed, unauthenticated) and land
in a project-relative DuckDB file — durable locally, ephemeral on
Serverless (swap the sink for a warehouse `table_sinks` for production).

## Pipeline shape

  hn_top_story_ids ────► hn_stories ─┬─► hn_leaderboard    (top 20 by score → CSV)
                                     ├─► hn_domains         (top 20 domains → CSV)
                                     └─► hn_warehouse       (DuckDB fact table)

Every step is a browsable Dagster asset with typed metadata (row counts,
timestamps, fetch latency). Filter the catalog by kind. Rerun any single
asset without touching upstreams. This is the "why Dagster's assets model"
story on a shape any data engineer recognizes.
"""
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests
import dagster as dg


HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
OUT_DIR = Path("out")
OUT_DIR.mkdir(exist_ok=True)


# ── 1. fetch top-story IDs ──────────────────────────────────────────

@dg.asset(
    group_name="ingest",
    kinds={"api", "hackernews"},
    description="Fetch the current top-500 story IDs from the HN public API.",
)
def hn_top_story_ids(context: dg.AssetExecutionContext):
    t0 = time.time()
    r = requests.get(HN_TOP_URL, timeout=10)
    r.raise_for_status()
    ids = r.json()
    context.add_output_metadata({
        "n_ids": dg.MetadataValue.int(len(ids)),
        "fetch_latency_ms": dg.MetadataValue.int(int((time.time() - t0) * 1000)),
        "fetched_at": dg.MetadataValue.timestamp(datetime.now(timezone.utc)),
        "sample_ids": dg.MetadataValue.json(ids[:5]),
    })
    return ids


# ── 2. fetch story details ──────────────────────────────────────────

@dg.asset(
    group_name="ingest",
    kinds={"api", "hackernews"},
    description="Fetch full metadata for the top 100 stories (title, url, score, author, timestamp).",
)
def hn_stories(context: dg.AssetExecutionContext, hn_top_story_ids) -> pd.DataFrame:
    t0 = time.time()
    n = 100
    rows = []
    for i, sid in enumerate(hn_top_story_ids[:n]):
        r = requests.get(HN_ITEM_URL.format(id=sid), timeout=10)
        if r.status_code != 200:
            continue
        item = r.json() or {}
        rows.append({
            "id": item.get("id"),
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "score": item.get("score") or 0,
            "by": item.get("by") or "",
            "type": item.get("type") or "",
            "descendants": item.get("descendants") or 0,   # comment count
            "time": item.get("time"),
        })
        if (i + 1) % 25 == 0:
            context.log.info(f"fetched {i + 1}/{n} items")

    df = pd.DataFrame(rows)
    # UNIX epoch → ISO for readability
    if not df.empty and "time" in df.columns:
        df["submitted_at"] = pd.to_datetime(df["time"], unit="s", utc=True)

    context.add_output_metadata({
        "n_stories": dg.MetadataValue.int(len(df)),
        "fetch_latency_ms": dg.MetadataValue.int(int((time.time() - t0) * 1000)),
        "median_score": dg.MetadataValue.int(int(df["score"].median()) if not df.empty else 0),
        "max_score": dg.MetadataValue.int(int(df["score"].max()) if not df.empty else 0),
        "n_with_url": dg.MetadataValue.int(int(df["url"].astype(bool).sum())),
        "sample": dg.MetadataValue.md(
            df[["id", "title", "score", "by"]].head(5).to_markdown(index=False)
            if not df.empty else "_(empty)_"
        ),
    })
    return df


# ── 3. leaderboard (top 20 by score) ────────────────────────────────

@dg.asset(
    group_name="transform",
    kinds={"pandas", "transform"},
    description="Top 20 HN stories by score. Written to out/hn_leaderboard.csv.",
)
def hn_leaderboard(context: dg.AssetExecutionContext, hn_stories: pd.DataFrame) -> pd.DataFrame:
    top = hn_stories.sort_values("score", ascending=False).head(20).reset_index(drop=True)
    out_path = OUT_DIR / "hn_leaderboard.csv"
    top.to_csv(out_path, index=False)

    context.add_output_metadata({
        "n_rows": dg.MetadataValue.int(len(top)),
        "top_score": dg.MetadataValue.int(int(top["score"].max()) if not top.empty else 0),
        "csv_path": dg.MetadataValue.path(str(out_path.absolute())),
        "leaderboard": dg.MetadataValue.md(
            top[["title", "score", "by"]].to_markdown(index=False)
            if not top.empty else "_(empty)_"
        ),
    })
    return top


# ── 4. domain aggregation ───────────────────────────────────────────

@dg.asset(
    group_name="transform",
    kinds={"pandas", "aggregation"},
    description="Top 20 domains by number of stories on HN's front page right now.",
)
def hn_domains(context: dg.AssetExecutionContext, hn_stories: pd.DataFrame) -> pd.DataFrame:
    df = hn_stories.copy()
    df["domain"] = df["url"].apply(lambda u: (urlparse(u).netloc or "").lstrip("www."))
    df = df[df["domain"] != ""]

    agg = (
        df.groupby("domain")
        .agg(n_stories=("id", "count"), median_score=("score", "median"))
        .reset_index()
        .sort_values("n_stories", ascending=False)
        .head(20)
    )
    agg["median_score"] = agg["median_score"].astype(int)
    out_path = OUT_DIR / "hn_domains.csv"
    agg.to_csv(out_path, index=False)

    context.add_output_metadata({
        "n_domains": dg.MetadataValue.int(len(agg)),
        "csv_path": dg.MetadataValue.path(str(out_path.absolute())),
        "leaderboard": dg.MetadataValue.md(
            agg.to_markdown(index=False)
            if not agg.empty else "_(empty)_"
        ),
    })
    return agg


# ── 5. warehouse-style sink (DuckDB) ────────────────────────────────

@dg.asset(
    group_name="sink",
    kinds={"duckdb", "warehouse"},
    description="Persist all fetched stories to a local DuckDB fact table. Swap for Snowflake/BigQuery/etc. in prod via a `table_sinks` component.",
)
def hn_warehouse(context: dg.AssetExecutionContext, hn_stories: pd.DataFrame) -> None:
    import duckdb

    db_path = OUT_DIR / "hn.duckdb"
    con = duckdb.connect(str(db_path))
    con.register("df", hn_stories)
    con.execute("CREATE OR REPLACE TABLE fact_hn_stories AS SELECT * FROM df")
    n = con.execute("SELECT COUNT(*) FROM fact_hn_stories").fetchone()[0]
    con.close()

    context.add_output_metadata({
        "n_rows_loaded": dg.MetadataValue.int(int(n)),
        "db_path": dg.MetadataValue.path(str(db_path.absolute())),
        "loaded_at": dg.MetadataValue.timestamp(datetime.now(timezone.utc)),
    })


# ── Definitions ─────────────────────────────────────────────────────

defs = dg.Definitions(
    assets=[hn_top_story_ids, hn_stories, hn_leaderboard, hn_domains, hn_warehouse],
)
