#!/bin/bash
#
# AWS Teardown for RiskRunway App (app.risk-runway.com ONLY)
#
# TEARS DOWN:
#   - ECS service + cluster
#   - ALB + target groups + listeners
#   - RDS (with final snapshot for future restore)
#   - ECR images (keeps repo)
#   - CloudWatch log group
#   - DNS: app.risk-runway.com record
#   - DNS: paulin.risk-runway.com record (pilot cancelled)
#   - DNS: fixes www.risk-runway.com to point at CloudFront (was misconfigured to ALB)
#
# DOES NOT TOUCH:
#   - risk-runway.com apex (CloudFront marketing site)
#   - Route 53 hosted zone itself
#   - Secrets Manager secrets (cheap, keep them for restore)
#   - IAM roles (free)
#   - S3 bucket + uploaded docs
#   - VPC + subnets + security groups (free)
#   - SES/email config
#   - ACM certificate (free)
#
# Usage:
#   chmod +x scripts/aws_teardown.sh
#   ./scripts/aws_teardown.sh
#

set -euo pipefail

REGION="us-east-1"
CLUSTER="riskrunway"
SERVICE="riskrunway-service"
ECR_REPO="riskrunway-mapper"
ALB_NAME="riskrunway-alb"
RDS_INSTANCE="riskrunway-db"
LOG_GROUP="/ecs/riskrunway"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo -e "${RED}============================================================${NC}"
echo -e "${RED}  ⚠️  RiskRunway App TEARDOWN (app.risk-runway.com only)${NC}"
echo -e "${RED}============================================================${NC}"
echo ""
echo "  This will DELETE:"
echo "    • ECS service + cluster (stops Fargate billing)"
echo "    • ALB + listeners + target groups (stops LB billing)"
echo "    • RDS instance (final snapshot taken for restore)"
echo "    • ECR images (repo kept)"
echo "    • CloudWatch log group"
echo "    • DNS: app.risk-runway.com"
echo "    • DNS: paulin.risk-runway.com (pilot cancelled)"
echo "    • DNS: www → ALB (will re-point to CloudFront)"
echo ""
echo "  This will KEEP:"
echo "    • risk-runway.com → CloudFront (marketing site)"
echo "    • Route 53 hosted zone"
echo "    • Secrets Manager secrets"
echo "    • IAM roles, ACM cert, VPC, S3"
echo ""
echo -e "${YELLOW}  Estimated savings: ~\$55-70/month${NC}"
echo ""
echo -e "${RED}  RDS data preserved via final snapshot.${NC}"
echo ""
read -p "  Type 'TEARDOWN' to proceed: " CONFIRM

if [ "$CONFIRM" != "TEARDOWN" ]; then
    echo "  Aborted."
    exit 1
fi

echo ""
echo "Starting teardown..."
echo ""

# ─────────────────────────────────────────────
# 1. ECS: Scale to 0, then delete service
# ─────────────────────────────────────────────
echo -e "${YELLOW}[1/6] ECS Service${NC}"

echo -n "  Scaling service to 0... "
aws ecs update-service \
    --cluster "$CLUSTER" \
    --service "$SERVICE" \
    --desired-count 0 \
    --region "$REGION" \
    --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (may already be at 0)"

echo -n "  Waiting for tasks to drain (up to 2 min)... "
aws ecs wait services-stable \
    --cluster "$CLUSTER" \
    --services "$SERVICE" \
    --region "$REGION" 2>/dev/null && echo "✓" || echo "✓ (timed out, continuing)"

echo -n "  Deleting service... "
aws ecs delete-service \
    --cluster "$CLUSTER" \
    --service "$SERVICE" \
    --region "$REGION" \
    --force \
    --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (may not exist)"

echo -n "  Deleting cluster... "
aws ecs delete-cluster \
    --cluster "$CLUSTER" \
    --region "$REGION" \
    --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (may have active resources)"

# ─────────────────────────────────────────────
# 2. ALB: Delete listeners, target groups, then ALB
# ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[2/6] Application Load Balancer${NC}"

ALB_ARN=$(aws elbv2 describe-load-balancers \
    --names "$ALB_NAME" \
    --region "$REGION" \
    --query 'LoadBalancers[0].LoadBalancerArn' \
    --output text 2>/dev/null || echo "")

