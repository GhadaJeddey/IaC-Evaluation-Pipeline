import requests
import csv
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================================
# CONFIGURATION
# ============================================================================

BASE_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current"
OUTPUT_CSV = "pricing_clean.csv"
PREVIEW_REGIONS = ["us-east-1", "af-south-1", "ap-northeast-2"]

TARGET_FAMILIES = {
    't3', 't4g', 't3a',
    'm5', 'm7i', 'm8i', 'm7g', 'm8g', 'm7a',
    'c5', 'c7i', 'c8i', 'c7g', 'c8g', 'c7a',
    'r5', 'r7i', 'r8i', 'r7g', 'r8g', 'r7a'
}

LOCATION_TO_REGION = {
    "US East (N. Virginia)": "us-east-1",
    "US East (Ohio)": "us-east-2",
    "US West (N. California)": "us-west-1",
    "US West (Oregon)": "us-west-2",
    "Canada (Central)": "ca-central-1",
    "Canada West (Calgary)": "ca-west-1",
    "EU (Ireland)": "eu-west-1",
    "EU (London)": "eu-west-2",
    "EU (Paris)": "eu-west-3",
    "EU (Frankfurt)": "eu-central-1",
    "EU (Stockholm)": "eu-north-1",
    "EU (Milan)": "eu-south-1",
    "EU (Spain)": "eu-south-2",
    "Africa (Cape Town)": "af-south-1",
    "Asia Pacific (Singapore)": "ap-southeast-1",
    "Asia Pacific (Sydney)": "ap-southeast-2",
    "Asia Pacific (Melbourne)": "ap-southeast-4",
    "Asia Pacific (Tokyo)": "ap-northeast-1",
    "Asia Pacific (Seoul)": "ap-northeast-2",
    "Asia Pacific (Osaka)": "ap-northeast-3",
    "Asia Pacific (Mumbai)": "ap-south-1",
    "Asia Pacific (Hyderabad)": "ap-south-2",
    "Asia Pacific (Malaysia)": "ap-southeast-5",
    "Asia Pacific (Thailand)": "ap-southeast-7",
    "South America (Sao Paulo)": "sa-east-1",
    "Middle East (UAE)": "me-central-1",
    "Middle East (Bahrain)": "me-south-1",
    "Israel (Tel Aviv)": "il-central-1",
    "Mexico (Central)": "mx-central-1",
}

# ============================================================================
# FIX 1: Correct OS filter rules
#
# The AWS pricing JSON uses these exact values:
#
#   OS          operatingSystem   preInstalledSw   capacitystatus   tenancy
#   -------     ---------------   --------------   --------------   -------
#   Linux       Linux             NA               Used             Shared
#   Windows     Windows           NA               Used             Shared   ← BUG WAS HERE (was checking for '')
#   RHEL        RHEL              NA               Used             Shared
#   SUSE        SUSE              NA               Used             Shared
#   Win+SQL Web Windows           SQL Web          Used             Shared   ← excluded (preInstalledSw != NA)
#   Win+SQL Std Windows           SQL Std          Used             Shared   ← excluded
#   RHEL+HA     RHEL              HA               Used             Shared   ← excluded
#
# All standard OS SKUs share preInstalledSw = "NA".
# The old code used pre_sw == '' for Windows — this matched nothing real.
# ============================================================================

VALID_OS_FILTERS = {
    'Linux':   {'operatingSystem': 'Linux',   'preInstalledSw': 'NA', 'capacitystatus': 'Used', 'tenancy': 'Shared'},
    'Windows': {'operatingSystem': 'Windows', 'preInstalledSw': 'NA', 'capacitystatus': 'Used', 'tenancy': 'Shared'},
    'RHEL':    {'operatingSystem': 'RHEL',    'preInstalledSw': 'NA', 'capacitystatus': 'Used', 'tenancy': 'Shared'},
    'SUSE':    {'operatingSystem': 'SUSE',    'preInstalledSw': 'NA', 'capacitystatus': 'Used', 'tenancy': 'Shared'},
}

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_all_regions():
    url = f"{BASE_URL}/us-east-1/index.json"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return [r['regionCode'] for r in data['config']['regions']]
    except Exception as e:
        print(f"Error fetching regions: {e}")
        return list(LOCATION_TO_REGION.values())


def parse_memory(mem_str):
    if not mem_str:
        return None
    match = re.match(r"([\d.]+)\s*(Gi?B)?", str(mem_str).strip(), re.IGNORECASE)
    return round(float(match.group(1)), 2) if match else None


