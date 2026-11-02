"""Controlled diagnostic run of the production collect_kroger_catalog function for the 7 terms."""
import json
import sys
from pathlib import Path
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from kroger_collector import KrogerClient, collect_kroger_catalog

vals = dotenv_values(".env")
client = KrogerClient(vals.get("KROGER_CLIENT_ID"), vals.get("KROGER_CLIENT_SECRET"))
client.authenticate()

location = {
    "store_id": "53100502",
    "store_location": "1720 N Milwaukee Ave, Vernon Hills, IL 60061",
    "zipcode": "60061",
    "store_name": "Mariano's",
}

diag_run_id = "DIAG_SEVEN_TERMS_20261102T214000Z"
diag_evidence_dir = Path("data/evidence_diag_test")
diag_checkpoint_dir = Path("data/checkpoints_diag_test")

categories_to_run = {
    "Produce": ["apples", "bananas", "berries", "lettuce", "tomatoes", "potatoes"],
    "Pantry": ["sugar"],
}

print("=== EXECUTING PRODUCTION collect_kroger_catalog FOR 7 TERMS ===")
records, stats, meta = collect_kroger_catalog(
    client=client,
    location=location,
    categories_terms=categories_to_run,
    target_count=500,
    proof_only=False,
    run_id=diag_run_id,
    evidence_dir=diag_evidence_dir,
    checkpoint_dir=diag_checkpoint_dir,
)

print("\n=== EXECUTION COMPLETED ===")
print(f"Total unique records collected: {len(records)}")
print(f"Stats categories count: {len(stats)}")

for c_stat in stats:
    cat = c_stat.get("category")
    cov_st = c_stat.get("coverage_status")
    cov_comp = c_stat.get("coverage_complete")
    print(f"\nCategory: {cat} (coverage_status: {cov_st}, coverage_complete: {cov_comp})")
    for td in c_stat.get("term_details", []):
        term = td.get("term")
        st = td.get("status")
        sr = td.get("stop_reason")
        pg = td.get("pages_requested")
        acc = td.get("accepted_products")
        raw = td.get("raw_results_received")
        dup = td.get("duplicates_encountered")
        print(f"  Term: {term:10s} | status: {st:22s} | stop_reason: {sr:20s} | pages: {pg} | accepted: {acc:3d} | raw: {raw:3d} | dups: {dup:3d}")

# Verify isolated evidence JSONL
ev_file = diag_evidence_dir / f"product_evidence_{location['store_id']}.jsonl"
print(f"\nIsolated evidence file exists: {ev_file.exists()}")
if ev_file.exists():
    ev_lines = [json.loads(l) for l in ev_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"Evidence records count: {len(ev_lines)}")
    if ev_lines:
        print(f"Sample evidence entry run_id: {ev_lines[0].get('run_id')}")
        unique_keys = {e.get("product_key") for e in ev_lines}
        print(f"Unique product_keys in evidence: {len(unique_keys)}")
        # Check all entries have run_id
        all_have_run_id = all(e.get("run_id") == diag_run_id for e in ev_lines)
        print(f"All evidence entries have matching run_id ({diag_run_id}): {all_have_run_id}")
