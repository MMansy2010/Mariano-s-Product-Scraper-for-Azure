# Azure Production Architecture & Deployment Guide

This document describes the production deployment design and operational requirements for running the Mariano's catalog scraper on Microsoft Azure twice weekly per store.

## Target Architecture

1. **Workload Host**: Azure Container Apps (ACA) Job
   - Trigger: Scheduled UTC cron (`0 9 * * 2,5` = Tuesdays and Fridays at 09:00 UTC).
     - *Timezone / DST Note*: Azure Container Apps Jobs evaluate cron expressions exclusively in UTC. `09:00 UTC` corresponds to 03:00 AM Central Standard Time (CST, UTC-6 in winter) and 04:00 AM Central Daylight Time (CDT, UTC-5 in summer). For exact 03:00 AM local execution year-round, update `cronExpression` to `0 8 * * 2,5` during CDT or trigger the job via an Azure Logic App recurrence trigger with native IANA timezone support.
   - Base Image: `mcr.microsoft.com/playwright/python:v1.52.0-noble`.
   - Compute allocation: 1.0 vCPU, 2.0 GiB memory.
   - Timeout: 7200 seconds (2 hours) with replicaRetryLimit: 1.

2. **Catalog Publication & Retention**: Azure Blob Storage
   - Container: `marianos-catalog`
   - Active Catalog: `marianos_latest.json` (atomic publication only after full catalog validation passes).
   - Immutable Historical Runs: `runs/{run_timestamp}/catalog.json`.
   - Run Diagnostics & Audit Reports: `runs/{run_timestamp}/report.json`.

3. **Secrets & Security**:
   - Storage connection strings and API credentials stored in Azure Key Vault or ACA secret store (`secretRef`).
   - Container runs non-root where supported.

4. **Observability & Alerting**:
   - Logs forwarded to Azure Log Analytics Workspace.
   - Alert rules trigger when job exit code != 0 or when `final_status == "rejected_preserving_previous_success"`.

## Deployment Commands (Azure CLI)

```bash
# 1. Login to Azure
az login
az account set --subscription "<SUBSCRIPTION_ID>"

# 2. Create resource group and container environment
az group create --name rg-marianos-scraper-prod --location centralus

az containerapp env create \
  --name cae-marianos-scraper-prod \
  --resource-group rg-marianos-scraper-prod \
  --location centralus

# 3. Deploy the Container Apps Job using Bicep
az deployment group create \
  --resource-group rg-marianos-scraper-prod \
  --template-file azure/container-app-job.bicep \
  --parameters \
    storageAccountName="samarianosprod" \
    storageConnectionString="<STORAGE_CONNECTION_STRING>" \
    krogerClientId="<KROGER_CLIENT_ID>" \
    krogerClientSecret="<KROGER_CLIENT_SECRET>"
```

> [!NOTE]
> **Deployment Status: Static Architectural Template — Not Deployed**
> - **Template Status:** `STATICALLY_REVIEWED_NOT_COMPILED` (Bicep template reviewed against ARM specs; Azure CLI / Bicep CLI not installed locally).
> - **Job Status:** `CONFIGURED_NOT_DEPLOYED` (No Azure subscription, resource group, or ACA Job deployed).
> - **Storage Status:** `LOCAL_STORAGE_TESTED`, `AZURE_BLOB_NOT_LIVE_VERIFIED` (Local paired file publication and backup rollback verified via unit tests; live Azure Blob Storage integration not tested due to absence of Azure connection string).
> Cloud provisioning must be performed by a cloud administrator using the commands above once an active Azure subscription is available.

## Fail-Safe Execution Model

- The job executes `python src/kroger_collector.py --config config/settings.json --target-count 500`.
- When all configured categories pass validation (minimum 500 records, verified store identity `53100502`, 0 duplicate identities, 0 nulls, correct 22-field schema order), the collector atomically publishes `marianos_latest.json` (and store-specific `marianos_{store_id}_latest.json`) and writes timestamped run records.
- If any category or page encounters an unrecoverable failure or incomplete pagination, the run rejects with exit code 2. The previous good published catalog is preserved completely untouched (byte-for-byte).
