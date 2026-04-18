"""
Fetch AmazonEC2 EBS product family pricing from the AWS Bulk Pricing API.
Streams the response to avoid loading the full ~700MB file into memory.
"""

import json
import urllib.request
import ijson

EC2_PRICING_URL = (
    "https://pricing.us-east-1.amazonaws.com"
    "/offers/v1.0/aws/AmazonEC2/current/index.json"
)

OUTPUT_FILE = "ebs_pricing.json"


def fetch_ebs_pricing(url: str, output_path: str) -> None:
    print(f"Connecting to: {url}")
    print("Streaming response (this may take a while — file is ~700MB)...\n")

    ebs_products = {}
    ebs_terms = {"OnDemand": {}, "Reserved": {}}
    ebs_sku_set = set()

    with urllib.request.urlopen(url) as response:
        # Stream products — collect only EBS product family
        print("Pass 1: collecting EBS SKUs from products...")
        parser = ijson.kvitems(response, "products")
        count = 0
        for sku, product in parser:
            if product.get("productFamily") == "Storage":
                attrs = product.get("attributes", {})
                # Filter to EBS storage (not S3, Glacier, etc.)
                if attrs.get("storageMedia") in (
                    "SSD-backed", "HDD-backed", "Magnetic"
                ) or "EBS" in attrs.get("usagetype", ""):
                    ebs_products[sku] = product
                    ebs_sku_set.add(sku)
                    count += 1
                    if count % 100 == 0:
                        print(f"  Found {count} EBS products so far...")

    print(f"\nTotal EBS products found: {len(ebs_products)}")

    # Second pass: fetch matching terms (OnDemand pricing)
    print("\nPass 2: collecting pricing terms for EBS SKUs...")
    with urllib.request.urlopen(url) as response:
        term_count = 0
        for term_type in ("OnDemand", "Reserved"):
            parser = ijson.kvitems(response, f"terms.{term_type}")
            for sku, term_data in parser:
                if sku in ebs_sku_set:
                    ebs_terms[term_type][sku] = term_data
                    term_count += 1

    print(f"Total term entries found: {term_count}")

    # Build output structure
    result = {
        "source": url,
        "productFamily": "Storage (EBS)",
        "products": ebs_products,
        "terms": ebs_terms,
    }

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nSaved {len(ebs_products)} EBS products to: {output_path}")
    _print_summary(ebs_products)


def _print_summary(products: dict) -> None:
    """Print a quick breakdown of EBS volume types found."""
    volume_types: dict[str, int] = {}
    regions: set[str] = set()

    for product in products.values():
        attrs = product.get("attributes", {})
        vtype = attrs.get("volumeType") or attrs.get("volumeApiName") or "Unknown"
        volume_types[vtype] = volume_types.get(vtype, 0) + 1
        if "location" in attrs:
            regions.add(attrs["location"])

    print("\n--- EBS Volume Types ---")
    for vtype, count in sorted(volume_types.items(), key=lambda x: -x[1]):
        print(f"  {vtype}: {count} SKUs")

    print(f"\n--- Regions covered: {len(regions)} ---")
    for r in sorted(regions):
        print(f"  {r}")


if __name__ == "__main__":
    fetch_ebs_pricing(EC2_PRICING_URL, OUTPUT_FILE)
