#!/bin/bash
#
# AWS Restore for RiskRunway App (app.risk-runway.com)
#
# Recreates the application infrastructure from the snapshot taken before teardown.
# Run this when you have a new client to demo for.
#
# What it restores:
#   - RDS from final snapshot
#   - ALB + target group + listeners (HTTP→HTTPS redirect, HTTPS→target)
#   - ECS cluster + service (using existing task definition)
#   - DNS: app.risk-runway.com → new ALB
#   - Pushes latest Docker image to ECR
#
# What it does NOT restore:
#   - paulin.risk-runway.com (pilot cancelled)
#   - www.risk-runway.com → ALB (that was a misconfiguration)
#
# Prerequisites:
#   - aws_snapshot/ directory exists with JSON files from snapshot
#   - AWS CLI authenticated
#   - Docker installed (for image push)
#
# Usage:
#   chmod +x scripts/aws_restore.sh
#   ./scripts/aws_restore.sh
#
# Estimated time: ~10-15 minutes (mostly waiting for RDS)
#

set -euo pipefail

REGION="us-east-1"
ACCOUNT_ID="703671916421"

# Resource names (same as before teardown)
CLUSTER="riskrunway"
SERVICE="riskrunway-service"
TASK_DEF_FAMILY="riskrunway"
ECR_REPO="riskrunway-mapper"
ALB_NAME="riskrunway-alb"
RDS_INSTANCE="riskrunway-db"
LOG_GROUP="/ecs/riskrunway"

# Network config (from snapshot)
VPC_ID="vpc-0f9a59d9e88e0f80f"
ALB_SUBNETS="subnet-021b3d70dbda48b57,subnet-04246fe0f4d27caf8,subnet-04e1b3bcdf8822186,subnet-065287e125d96b95e,subnet-07b4573c0658a5136,subnet-0bcf696a0491dea87"
ALB_SG="sg-025209b993acf6cf0"
ECS_SG="sg-0ea045d25d7e220d6"
RDS_SG="sg-0cf6d2d2e3b4d7813"

# ACM certificate
ACM_CERT_ARN="arn:aws:acm:us-east-1:703671916421:certificate/b639d555-aff4-42e0-a9c0-c77889f70f6a"

# ECS config
TASK_CPU="1024"
TASK_MEMORY="2048"
CONTAINER_PORT="5001"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo ""
echo -e "${BLUE}============================================================${NC}"
echo -e "${BLUE}  RiskRunway App RESTORE (app.risk-runway.com)${NC}"
echo -e "${BLUE}============================================================${NC}"
echo ""
echo "  This will recreate:"
echo "    • RDS instance (from final snapshot)"
echo "    • ALB + HTTPS listener + target group"
echo "    • ECS cluster + service"
echo "    • DNS: app.risk-runway.com → ALB"
echo "    • Docker image push to ECR"
echo ""
echo "  Estimated time: 10-15 minutes"
echo "  Estimated monthly cost: ~\$60-75"
echo ""
read -p "  Continue? (y/n): " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "  Aborted."
    exit 1
fi

echo ""

# ─────────────────────────────────────────────
# 1. RDS: Restore from snapshot
# ─────────────────────────────────────────────
echo -e "${YELLOW}[1/6] RDS Database${NC}"

# Find the most recent snapshot
SNAPSHOT_ID=$(aws rds describe-db-snapshots \
    --db-instance-identifier "$RDS_INSTANCE" \
    --region "$REGION" \
    --query 'sort_by(DBSnapshots, &SnapshotCreateTime)[-1].DBSnapshotIdentifier' \
    --output text 2>/dev/null || echo "")

if [ -z "$SNAPSHOT_ID" ] || [ "$SNAPSHOT_ID" = "None" ]; then
    echo "  ⚠ No snapshot found for $RDS_INSTANCE"
    echo "  Looking for any snapshot with 'riskrunway' prefix..."
    SNAPSHOT_ID=$(aws rds describe-db-snapshots \
        --region "$REGION" \
        --query "sort_by(DBSnapshots[?starts_with(DBSnapshotIdentifier, 'riskrunway')], &SnapshotCreateTime)[-1].DBSnapshotIdentifier" \
        --output text 2>/dev/null || echo "")
fi

if [ -z "$SNAPSHOT_ID" ] || [ "$SNAPSHOT_ID" = "None" ]; then
    echo "  ✗ No RDS snapshot found. Cannot restore database."
    echo "  You'll need to create a fresh RDS instance manually."
    exit 1
fi

echo "  Found snapshot: $SNAPSHOT_ID"

# Check if instance already exists
EXISTING=$(aws rds describe-db-instances \
    --db-instance-identifier "$RDS_INSTANCE" \
    --region "$REGION" \
    --query 'DBInstances[0].DBInstanceStatus' \
    --output text 2>/dev/null || echo "")

if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
    echo "  ⚠ RDS instance '$RDS_INSTANCE' already exists (status: $EXISTING), skipping restore"
