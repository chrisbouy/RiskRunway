#!/bin/bash
#
# AWS Infrastructure Snapshot for RiskRunway
#
# Dumps all AWS configuration needed to recreate the environment.
# Run this BEFORE tearing anything down.
#
# Usage:
#   chmod +x scripts/aws_snapshot.sh
#   ./scripts/aws_snapshot.sh
#
# Output: aws_snapshot/ directory with JSON files for every resource
#

set -euo pipefail

REGION="us-east-1"
CLUSTER="riskrunway"
SERVICE="riskrunway-service"
TASK_DEF="riskrunway"
ECR_REPO="riskrunway-mapper"
S3_BUCKET="riskrunway-uploads"

SNAPSHOT_DIR="aws_snapshot"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

echo "============================================================"
echo "  RiskRunway AWS Infrastructure Snapshot"
echo "  Region: $REGION"
echo "  Timestamp: $TIMESTAMP"
echo "============================================================"
echo ""

mkdir -p "$SNAPSHOT_DIR"

# Helper: run an AWS command and save output, skip gracefully if it fails
dump() {
    local label="$1"
    local filename="$2"
    shift 2
    echo -n "  [$label] "
    if "$@" > "$SNAPSHOT_DIR/$filename" 2>/dev/null; then
        echo "✓ saved → $filename"
    else
        echo "✗ skipped (not found or access denied)"
        rm -f "$SNAPSHOT_DIR/$filename"
    fi
}

# ─────────────────────────────────────────────
# ECS
# ─────────────────────────────────────────────
echo ""
echo "[ECS]"

dump "Cluster" "ecs_cluster.json" \
    aws ecs describe-clusters --clusters "$CLUSTER" --region "$REGION" --include ATTACHMENTS SETTINGS STATISTICS

dump "Service" "ecs_service.json" \
    aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --region "$REGION"

# Get the current task definition ARN, then dump it
TASK_DEF_ARN=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --region "$REGION" \
    --query 'services[0].taskDefinition' --output text 2>/dev/null || echo "")

if [ -n "$TASK_DEF_ARN" ] && [ "$TASK_DEF_ARN" != "None" ]; then
    dump "Task Definition" "ecs_task_definition.json" \
        aws ecs describe-task-definition --task-definition "$TASK_DEF_ARN" --region "$REGION" --include TAGS
else
    dump "Task Definition (by name)" "ecs_task_definition.json" \
        aws ecs describe-task-definition --task-definition "$TASK_DEF" --region "$REGION" --include TAGS
fi

# ─────────────────────────────────────────────
# ECR
# ─────────────────────────────────────────────
echo ""
echo "[ECR]"

dump "Repository" "ecr_repository.json" \
    aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$REGION"

dump "Lifecycle Policy" "ecr_lifecycle_policy.json" \
    aws ecr get-lifecycle-policy --repository-name "$ECR_REPO" --region "$REGION"

dump "Repository Policy" "ecr_repository_policy.json" \
    aws ecr get-repository-policy --repository-name "$ECR_REPO" --region "$REGION"

# ─────────────────────────────────────────────
# RDS
# ─────────────────────────────────────────────
echo ""
echo "[RDS]"

dump "All DB Instances" "rds_instances.json" \
    aws rds describe-db-instances --region "$REGION"

dump "DB Subnet Groups" "rds_subnet_groups.json" \
    aws rds describe-db-subnet-groups --region "$REGION"

dump "DB Parameter Groups" "rds_parameter_groups.json" \
    aws rds describe-db-parameter-groups --region "$REGION"

# ─────────────────────────────────────────────
# ALB / Load Balancer
# ─────────────────────────────────────────────
echo ""
echo "[Load Balancer]"

dump "Load Balancers" "alb_load_balancers.json" \
    aws elbv2 describe-load-balancers --region "$REGION"

# Get all ALB ARNs and dump their listeners and target groups
ALB_ARNS=$(aws elbv2 describe-load-balancers --region "$REGION" \
    --query 'LoadBalancers[].LoadBalancerArn' --output text 2>/dev/null || echo "")

if [ -n "$ALB_ARNS" ]; then
    for ARN in $ALB_ARNS; do
        ALB_NAME=$(echo "$ARN" | grep -oP '(?<=loadbalancer/app/)[^/]+' || echo "unknown")
        dump "Listeners ($ALB_NAME)" "alb_listeners_${ALB_NAME}.json" \
            aws elbv2 describe-listeners --load-balancer-arn "$ARN" --region "$REGION"
    done
