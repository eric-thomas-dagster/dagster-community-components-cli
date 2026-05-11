# US Cities — pairwise haversine distances

A 6-component pipeline that takes 10 major US cities, cross-joins to
itself for all 100 pairs, computes haversine distance per pair, drops
self-pairs, sorts shortest-first, writes a CSV.

## Pipeline

```
csv_file_ingestion ─┐
                     ├─→ dataframe_join (cross)
csv_file_ingestion ─┘   → distance_calculator → filter
                                              → sort
                                              → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1a / 1b | `csv_file_ingestion` × 2 | ingestion | Same CSV, two distinct assets so the cross join has two inputs |
| 2 | `dataframe_join` | transformation | `how: cross` — Cartesian product (10 × 10 = 100 pairs); `suffixes: [_origin, _dest]` |
| 3 | `distance_calculator` | analytics | Haversine distance from `lat_origin/lng_origin` → `lat_dest/lng_dest` in km |
| 4 | `filter` | transformation | Drop rows where origin == dest |
| 5 | `sort` | transformation | Ascending by `distance_km` |
| 6 | `dataframe_to_csv` | sink | Write `city_origin, city_dest, distance_km` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_cities_distance_demo.sh | bash
cd cities-distance-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/city_distances.csv` — 90 pairs (100 cross-product minus 10
self-pairs), sorted shortest-first. Sample:

```
city_origin,city_dest,distance_km
New York,Philadelphia,129.61
Philadelphia,New York,129.61
San Diego,Los Angeles,179.41
...
San Francisco,New York,4129.09
New York,San Francisco,4129.09
```

NY-Philly the closest at ~130km, NY-SF the farthest at ~4,129km.

## What this demo shows

- **`dataframe_join` with `how: cross`** — Cartesian product, no join
  key needed. Useful for distance matrices, all-pairs comparisons,
  scenario sweeps. The same component handles inner / left / right /
  outer joins via the `how` field.
- **`distance_calculator` haversine math.** Real spherical distance
  using lat/lng — accurate at any scale. Other formulas: `manhattan`,
  `euclidean` (for projected coordinates), and unit conversion to
  miles or meters via the `unit` field.
- **`suffixes: [_origin, _dest]`** — keeps the join's left/right
  columns distinguishable so downstream `lat_origin` and `lat_dest`
  are unambiguous.

## Extending

Cap the search to "near each origin" by filtering `distance_km < 1000`
before sort. Or replace the cross-join with `dataframe_join how=inner`
on a `state` column to compute only intra-state pairs.