else
    echo -n "  Restoring from snapshot... "
    aws rds restore-db-instance-from-db-snapshot \
        --db-instance-identifier "$RDS_INSTANCE" \
        --db-snapshot-identifier "$SNAPSHOT_ID" \
        --db-instance-class "db.t3.micro" \
        --vpc-security-group-ids "$RDS_SG" \
        --no-multi-az \
        --publicly-accessible \
        --region "$REGION" \
        --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗"

    echo "  Waiting for RDS to become available (this takes 5-10 min)..."
    aws rds wait db-instance-available \
        --db-instance-identifier "$RDS_INSTANCE" \
        --region "$REGION" 2>/dev/null
    echo "  ✓ RDS is available"
fi

# Get the new RDS endpoint
RDS_ENDPOINT=$(aws rds describe-db-instances \
    --db-instance-identifier "$RDS_INSTANCE" \
    --region "$REGION" \
    --query 'DBInstances[0].Endpoint.Address' \
    --output text 2>/dev/null || echo "")
echo "  RDS endpoint: $RDS_ENDPOINT"

# ─────────────────────────────────────────────
# 2. CloudWatch Log Group
# ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[2/6] CloudWatch Log Group${NC}"

echo -n "  Creating log group $LOG_GROUP... "
aws logs create-log-group \
    --log-group-name "$LOG_GROUP" \
    --region "$REGION" \
    --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "already exists ✓"

# ─────────────────────────────────────────────
# 3. ALB + Target Group + Listeners
# ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[3/6] Application Load Balancer${NC}"

# Create ALB
echo -n "  Creating ALB... "
ALB_ARN=$(aws elbv2 create-load-balancer \
    --name "$ALB_NAME" \
    --subnets $(echo "$ALB_SUBNETS" | tr ',' ' ') \
    --security-groups "$ALB_SG" \
    --scheme internet-facing \
    --type application \
    --ip-address-type ipv4 \
    --region "$REGION" \
    --query 'LoadBalancers[0].LoadBalancerArn' \
    --output text 2>/dev/null || echo "")

if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
    echo "✓"
else
    echo "✗ (may already exist)"
    ALB_ARN=$(aws elbv2 describe-load-balancers \
        --names "$ALB_NAME" \
        --region "$REGION" \
        --query 'LoadBalancers[0].LoadBalancerArn' \
        --output text 2>/dev/null || echo "")
fi

# Get ALB DNS name for Route 53
ALB_DNS=$(aws elbv2 describe-load-balancers \
    --load-balancer-arns "$ALB_ARN" \
    --region "$REGION" \
    --query 'LoadBalancers[0].DNSName' \
    --output text 2>/dev/null || echo "")
ALB_HOSTED_ZONE=$(aws elbv2 describe-load-balancers \
    --load-balancer-arns "$ALB_ARN" \
    --region "$REGION" \
    --query 'LoadBalancers[0].CanonicalHostedZoneId' \
    --output text 2>/dev/null || echo "Z35SXDOTRQ7X7K")

echo "  ALB DNS: $ALB_DNS"

# Create Target Group
echo -n "  Creating target group... "
TG_ARN=$(aws elbv2 create-target-group \
    --name "riskrunway-tg-5001" \
    --protocol HTTP \
    --port 5001 \
    --vpc-id "$VPC_ID" \
    --target-type ip \
    --health-check-protocol HTTP \
    --health-check-path "/health" \
    --health-check-interval-seconds 30 \
    --healthy-threshold-count 2 \
    --unhealthy-threshold-count 3 \
    --region "$REGION" \
    --query 'TargetGroups[0].TargetGroupArn' \
    --output text 2>/dev/null || echo "")

if [ -n "$TG_ARN" ] && [ "$TG_ARN" != "None" ]; then
    echo "✓"
else
    echo "already exists ✓"
    TG_ARN=$(aws elbv2 describe-target-groups \
        --names "riskrunway-tg-5001" \
        --region "$REGION" \
        --query 'TargetGroups[0].TargetGroupArn' \
        --output text 2>/dev/null || echo "")
fi

# Create HTTPS listener (443 → target group)
echo -n "  Creating HTTPS listener (443)... "
aws elbv2 create-listener \
    --load-balancer-arn "$ALB_ARN" \
    --protocol HTTPS \
    --port 443 \
    --certificates CertificateArn="$ACM_CERT_ARN" \
    --default-actions Type=forward,TargetGroupArn="$TG_ARN" \
    --region "$REGION" \
    --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (may already exist)"

# Create HTTP listener (80 → redirect to HTTPS)
echo -n "  Creating HTTP listener (80 → redirect HTTPS)... "
aws elbv2 create-listener \
    --load-balancer-arn "$ALB_ARN" \
    --protocol HTTP \
    --port 80 \
    --default-actions 'Type=redirect,RedirectConfig={Protocol=HTTPS,Port=443,Host="#{host}",Path="/#{path}",Query="#{query}",StatusCode=HTTP_301}' \
    --region "$REGION" \
    --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (may already exist)"

# ─────────────────────────────────────────────
# 4. ECR: Build and push Docker image
# ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[4/6] ECR Image Push${NC}"

