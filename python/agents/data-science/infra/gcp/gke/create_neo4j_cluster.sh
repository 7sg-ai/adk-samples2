#!/usr/bin/env bash
set -euo pipefail
PROJECT=gen-lang-client-0373235205
REGION=us-central1
ZONE=us-central1-a
CLUSTER=data-science-neo4j
NETWORK=data-science-neo4j
SUBNET=data-science-neo4j
ADDRESS=neo4j-bolt-ip
gcloud config set project "$PROJECT"
gcloud services enable container.googleapis.com compute.googleapis.com secretmanager.googleapis.com
gcloud compute networks create "$NETWORK" --subnet-mode=custom
gcloud compute networks subnets create "$SUBNET" \
  --network="$NETWORK" \
  --region="$REGION" \
  --range=10.128.0.0/28 \
  --secondary-range=pods=10.128.1.0/24 \
  --secondary-range=services=10.128.2.0/27
gcloud container clusters create "$CLUSTER" \
  --zone us-central1-a \
  --machine-type e2-standard-2 \
  --num-nodes 1 \
  --enable-ip-alias \
  --network "$NETWORK" \
  --subnetwork "$SUBNET" \
  --cluster-secondary-range-name pods \
  --services-secondary-range-name services \
  --release-channel regular
gcloud compute addresses create "$ADDRESS" \
  --region "$REGION" \
  --subnet "$SUBNET" \
  --purpose SHARED_LOADBALANCER_VIP
IP=$(gcloud compute addresses describe "$ADDRESS" --region "$REGION" --format='value(address)')
gcloud run services update data-science \
  --region "$REGION" \
  --network "$NETWORK" \
  --subnet "$SUBNET" \
  --vpc-egress private-ranges-only
printf 'NEO4J_LOAD_BALANCER_IP=%s\n' "$IP"
