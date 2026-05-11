# Image EXIF — synthetic JPEGs with injected EXIF → flat DataFrame

**Validated end-to-end** (pure Python; Pillow + piexif). 3 synthetic JPEGs with injected camera/GPS/capture metadata → flat DataFrame with all EXIF columns extracted.

```
sample_images       ← synthetic_image_generator (3 JPEGs, inject_exif: true)
       │
       └── image_metadata  ← image_exif_extractor
```

## Components covered (2)

| Component | What it does |
|---|---|
| `synthetic_image_generator` | Generates sample images. With `inject_exif: true` it writes JPEGs (not PNGs) and embeds realistic EXIF: Make/Model/DateTimeOriginal/ISO/FNumber/ExposureTime/FocalLength/GPS. GPS lat/lon nudges per image so each is distinct. |
| `image_exif_extractor` | Pillow-based EXIF extraction. Adds `exif_make`, `exif_model`, `exif_datetime_original`, `exif_iso`, `exif_focal_length_mm`, `exif_exposure_time`, `exif_f_number`, `exif_orientation`, `exif_gps_lat`, `exif_gps_lon`, `exif_gps_altitude_m`, `exif_raw` (full dict), `exif_width`/`height`. |

## Live output

| sku | exif_make | exif_model | exif_iso | exif_focal_length_mm | exif_gps_lat | exif_gps_lon |
|---|---|---|---|---|---|---|
| FRUIT-1 | DagsterCam | DG-1 | 200.0 | 35.0 | 37.7749 | -122.419397 |
| VEH-1 | DagsterCam | DG-1 | 200.0 | 35.0 | 37.7759 | -122.418397 |
| PLANT-1 | DagsterCam | DG-1 | 200.0 | 35.0 | 37.7769 | -122.417400 |

Real-world note: actual camera EXIF varies widely (Canon vs iPhone vs DSLR all use slightly different tag sets). The component falls back gracefully on missing tags — every missing field is `None` rather than an error.

## Use cases

| Goal | Pattern |
|---|---|
| **PII / compliance**: strip GPS before publishing user photos | Check `exif_gps_lat is not None` → route to redaction |
| **ML training filter**: filter by camera type, ISO range, focal length | `WHERE exif_iso < 800 AND exif_make = 'Canon'` |
| **Asset library sorting**: group photos by location / date | `GROUP BY DATE(exif_datetime_original), ROUND(exif_gps_lat, 2)` |
| **Audit camera fleet** | `GROUP BY exif_make, exif_model` for inventory |

## EXIF tags surfaced

| Column | EXIF tag |
|---|---|
| `exif_make` | `Make` |
| `exif_model` | `Model` |
| `exif_software` | `Software` |
| `exif_datetime_original` | `DateTimeOriginal` (when shot) |
| `exif_iso` | `ISOSpeedRatings` / `PhotographicSensitivity` |
| `exif_focal_length_mm` | `FocalLength` (rational → float) |
| `exif_exposure_time` | `ExposureTime` |
| `exif_f_number` | `FNumber` |
| `exif_orientation` | `Orientation` (1-8, used for auto-rotate) |
| `exif_gps_lat` / `exif_gps_lon` | `GPSInfo` (DMS rationals → signed decimal degrees) |
| `exif_gps_altitude_m` | `GPSInfo.GPSAltitude` |
| `exif_raw` | Full dict of every parsed tag |

## Run it

```bash
./setup_image_exif_demo.sh
cd image-exif-demo
uv run dg launch --assets '*'
```
