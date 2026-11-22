# Firewalls for one environment. The network itself (VPC and subnets) is an
# input: staging passes the account's default VPC, production its own VPC
# with private subnets (guide Chapter 4).

# Security groups are firewalls attached to resources. Rules refer to other
# security groups rather than IP addresses, so "the app may talk to the
# database" stays true however many containers run and whatever IPs they get.

# The app's containers (ECS tasks, Step 5). Outbound: HTTPS to anywhere (ECR,
# S3, SQS, Gemini, the sign-in provider) and Postgres to the database only.
# Inbound rules from the load balancer come in Step 5. (DNS needs no rule:
# security groups don't filter the VPC's own resolver.)
resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "Ledgerline app containers"
  vpc_id      = var.vpc_id
}

# Was "all outbound" until Chapter 8: nothing the app does needs any other
# port, and a compromised container then can't reach, say, a mail server or an
# attacker's listener on an odd port.
resource "aws_vpc_security_group_egress_rule" "app_https" {
  security_group_id = aws_security_group.app.id
  description       = "HTTPS outbound"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "app_to_db" {
  security_group_id            = aws_security_group.app.id
  description                  = "Postgres to the database only"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.db.id
}

# The database: Postgres port, and only from the app.
resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "Ledgerline Postgres"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  description                  = "Postgres from the app only"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app.id
}