fi

dump "Target Groups" "alb_target_groups.json" \
    aws elbv2 describe-target-groups --region "$REGION"

# Dump listener rules for each listener
LISTENER_ARNS=$(aws elbv2 describe-load-balancers --region "$REGION" \
    --query 'LoadBalancers[].LoadBalancerArn' --output text 2>/dev/null | \
    xargs -I{} aws elbv2 describe-listeners --load-balancer-arn {} --region "$REGION" \
    --query 'Listeners[].ListenerArn' --output text 2>/dev/null || echo "")

if [ -n "$LISTENER_ARNS" ]; then
    LISTENER_IDX=0
    for LARN in $LISTENER_ARNS; do
        LISTENER_IDX=$((LISTENER_IDX + 1))
        dump "Listener Rules #$LISTENER_IDX" "alb_listener_rules_${LISTENER_IDX}.json" \
            aws elbv2 describe-rules --listener-arn "$LARN" --region "$REGION"
    done
fi

# ─────────────────────────────────────────────
# ACM (SSL Certificates)
# ─────────────────────────────────────────────
echo ""
echo "[ACM Certificates]"

dump "Certificates List" "acm_certificates.json" \
    aws acm list-certificates --region "$REGION"

# Get details for each cert
CERT_ARNS=$(aws acm list-certificates --region "$REGION" \
    --query 'CertificateSummaryList[].CertificateArn' --output text 2>/dev/null || echo "")

CERT_IDX=0
for CARN in $CERT_ARNS; do
    CERT_IDX=$((CERT_IDX + 1))
    dump "Certificate #$CERT_IDX details" "acm_cert_${CERT_IDX}.json" \
        aws acm describe-certificate --certificate-arn "$CARN" --region "$REGION"
done

# ─────────────────────────────────────────────
# S3
# ─────────────────────────────────────────────
echo ""
echo "[S3]"

dump "Bucket Policy" "s3_bucket_policy.json" \
    aws s3api get-bucket-policy --bucket "$S3_BUCKET" --region "$REGION"

dump "Bucket CORS" "s3_bucket_cors.json" \
    aws s3api get-bucket-cors --bucket "$S3_BUCKET" --region "$REGION"

dump "Bucket Versioning" "s3_bucket_versioning.json" \
    aws s3api get-bucket-versioning --bucket "$S3_BUCKET" --region "$REGION"

dump "Bucket Encryption" "s3_bucket_encryption.json" \
    aws s3api get-bucket-encryption --bucket "$S3_BUCKET" --region "$REGION"

dump "Bucket Lifecycle" "s3_bucket_lifecycle.json" \
    aws s3api get-bucket-lifecycle-configuration --bucket "$S3_BUCKET" --region "$REGION"

# ─────────────────────────────────────────────
# VPC & Security Groups
# ─────────────────────────────────────────────
echo ""
echo "[VPC & Security Groups]"

dump "VPCs" "vpc_list.json" \
    aws ec2 describe-vpcs --region "$REGION"

dump "Subnets" "vpc_subnets.json" \
    aws ec2 describe-subnets --region "$REGION"

dump "Security Groups" "vpc_security_groups.json" \
    aws ec2 describe-security-groups --region "$REGION"

dump "Internet Gateways" "vpc_internet_gateways.json" \
    aws ec2 describe-internet-gateways --region "$REGION"

dump "NAT Gateways" "vpc_nat_gateways.json" \
    aws ec2 describe-nat-gateways --region "$REGION"

dump "Route Tables" "vpc_route_tables.json" \
    aws ec2 describe-route-tables --region "$REGION"

# ─────────────────────────────────────────────
# IAM (ECS Task Role & Execution Role)
# ─────────────────────────────────────────────
echo ""
echo "[IAM Roles]"

