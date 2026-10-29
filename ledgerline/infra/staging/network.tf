# Networking for staging.
#
# Staging uses the account's DEFAULT VPC: it already exists, costs nothing,
# and has subnets in every Availability Zone. Its subnets are public, which
# is why the database below is explicitly NOT publicly accessible and only
# accepts connections from the app's security group.
#
# Production would get its own VPC with private subnets (guide Chapter 4).
# That costs more (NAT gateways or VPC endpoints), which is a real trade-off
# for a small staging environment.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# Security groups are firewalls attached to resources. Rules refer to other
# security groups rather than IP addresses, so "the app may talk to the
# database" stays true however many containers run and whatever IPs they get.

# The app's containers (ECS tasks, Step 5). Outbound: anywhere (ECR, S3,
# Gemini, the database). Inbound rules from the load balancer come in Step 5.
resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "Ledgerline app containers"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  description       = "All outbound"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# The database: Postgres port, and only from the app.
resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "Ledgerline Postgres"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  description                  = "Postgres from the app only"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app.id
}