def extract_instance_family(instance_type):
    if not instance_type:
        return None
    match = re.match(r"([a-z]+\d+[a-z]?)", instance_type, re.IGNORECASE)
    return match.group(1).lower() if match else None


def classify_os(attr):
    """
    FIX 1: Check all four fields strictly for each OS type.
    Returns the normalized OS name or None if no valid match.
    Previously, Windows used pre_sw == '' which never matched real SKUs.
    """
    raw_os  = attr.get('operatingSystem', '')
    pre_sw  = attr.get('preInstalledSw', '')
    cap     = attr.get('capacitystatus', '')
    tenancy = attr.get('tenancy', '')

    for os_name, rules in VALID_OS_FILTERS.items():
        if (raw_os  == rules['operatingSystem'] and
            pre_sw  == rules['preInstalledSw'] and
            cap     == rules['capacitystatus'] and
            tenancy == rules['tenancy']):
            return os_name
    return None


def resolve_region(location_name):
    """
    FIX 2: Return None for unknown locations instead of a corrupt lowercase string.
    The old code used location_name.lower() as fallback, producing garbage like
    'us east (n. virginia)' instead of 'us-east-1'.
    """
    return LOCATION_TO_REGION.get(location_name, None)


def fetch_region_prices(region):
    url = f"{BASE_URL}/{region}/index.json"
    print(f"  Fetching {region}...")
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        products       = data.get('products', {})
        on_demand_terms = data.get('terms', {}).get('OnDemand', {})

        results = []

        for sku, product_info in products.items():
            attr          = product_info.get('attributes', {})
            instance_type = attr.get('instanceType', '')
            family        = extract_instance_family(instance_type)

            if family not in TARGET_FAMILIES:
                continue
            if 'metal' in instance_type.lower():
                continue

            norm_os = classify_os(attr)
            if norm_os is None:
                continue

            if sku not in on_demand_terms:
                continue

            location_name = attr.get('location', '')
            region_code   = resolve_region(location_name)
            if region_code is None:
                # FIX 2: skip rows with unmapped locations instead of corrupting them
                continue

            vcpu   = int(attr.get('vcpu', 0) or 0)
            ram_gb = parse_memory(attr.get('memory'))
            if ram_gb is None or vcpu == 0:
                continue

            for term_details in on_demand_terms[sku].values():
                for pd in term_details.get('priceDimensions', {}).values():
                    if pd.get('unit') != 'Hrs':
                        continue
                    usd = pd.get('pricePerUnit', {}).get('USD', '0')
                    price = float(usd)
                    if price <= 0.0:
                        continue

                    results.append({
                        'instance_family': family,
                        'instance_type':   instance_type,
                        'vcpu':            vcpu,
                        'ram_gb':          ram_gb,
                        'region':          region_code,
                        'os':              norm_os,
                        'price_per_hour':  round(price, 5),
                    })

        return results

    except Exception as e:
        print(f"  Error in {region}: {e}")
        return []


# ============================================================================
# FIX 3: Deduplication — keep HIGHEST price for Windows, LOWEST for Linux
#
# The old code always kept the lower price. For Linux this is correct (avoid
# reserved-capacity SKUs that slip through). For Windows it can pull in a
# wrong low-priced SKU if multiple Windows SKUs share the same key.
# Safest fix: keep the highest price per key — reserved/spot SKUs that
# slip through are always cheaper than on-demand, so max() gives on-demand.
# ============================================================================

def deduplicate(all_raw_rows):
    rows_by_key = {}
    for row in all_raw_rows:
        key = (row['instance_type'], row['region'], row['os'])
        if key not in rows_by_key:
            rows_by_key[key] = row
        else:
            existing = rows_by_key[key]['price_per_hour']
            incoming = row['price_per_hour']
            # FIX 3: keep the HIGHER price — on-demand is always the most expensive
            if incoming > existing:
                rows_by_key[key] = row
    return rows_by_key


def compute_ladder_ranks(rows_by_key):
    unique_hardware = set()
    for row in rows_by_key.values():
        unique_hardware.add((row['instance_family'], row['instance_type'], row['vcpu'], row['ram_gb']))

    sorted_hardware = sorted(unique_hardware, key=lambda x: (x[0], x[2], x[3]))

    type_to_rank = {}
    current_family = ""
    rank = 0

    for fam, itype, vcpu, ram in sorted_hardware:
        if fam != current_family:
            rank = 1
            current_family = fam
        else:
            rank += 1
        type_to_rank[itype] = rank

    for row in rows_by_key.values():
        row['ladder_rank'] = type_to_rank.get(row['instance_type'], 0)


