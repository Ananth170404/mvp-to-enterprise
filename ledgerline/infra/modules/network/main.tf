# A VPC for one environment (guide Chapter 4, section 4.7):
#
#   public subnets   load balancer, NAT gateway      (route to the internet gateway)
#   private subnets  app tasks, database             (no inbound from the internet;
#                                                     outbound through NAT)
#
# One public and one private subnet per availability zone.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

variable "name" {
  type = string
}

variable "cidr" {
  description = "The VPC's address range. Different per environment, so they could be connected later."
  type        = string
  default     = "10.20.0.0/16"
}

variable "az_count" {
  description = "Availability zones to span. Two is the minimum for a load balancer and a Multi-AZ database."
  type        = number
  default     = 2
}

variable "nat_per_az" {
  description = "One NAT gateway per AZ (resilient to an AZ failure) or one shared (about half the cost)."
  type        = bool
  default     = false
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)
  # /20 subnets: 4,096 addresses each. Public 0..n-1, private from 8 onwards.
  public_cidrs  = [for i in range(var.az_count) : cidrsubnet(var.cidr, 4, i)]
  private_cidrs = [for i in range(var.az_count) : cidrsubnet(var.cidr, 4, i + 8)]
  nat_count     = var.nat_per_az ? var.az_count : 1
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # RDS endpoints and VPC endpoints need DNS names
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

# ---------------------------------------------------------------- public

resource "aws_subnet" "public" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.this.id
  availability_zone       = local.azs[count.index]
  cidr_block              = local.public_cidrs[count.index]
  map_public_ip_on_launch = false # only things that need one (the load balancer) get one
  tags                    = { Name = "${var.name}-public-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------- NAT

# Lets private resources reach out (image pulls, the LLM API, Parameter
# Store) without being reachable from outside. The biggest fixed cost here:
# ~$0.059/hour in Sydney (~$43/month) per gateway, plus data processed.
resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"
  tags   = { Name = "${var.name}-nat-${count.index}" }
}

resource "aws_nat_gateway" "this" {
  count         = local.nat_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = { Name = "${var.name}-${local.azs[count.index]}" }
  depends_on    = [aws_internet_gateway.this]
}

# ---------------------------------------------------------------- private

resource "aws_subnet" "private" {
  count             = var.az_count
  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = local.private_cidrs[count.index]
  tags              = { Name = "${var.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table" "private" {
  count  = var.az_count
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    # Each AZ uses its own NAT when there's one per AZ; otherwise the shared one.
    nat_gateway_id = aws_nat_gateway.this[var.nat_per_az ? count.index : 0].id
  }
  tags = { Name = "${var.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# S3 traffic (uploaded documents, ECR image layers) goes straight to S3,
# skipping NAT and its per-GB charge. Gateway endpoints are free.
data "aws_region" "current" {}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id
  tags              = { Name = "${var.name}-s3" }
}

# ---------------------------------------------------------------- outputs

output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}
