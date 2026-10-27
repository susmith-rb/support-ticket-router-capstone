# Cloud Run Agent & MCP Services

Unified multi-service container image and deployment guide for the Support Ticket Router & Assistant.

## Services Topology

| Service | Component | Target Runtime | GCP Identity | Database IAM Role | Description |
|---|---|---|---|---|---|
| `ticket-db-mcp` | MCP Server | Cloud Run | `sa-ticket-mcp@` | `ticket_rw` | CRUD tickets, lifecycle events, and audit logging. |
| `order-lookup-mcp` | MCP Server | Cloud Run | `sa-order-mcp@` | `orders_ro` | Read-only order mock with Postgres Row-Level Security (RLS). |
| `human-queue` | Agent Service | Cloud Run | `sa-queue@` | `queue_rw` | Queue priority ordering, claim, resolve, and audited PII unmasking. |
| `faq-resolver` | Agent Service | GKE Autopilot / Cloud Run | `sa-faq@` | — | Bounded ReAct FAQ resolution with citations. |

All services are packaged into a single shared image and dispatched at runtime via the `SERVICE` environment variable:
- `SERVICE=ticket_db`: Launches `ticket-db-mcp` FastAPI server.
- `SERVICE=order_lookup`: Launches `order-lookup-mcp` FastAPI server.
- `SERVICE=human_queue`: Launches `human-queue` FastAPI server.
- `SERVICE=faq_resolver`: Launches `faq-resolver` FastAPI server.

## Building the Container Image

Build and publish the container image to Google Artifact Registry via Google Cloud Build:

```bash
gcloud builds submit \
  --project=susmith-fde-ticket-router \
  --config=services/cloudbuild.yaml \
  --gcs-source-staging-dir=gs://susmith-fde-ticket-router-laya/cloudbuild-src \
  --service-account=projects/susmith-fde-ticket-router/serviceAccounts/ticket-router-build@susmith-fde-ticket-router.iam.gserviceaccount.com \
  --substitutions=_IMAGE_TAG=us-central1-docker.pkg.dev/susmith-fde-ticket-router/serving/agents:v1 \
  .
```

> **Note on Cloud Build Storage**: Cloud Build service account `ticket-router-build@susmith-fde-ticket-router.iam.gserviceaccount.com` does not have access to the project's default `_cloudbuild` bucket. The `--gcs-source-staging-dir=gs://susmith-fde-ticket-router-laya/cloudbuild-src` flag is mandatory for staging source tarballs.

## Build-Time Validation

`services/Dockerfile` includes an automated build-time import verification step:
```dockerfile
RUN python -c "import ticket_router.serve, ticket_router.mcp.ticket_db, ticket_router.mcp.order_lookup, ticket_router.agents.human_queue, ticket_router.agents.faq_resolver.app; print('All agent & MCP services import cleanly')"
```
This fails the build immediately if any packaging, dependency, or syntax regression exists before the image is pushed to Artifact Registry.

## Runtime Configuration

The container listens on `$PORT` (default `8080`) and exposes `/healthz` for Cloud Run liveness/readiness probes.

### Environment Variables

| Variable | Default | Allowed Values | Purpose |
|---|---|---|---|
| `SERVICE` | `ticket_db` | `ticket_db`, `order_lookup`, `human_queue`, `faq_resolver` | Selects which FastAPI service to run. |
| `PORT` | `8080` | Any valid TCP port | Cloud Run container ingress port. |
| `HOST` | `0.0.0.0` | IP string | Bind host interface. |
| `INSTANCE_CONNECTION_NAME` | — | `project:region:instance` | Cloud SQL instance connection string (required unless `ALLOW_FAKE_REPOS=1`). |
| `DB_NAME` | `support` | String | Cloud SQL database name. |
| `DB_IAM_USER` | — | IAM service account username | IAM DB user without `.gserviceaccount.com` (e.g. `sa-ticket-mcp`). |
| `ALLOW_FAKE_REPOS` | `0` | `0`, `1` | Set to `1` in development or isolated unit testing to bypass Cloud SQL. |
