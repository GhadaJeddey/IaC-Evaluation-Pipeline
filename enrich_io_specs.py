"""
Enrich pricing_clean.csv with max Network I/O (Gbps) and max Disk I/O (MB/s)
sourced from the AWS Bulk Pricing API (us-east-1 regional JSON).

Columns added:
  max_network_io_gbps  – upper bound of the instance's advertised network bandwidth
  max_disk_io_mbps     – maximum dedicated EBS throughput in MB/s
"""

import re
import json
import urllib.request
import pandas as pd

# ---------------------------------------------------------------------------
# 1. Fetch the AWS Bulk Pricing JSON for us-east-1
#    Instance specs (network, storage) are region-independent; one region suffices.
# ---------------------------------------------------------------------------
BULK_URL = (
    "https://pricing.us-east-1.amazonaws.com"
    "/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.json"
)

print(f"Fetching bulk pricing data from AWS …")
print(f"  URL: {BULK_URL}")
print("  (this is ~100 MB – may take a minute on a slow connection)")

with urllib.request.urlopen(BULK_URL) as resp:
    data = json.load(resp)

products = data.get("products", {})
print(f"  Loaded {len(products):,} product entries.")

# ---------------------------------------------------------------------------
# 2. Extract per-instance-type attributes
#    We keep only OnDemand / RunInstances product families.
# ---------------------------------------------------------------------------

def parse_network_gbps(raw: str) -> float | None:
    """
    Convert AWS 'networkPerformance' strings to a numeric Gbps upper bound.

    Examples:
      "Up to 10 Gigabit"   → 10.0
      "25 Gigabit"         → 25.0
      "100 Gigabit"        → 100.0
      "Up to 25 Gigabit"   → 25.0
      "Very Low"           → 0.1
      "Low"                → 0.25
      "Low to Moderate"    → 0.5
      "Moderate"           → 1.0
      "High"               → 10.0
      "NA" / None          → None
    """
    if not raw or raw.strip().upper() in ("NA", "N/A", ""):
        return None

    s = raw.strip()

    # Numeric gigabit mention (handles "Up to X Gigabit", "X Gigabit", "X x Y Gbps" etc.)
    m = re.search(r"([\d,.]+)\s*(?:Gigabit|Gbps|Gbit)", s, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ""))

    # Megabit mention
    m = re.search(r"([\d,.]+)\s*(?:Megabit|Mbps|Mbit)", s, re.IGNORECASE)
    if m:
        return round(float(m.group(1).replace(",", "")) / 1000, 4)

    # Qualitative tiers
    tiers = {
        "very low": 0.1,
        "low to moderate": 0.5,
        "low": 0.25,
        "moderate": 1.0,
        "high": 10.0,
    }
    sl = s.lower()
    for key, val in tiers.items():
        if key in sl:
            return val

    return None


def parse_disk_mbps(raw: str) -> float | None:
    """
    Convert AWS 'dedicatedEbsThroughput' strings to numeric MB/s.

    Examples:
      "Up to 2,780 Mbps"   → 2780.0
      "3500 Mbps"          → 3500.0
      "Upto 9000 Mbps"     → 9000.0
      "Not Applicable"     → None
    """
    if not raw or raw.strip().upper() in ("NOT APPLICABLE", "NA", "N/A", ""):
        return None

    s = raw.strip()

    # Mbps
    m = re.search(r"([\d,]+)\s*Mbps", s, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ""))

    # Gbps → convert
    m = re.search(r"([\d,.]+)\s*Gbps", s, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", "")) * 1000

    return None


# Build a map: instance_type → (max_network_io_gbps, max_disk_io_mbps)
# Some instance types appear in multiple SKUs (different OS / tenancy);
# we take the MAX across all matching SKUs to get the true ceiling.
network_map: dict[str, float] = {}
disk_map: dict[str, float] = {}

for sku, product in products.items():
    attrs = product.get("attributes", {})
    itype = attrs.get("instanceType", "").strip()
    if not itype:
        continue

    net_raw = attrs.get("networkPerformance", "")
    disk_raw = attrs.get("dedicatedEbsThroughput", "")

    net_val = parse_network_gbps(net_raw)
    disk_val = parse_disk_mbps(disk_raw)

    if net_val is not None:
        network_map[itype] = max(network_map.get(itype, 0.0), net_val)
    if disk_val is not None:
        disk_map[itype] = max(disk_map.get(itype, 0.0), disk_val)

print(f"  Extracted specs for {len(network_map):,} instance types (network) "
      f"and {len(disk_map):,} (disk).")

# ---------------------------------------------------------------------------
# 3. Load CSV and join the new columns
# ---------------------------------------------------------------------------
CSV_PATH = "pricing_clean.csv"
df = pd.read_csv(CSV_PATH)
print(f"\nLoaded {len(df):,} rows from {CSV_PATH}.")

df["max_network_io_gbps"] = df["instance_type"].map(network_map)
df["max_disk_io_mbps"] = df["instance_type"].map(disk_map)

# ---------------------------------------------------------------------------
# 4. Coverage report
# ---------------------------------------------------------------------------
n_net_missing = df["max_network_io_gbps"].isna().sum()
n_disk_missing = df["max_disk_io_mbps"].isna().sum()
total = len(df)

print(f"\nCoverage:")
print(f"  max_network_io_gbps : {total - n_net_missing:,}/{total:,} rows filled "
      f"({100*(total-n_net_missing)/total:.1f}%)")
print(f"  max_disk_io_mbps    : {total - n_disk_missing:,}/{total:,} rows filled "
      f"({100*(total-n_disk_missing)/total:.1f}%)")

if n_net_missing or n_disk_missing:
    missing_types = sorted(
        df.loc[df["max_network_io_gbps"].isna(), "instance_type"].unique()
    )
    print(f"\n  Instance types with no network data ({len(missing_types)}): "
          + ", ".join(missing_types[:20])
          + (" …" if len(missing_types) > 20 else ""))

# ---------------------------------------------------------------------------
# 5. Save
# ---------------------------------------------------------------------------
df.to_csv(CSV_PATH, index=False)
print(f"\nSaved enriched CSV to {CSV_PATH}.")
print("New columns: max_network_io_gbps, max_disk_io_mbps")

# Quick sanity-check preview
print("\nSample rows:")
cols = ["instance_type", "region", "os", "max_network_io_gbps", "max_disk_io_mbps"]
print(df[cols].drop_duplicates("instance_type").head(10).to_string(index=False))