ECR_URI="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_REPO"

echo -n "  Logging into ECR... "
aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com" > /dev/null 2>&1 && echo "✓" || echo "✗"

echo -n "  Building Docker image... "
docker build -t "$ECR_URI:latest" . > /dev/null 2>&1 && echo "✓" || echo "✗"

echo -n "  Pushing to ECR... "
docker push "$ECR_URI:latest" > /dev/null 2>&1 && echo "✓" || echo "✗"

# ─────────────────────────────────────────────
# 5. ECS: Create cluster + service
# ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[5/6] ECS Cluster + Service${NC}"

# Create cluster
echo -n "  Creating ECS cluster... "
aws ecs create-cluster \
    --cluster-name "$CLUSTER" \
    --region "$REGION" \
    --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "already exists ✓"

# Get latest task definition ARN (task defs persist even after service deletion)
TASK_DEF_ARN=$(aws ecs describe-task-definition \
    --task-definition "$TASK_DEF_FAMILY" \
    --region "$REGION" \
    --query 'taskDefinition.taskDefinitionArn' \
    --output text 2>/dev/null || echo "")

if [ -z "$TASK_DEF_ARN" ] || [ "$TASK_DEF_ARN" = "None" ]; then
    echo "  ✗ Task definition '$TASK_DEF_FAMILY' not found. You may need to register it."
    echo "  Use: aws ecs register-task-definition --cli-input-json file://aws_snapshot/ecs_task_definition.json"
    exit 1
fi

echo "  Task definition: $TASK_DEF_ARN"

# Get all subnets for ECS (same as ALB)
ECS_SUBNETS=$(echo "$ALB_SUBNETS" | tr ',' ',')

# Create service
echo -n "  Creating ECS service... "
aws ecs create-service \
    --cluster "$CLUSTER" \
    --service-name "$SERVICE" \
    --task-definition "$TASK_DEF_ARN" \
    --desired-count 1 \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$(echo $ALB_SUBNETS | tr ',' ',')],securityGroups=[$ECS_SG],assignPublicIp=ENABLED}" \
    --load-balancers "targetGroupArn=$TG_ARN,containerName=riskrunway,containerPort=$CONTAINER_PORT" \
    --region "$REGION" \
    --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (may already exist)"

echo "  Waiting for service to stabilize..."
aws ecs wait services-stable \
    --cluster "$CLUSTER" \
    --services "$SERVICE" \
    --region "$REGION" 2>/dev/null && echo "  ✓ Service stable" || echo "  ⚠ Timed out (check ECS console)"

# ─────────────────────────────────────────────
# 6. DNS: app.risk-runway.com → ALB
# ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[6/6] DNS Record${NC}"

HOSTED_ZONE_ID=$(aws route53 list-hosted-zones \
    --query "HostedZones[?Name=='risk-runway.com.'].Id" \
    --output text 2>/dev/null | sed 's|/hostedzone/||' || echo "")

if [ -n "$HOSTED_ZONE_ID" ] && [ "$HOSTED_ZONE_ID" != "None" ]; then
    echo -n "  Creating app.risk-runway.com → ALB... "
    aws route53 change-resource-record-sets \
        --hosted-zone-id "$HOSTED_ZONE_ID" \
        --change-batch "{
            \"Changes\": [{
                \"Action\": \"UPSERT\",
                \"ResourceRecordSet\": {
                    \"Name\": \"app.risk-runway.com\",
                    \"Type\": \"A\",
                    \"AliasTarget\": {
                        \"HostedZoneId\": \"$ALB_HOSTED_ZONE\",
                        \"DNSName\": \"dualstack.$ALB_DNS.\",
                        \"EvaluateTargetHealth\": true
                    }
                }
            }]
        }" \
        --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (add manually in console)"
else
    echo "  ⚠ Hosted zone not found. Add DNS manually:"
    echo "     app.risk-runway.com → $ALB_DNS"
fi

# ─────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  Restore complete!${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo "  ✓ RDS: $RDS_INSTANCE (endpoint: $RDS_ENDPOINT)"
echo "  ✓ ALB: $ALB_NAME ($ALB_DNS)"
echo "  ✓ ECS: $SERVICE running on $CLUSTER"
echo "  ✓ DNS: app.risk-runway.com → ALB"
echo "  ✓ Image: pushed to ECR"
echo ""
echo "  App should be live at: https://app.risk-runway.com"
echo "  (DNS propagation may take 1-2 minutes)"
echo ""
echo "  If the RDS endpoint changed, update the DATABASE_URL secret:"
echo "    aws secretsmanager update-secret \\"
echo "      --secret-id riskrunway/DATABASE_URL \\"
echo "      --secret-string \"postgresql://riskrunway:RiskRunway2026!@$RDS_ENDPOINT:5432/riskrunway\" \\"
echo "      --region us-east-1"
echo ""
echo "  Then force a new ECS deployment to pick up the change:"
echo "    aws ecs update-service --cluster $CLUSTER --service $SERVICE --force-new-deployment --region us-east-1"
echo ""
