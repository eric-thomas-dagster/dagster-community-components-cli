# US airports — DBSCAN spatial clustering

A 3-component pipeline that pulls 3,376 US airports (vega-datasets), runs
DBSCAN spatial clustering with a 50km neighborhood radius, writes each
airport's cluster ID. DBSCAN finds dense regions (major metros) and labels
isolated airports as noise (-1).

## Pipeline

```
csv_file_ingestion → spatial_cluster → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Pull vega's airports.csv (~3.4k US airports + lat/lng) |
| 2 | `spatial_cluster` | analytics | DBSCAN with `eps_km=50` and `min_samples=5`; uses haversine distance |
| 3 | `dataframe_to_csv` | sink | Write `iata`, `city`, `state`, `cluster_id` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_airports_cluster_demo.sh | bash
cd airports-cluster-demo
uv run dg launch --assets '*'
```

## Output

```
Total airports: 3376
Noise (rural/isolated): 1229
Distinct metro clusters: 90

Top metro clusters:
  cluster 3:  370 airports — Midwest corridor (OH/PA/WV)
  cluster 2:  306 airports — Southeast (MS/AL/GA)
  cluster 5:  173 airports — Northeast (NY/MA/CT)
  cluster 13: 112 airports — Plains (MO/IA/KS)
  cluster 11: 105 airports — Oklahoma corridor
```

## What this demo shows

- **First geo-spatial demo.** `spatial_cluster` uses sklearn's DBSCAN
  with haversine distance — `eps_km` is in real kilometers, not feature
  space.
- **DBSCAN finds density, not k.** Unlike k-means, you don't say "give me
  90 clusters" — DBSCAN discovers them. Tune `eps_km` and `min_samples`
  to control how aggressively it merges.
- **Noise (cluster_id = -1)** is a feature, not a bug. It identifies
  airports that don't belong to any dense region — useful for finding
  rural / isolated locations.
