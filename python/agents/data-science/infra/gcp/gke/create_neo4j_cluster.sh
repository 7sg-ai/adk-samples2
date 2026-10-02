#!/usr/bin/env bash
set -euo pipefail
PROJECT=gen-lang-client-0373235205
REGION=us-central1
ZONE=us-central1-a
CLUSTER=data-science-neo4j
ADDRESS=neo4j-bolt-ip
gcloud config set project "$PROJECT"
gcloud services enable container.googleapis.com compute.googleapis.com secretmanager.googleapis.com
gcloud container clusters create "$CLUSTER" \
  --zone us-central1-a \
  --machine-type e2-standard-2 \
  --num-nodes 1 \
  --enable-ip-alias \
  --network default \
  --subnetwork default \
  --release-channel regular
gcloud compute addresses create "$ADDRESS" \
  --region "$REGION" \
  --subnet default \
  --purpose SHARED_LOADBALANCER_VIP
IP=$(gcloud compute addresses describe "$ADDRESS" --region "$REGION" --format='value(address)')
gcloud run services update data-science \
  --region "$REGION" \
  --network default \
  --subnet default \
  --vpc-egress private-ranges-only
printf 'NEO4J_LOAD_BALANCER_IP=%s\n' "$IP"