if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
    LISTENER_ARNS=$(aws elbv2 describe-listeners \
        --load-balancer-arn "$ALB_ARN" \
        --region "$REGION" \
        --query 'Listeners[].ListenerArn' \
        --output text 2>/dev/null || echo "")

    for LARN in $LISTENER_ARNS; do
        echo -n "  Deleting listener... "
        aws elbv2 delete-listener --listener-arn "$LARN" --region "$REGION" --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗"
    done

    echo -n "  Deleting load balancer... "
    aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN" --region "$REGION" --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗"

    echo -n "  Waiting for ALB to deregister (30s)... "
    sleep 30
    echo "✓"
else
    echo "  ⚠ ALB not found, skipping"
fi

TG_ARNS=$(aws elbv2 describe-target-groups \
    --region "$REGION" \
    --query 'TargetGroups[].TargetGroupArn' \
    --output text 2>/dev/null || echo "")

for TG_ARN in $TG_ARNS; do
    TG_NAME=$(aws elbv2 describe-target-groups --target-group-arns "$TG_ARN" --region "$REGION" \
        --query 'TargetGroups[0].TargetGroupName' --output text 2>/dev/null || echo "unknown")
    echo -n "  Deleting target group ($TG_NAME)... "
    aws elbv2 delete-target-group --target-group-arn "$TG_ARN" --region "$REGION" --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗"
done

# ─────────────────────────────────────────────
# 3. RDS: Final snapshot, then delete
# ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[3/6] RDS Database${NC}"

SNAPSHOT_ID="riskrunway-final-$(date +%Y%m%d)"

echo -n "  Deleting RDS instance (final snapshot: $SNAPSHOT_ID)... "
aws rds delete-db-instance \
    --db-instance-identifier "$RDS_INSTANCE" \
    --final-db-snapshot-identifier "$SNAPSHOT_ID" \
    --region "$REGION" \
    --no-cli-pager > /dev/null 2>&1 && echo "✓ (deletion in progress, 5-10 min)" || echo "✗ (may not exist)"

echo "  ℹ  Restore later with:"
echo "     aws rds restore-db-instance-from-db-snapshot \\"
echo "       --db-instance-identifier riskrunway-db \\"
echo "       --db-snapshot-identifier $SNAPSHOT_ID \\"
echo "       --db-instance-class db.t3.micro \\"
echo "       --region us-east-1"

# ─────────────────────────────────────────────
# 4. ECR: Delete images (keep repo for future push)
# ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[4/6] ECR (cleaning images, keeping repo)${NC}"

IMAGE_IDS=$(aws ecr list-images \
    --repository-name "$ECR_REPO" \
    --region "$REGION" \
    --query 'imageIds[*]' \
    --output json 2>/dev/null || echo "[]")

if [ "$IMAGE_IDS" != "[]" ] && [ -n "$IMAGE_IDS" ]; then
    echo -n "  Deleting all images from $ECR_REPO... "
    aws ecr batch-delete-image \
        --repository-name "$ECR_REPO" \
        --image-ids "$IMAGE_IDS" \
        --region "$REGION" \
        --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗"
else
    echo "  No images to delete"
fi

# ─────────────────────────────────────────────
# 5. CloudWatch Log Group
# ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[5/6] CloudWatch Logs${NC}"

echo -n "  Deleting log group $LOG_GROUP... "
aws logs delete-log-group \
    --log-group-name "$LOG_GROUP" \
    --region "$REGION" \
    --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (may not exist)"

# ─────────────────────────────────────────────
# 6. DNS: Remove app + paulin, fix www → CloudFront
# ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[6/6] DNS Records${NC}"

HOSTED_ZONE_ID=$(aws route53 list-hosted-zones \
    --query "HostedZones[?Name=='risk-runway.com.'].Id" \
    --output text 2>/dev/null | sed 's|/hostedzone/||' || echo "")

