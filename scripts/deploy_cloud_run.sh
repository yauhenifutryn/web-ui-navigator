#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <gcp-project-id> <region> <service-name> [bucket-name]"
  exit 1
fi

PROJECT_ID="$1"
REGION="$2"
SERVICE_NAME="$3"
BUCKET_NAME="${4:-}"

if [[ -z "$BUCKET_NAME" ]]; then
  BUCKET_NAME="${PROJECT_ID}-live-navigator-artifacts"
fi

gcloud config set project "$PROJECT_ID" >/dev/null

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com >/dev/null

if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET_NAME}" --location="$REGION" >/dev/null
fi

ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},NAVIGATOR_GCS_BUCKET=${BUCKET_NAME}"

gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --set-env-vars "$ENV_VARS" >/dev/null

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)')"

echo "Cloud Run service deployed."
echo "Service URL: ${SERVICE_URL}"
echo "Artifact bucket: gs://${BUCKET_NAME}"