# Extract role names from task definition
if [ -f "$SNAPSHOT_DIR/ecs_task_definition.json" ]; then
    TASK_ROLE=$(cat "$SNAPSHOT_DIR/ecs_task_definition.json" | python3 -c "
import json, sys
td = json.load(sys.stdin).get('taskDefinition', {})
print(td.get('taskRoleArn', '').split('/')[-1])
" 2>/dev/null || echo "")

    EXEC_ROLE=$(cat "$SNAPSHOT_DIR/ecs_task_definition.json" | python3 -c "
import json, sys
td = json.load(sys.stdin).get('taskDefinition', {})
print(td.get('executionRoleArn', '').split('/')[-1])
" 2>/dev/null || echo "")

    if [ -n "$TASK_ROLE" ] && [ "$TASK_ROLE" != "" ]; then
        dump "Task Role ($TASK_ROLE)" "iam_task_role.json" \
            aws iam get-role --role-name "$TASK_ROLE"
        dump "Task Role Policies" "iam_task_role_policies.json" \
            aws iam list-attached-role-policies --role-name "$TASK_ROLE"
        # Inline policies
        dump "Task Role Inline Policies" "iam_task_role_inline.json" \
            aws iam list-role-policies --role-name "$TASK_ROLE"
    fi

    if [ -n "$EXEC_ROLE" ] && [ "$EXEC_ROLE" != "" ] && [ "$EXEC_ROLE" != "$TASK_ROLE" ]; then
        dump "Execution Role ($EXEC_ROLE)" "iam_exec_role.json" \
            aws iam get-role --role-name "$EXEC_ROLE"
        dump "Execution Role Policies" "iam_exec_role_policies.json" \
            aws iam list-attached-role-policies --role-name "$EXEC_ROLE"
    fi
fi

# ─────────────────────────────────────────────
# CloudWatch Log Groups
# ─────────────────────────────────────────────
echo ""
echo "[CloudWatch Logs]"

dump "Log Groups (riskrunway)" "cloudwatch_log_groups.json" \
    aws logs describe-log-groups --log-group-name-prefix "/ecs/riskrunway" --region "$REGION"

# ─────────────────────────────────────────────
# Route 53 (DNS)
# ─────────────────────────────────────────────
echo ""
echo "[Route 53 DNS]"

# Find hosted zone for risk-runway.com
HOSTED_ZONE_ID=$(aws route53 list-hosted-zones \
    --query "HostedZones[?Name=='risk-runway.com.'].Id" --output text 2>/dev/null | sed 's|/hostedzone/||' || echo "")

if [ -n "$HOSTED_ZONE_ID" ] && [ "$HOSTED_ZONE_ID" != "None" ]; then
    dump "Hosted Zone" "route53_hosted_zone.json" \
        aws route53 get-hosted-zone --id "$HOSTED_ZONE_ID"
    dump "DNS Records" "route53_records.json" \
        aws route53 list-resource-record-sets --hosted-zone-id "$HOSTED_ZONE_ID"
else
    echo "  ⚠ No hosted zone found for risk-runway.com (might be in another account or registrar)"
fi

# ─────────────────────────────────────────────
# Secrets Manager / Parameter Store
# ─────────────────────────────────────────────
echo ""
echo "[Secrets & Parameters]"

dump "Secrets Manager (list)" "secrets_manager_list.json" \
    aws secretsmanager list-secrets --region "$REGION"

dump "SSM Parameters (list)" "ssm_parameters.json" \
    aws ssm describe-parameters --region "$REGION"

# ─────────────────────────────────────────────
# SES (Email)
# ─────────────────────────────────────────────
echo ""
echo "[SES]"

dump "Verified Identities" "ses_identities.json" \
    aws ses list-identities --region "$REGION"

dump "Email Identity (risk-runway.com)" "ses_domain_identity.json" \
    aws sesv2 get-email-identity --email-identity "risk-runway.com" --region "$REGION"

# ─────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────
echo ""
echo "============================================================"
echo "  Snapshot complete!"
echo "  Files saved to: $SNAPSHOT_DIR/"
echo ""
echo "  File count: $(ls -1 "$SNAPSHOT_DIR" | wc -l | tr -d ' ') files"
echo "  Total size: $(du -sh "$SNAPSHOT_DIR" | cut -f1)"
echo ""
echo "  IMPORTANT: This directory contains sensitive config."
echo "  Add to .gitignore or store securely."
echo "============================================================"

# Also dump a metadata file
cat > "$SNAPSHOT_DIR/_metadata.json" <<EOF
{
    "snapshot_timestamp": "$TIMESTAMP",
    "region": "$REGION",
    "ecs_cluster": "$CLUSTER",
    "ecs_service": "$SERVICE",
    "task_definition": "$TASK_DEF",
    "ecr_repository": "$ECR_REPO",
    "s3_bucket": "$S3_BUCKET",
    "notes": "Snapshot taken before teardown. Use restore script to recreate."
}
EOF

echo ""
echo "  Next: Review the snapshot, then run the teardown."
echo ""