if [ -n "$HOSTED_ZONE_ID" ] && [ "$HOSTED_ZONE_ID" != "None" ]; then

    # Delete app.risk-runway.com → ALB
    echo -n "  Deleting app.risk-runway.com... "
    aws route53 change-resource-record-sets \
        --hosted-zone-id "$HOSTED_ZONE_ID" \
        --change-batch '{
            "Changes": [{
                "Action": "DELETE",
                "ResourceRecordSet": {
                    "Name": "app.risk-runway.com",
                    "Type": "A",
                    "AliasTarget": {
                        "HostedZoneId": "Z35SXDOTRQ7X7K",
                        "DNSName": "dualstack.riskrunway-alb-2143690742.us-east-1.elb.amazonaws.com.",
                        "EvaluateTargetHealth": true
                    }
                }
            }]
        }' \
        --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (manual cleanup needed)"

    # Delete www.risk-runway.com → ALB (misconfigured, should be CloudFront)
    echo -n "  Deleting www.risk-runway.com → ALB (misconfigured)... "
    aws route53 change-resource-record-sets \
        --hosted-zone-id "$HOSTED_ZONE_ID" \
        --change-batch '{
            "Changes": [{
                "Action": "DELETE",
                "ResourceRecordSet": {
                    "Name": "www.risk-runway.com",
                    "Type": "A",
                    "AliasTarget": {
                        "HostedZoneId": "Z35SXDOTRQ7X7K",
                        "DNSName": "dualstack.riskrunway-alb-2143690742.us-east-1.elb.amazonaws.com.",
                        "EvaluateTargetHealth": true
                    }
                }
            }]
        }' \
        --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (manual cleanup needed)"

    # Re-create www.risk-runway.com → CloudFront (same as apex)
    # Z2FDTNDATAQYW2 is the fixed hosted zone ID for all CloudFront distributions
    echo -n "  Creating www.risk-runway.com → CloudFront (marketing site)... "
    aws route53 change-resource-record-sets \
        --hosted-zone-id "$HOSTED_ZONE_ID" \
        --change-batch '{
            "Changes": [{
                "Action": "CREATE",
                "ResourceRecordSet": {
                    "Name": "www.risk-runway.com",
                    "Type": "A",
                    "AliasTarget": {
                        "HostedZoneId": "Z2FDTNDATAQYW2",
                        "DNSName": "d2v7sob7c0452c.cloudfront.net.",
                        "EvaluateTargetHealth": false
                    }
                }
            }]
        }' \
        --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (add manually in console)"

    # Delete paulin.risk-runway.com → ALB (pilot cancelled, not restoring)
    echo -n "  Deleting paulin.risk-runway.com (pilot cancelled)... "
    aws route53 change-resource-record-sets \
        --hosted-zone-id "$HOSTED_ZONE_ID" \
        --change-batch '{
            "Changes": [{
                "Action": "DELETE",
                "ResourceRecordSet": {
                    "Name": "paulin.risk-runway.com",
                    "Type": "A",
                    "AliasTarget": {
                        "HostedZoneId": "Z35SXDOTRQ7X7K",
                        "DNSName": "dualstack.riskrunway-alb-2143690742.us-east-1.elb.amazonaws.com.",
                        "EvaluateTargetHealth": true
                    }
                }
            }]
        }' \
        --no-cli-pager > /dev/null 2>&1 && echo "✓" || echo "✗ (manual cleanup needed)"

else
    echo "  ⚠ Hosted zone not found, skip DNS cleanup"
fi

# ─────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  Teardown complete!${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
echo "  Removed:"
echo "    ✓ ECS service + cluster"
echo "    ✓ ALB + listeners + target groups"
echo "    ✓ RDS instance (snapshot: $SNAPSHOT_ID)"
echo "    ✓ ECR images"
echo "    ✓ CloudWatch log group"
echo "    ✓ DNS: app.risk-runway.com"
echo "    ✓ DNS: paulin.risk-runway.com"
echo "    ✓ DNS: www.risk-runway.com → now points to CloudFront"
echo ""
echo "  Still running (free/cheap):"
echo "    • Secrets Manager (~\$5.60/month)"
echo "    • Route 53 hosted zone (\$0.50/month)"
echo "    • S3 bucket (pennies)"
echo "    • IAM roles, ACM cert, VPC (free)"
echo "    • ECR repo (free, no images)"
echo "    • RDS snapshot (free for first, then storage cost)"
echo ""
echo "  To restore app.risk-runway.com later:"
echo "    1. Restore RDS: aws rds restore-db-instance-from-db-snapshot ..."
echo "    2. Recreate ALB + target group + HTTPS listener"
echo "    3. Recreate ECS service (task def still registered)"
echo "    4. Add DNS: app.risk-runway.com → new ALB"
echo "    5. Push image to ECR + deploy"
echo ""
echo "  (See aws_snapshot/ for all original config values)"
echo ""