def validate_windows_premiums(rows_by_key):
    ok = 0
    issues = []
    for key, row in rows_by_key.items():
        if row['os'] != 'Windows':
            continue
        linux_key = (row['instance_type'], row['region'], 'Linux')
        if linux_key not in rows_by_key:
            continue
        linux_price = rows_by_key[linux_key]['price_per_hour']
        if row['price_per_hour'] > linux_price:
            ok += 1
        else:
            issues.append(
                f"  ISSUE: {row['instance_type']} {row['region']} "
                f"Win=${row['price_per_hour']:.4f} <= Linux=${linux_price:.4f}"
            )
    return ok, issues


# ============================================================================
# PREVIEW
# ============================================================================

def preview_results():
    print("\n" + "=" * 60)
    print("PREVIEW MODE — fetching sample data for validation")
    print("=" * 60)

    sample_rows = []
    for region in PREVIEW_REGIONS:
        rows = fetch_region_prices(region)
        sample_rows.extend(rows)
        time.sleep(0.3)

    if not sample_rows:
        print("No data fetched. Check TARGET_FAMILIES or network.")
        return False

    rows_by_key = deduplicate(sample_rows)

    print(f"\nSample rows collected: {len(rows_by_key)}")
    print(f"Windows rows: {sum(1 for r in rows_by_key.values() if r['os'] == 'Windows')}")
    print(f"Linux rows:   {sum(1 for r in rows_by_key.values() if r['os'] == 'Linux')}")

    print("\nFirst 15 rows (sorted):")
    print("-" * 100)
    print(f"{'Instance':<16} {'Region':<16} {'OS':<10} {'$/hr':<10} {'vCPU':<6} {'RAM GB'}")
    print("-" * 100)
    for row in sorted(rows_by_key.values(), key=lambda x: (x['instance_type'], x['region'], x['os']))[:15]:
        print(f"{row['instance_type']:<16} {row['region']:<16} {row['os']:<10} "
              f"${row['price_per_hour']:<9.4f} {row['vcpu']:<6} {row['ram_gb']}")

    print("\nWindows premium validation:")
    ok, issues = validate_windows_premiums(rows_by_key)
    print(f"  Rows with correct Windows premium: {ok}")
    if issues:
        for i in issues:
            print(i)
        print("\nDo NOT run full extraction — fix the filter logic.")
        return False

    if ok == 0:
        print("  WARNING: No Windows/Linux pairs found to validate.")
        print("  Check that Windows SKUs are being captured at all.")
        return False

    print("  All Windows prices are higher than Linux. Safe to proceed.")
    return True


# ============================================================================
# FULL EXTRACTION
# ============================================================================

def export_all_regions_to_csv():
    regions = get_all_regions()
    print(f"\nProcessing {len(regions)} regions with 10 workers...")

    all_raw_rows = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_region = {
            executor.submit(fetch_region_prices, region): region
            for region in regions
        }
        for future in as_completed(future_to_region):
            region = future_to_region[future]
            try:
                rows = future.result()
                all_raw_rows.extend(rows)
            except Exception as e:
                print(f"Region {region} failed: {e}")
            time.sleep(0.05)

    print(f"\nRaw rows fetched: {len(all_raw_rows)}")

    rows_by_key = deduplicate(all_raw_rows)
    compute_ladder_ranks(rows_by_key)

    ok, issues = validate_windows_premiums(rows_by_key)

    fieldnames = [
        'instance_family', 'instance_type', 'region', 'os',
        'price_per_hour', 'vcpu', 'ram_gb', 'ladder_rank'
    ]

    with open(OUTPUT_CSV, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(
            rows_by_key.values(),
            key=lambda x: (x['instance_family'], x['ladder_rank'], x['region'], x['os'])
        ):
            writer.writerow(row)

    print(f"\n{'=' * 60}")
    print(f"COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Raw rows fetched:          {len(all_raw_rows)}")
    print(f"  Unique rows after dedup:   {len(rows_by_key)}")
    print(f"  Windows rows (correct):    {ok}")
    if issues:
        print(f"  WARNING — bad rows:        {len(issues)}")
        for i in issues[:5]:
            print(i)
    print(f"  Output:                    {OUTPUT_CSV}")


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    export_all_regions_to_csv()