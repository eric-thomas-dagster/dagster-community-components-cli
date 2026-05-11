# Hacker News RSS — xml_parser variant

Same goal as the [regex_parser HN demo](hn_rss.md) — extract titles +
links from the HN front-page RSS feed — but routed through the proper
XML toolchain: `xml_parser` in `findall` mode (which returns lists of
matches per xpath), then `array_exploder` exploding both columns in
parallel for one row per item.

## Pipeline

```
rest_api_fetcher (text)  → xml_parser (mode: findall)
                         → array_exploder (parallel)
                         → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | GET HN RSS as raw XML (`output_format: text`) |
| 2 | `xml_parser` | transformation | xpath `findall` for `.//item/title` and `.//item/link` — each cell becomes a list of matches |
| 3 | `array_exploder` | transformation | Explode both list-columns in parallel (zip-style) — one row per item |
| 4 | `dataframe_to_csv` | sink | Write `title, link` |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_hn_xml_demo.sh | bash
cd hn-xml-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/hn_xml_frontpage.csv` — same shape as the regex variant, ~20 rows
of HN front-page stories.

## What this demo shows

- **`xml_parser` `mode: findall`** — return all xpath matches as a
  list, not just the first one. Pair with `array_exploder` to get one
  row per match. This was added specifically to enable RSS / Atom feed
  parsing without falling back to regex.
- **`array_exploder` with a list of columns** — pandas's
  `df.explode([col1, col2])` zips parallel lists row-wise. Without
  this, two independent explode calls would Cartesian-product (e.g.,
  20 titles × 20 links = 400 rows of mismatched pairs).
- **Compare to the regex variant** — same output, ~30% less YAML, more
  declarative. The regex demo is still useful when the source isn't
  well-formed XML or when the structure is irregular; xml_parser is the
  right tool when you have a real XML schema to lean on.

## Extending

Swap the xpath expressions to extract `pubDate`, `description`, `dc:creator`
(with a `namespace` mapping for the `dc:` prefix) — `xml_parser` accepts
a dict so adding fields is one YAML line each.
