# Ledgerline on ONE server (guide Chapter 4, part 2): the cheapest real
# deployment, and a lesson in what a platform normally does for you.
#
#   EC2 (Ubuntu 24.04, t3.small) running Docker Compose:
#   Postgres + migrate + app + Caddy (automatic HTTPS via Let's Encrypt)
#
# No SSH: port 22 is closed and there's no key pair. You run commands through
# AWS Systems Manager, authenticated by your AWS login, logged in CloudTrail.
#
# State goes in the same bucket as staging, under its own key:
#   cd ledgerline/infra/vm-demo
#   terraform init "-backend-config=../staging/backend.hcl" "-backend-config=key=vm-demo/terraform.tfstate"
#   terraform apply -var image_tag=<a commit SHA that's in ECR>
#   ...
#   terraform destroy -var image_tag=<same>      (it's a demo: don't leave it running)

terraform {
  required_version = ">= 1.15"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.23"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = { project = "ledgerline", environment = "vm-demo", managed_by = "terraform", stack = "vm-demo" }
  }
}

variable "region" {
  type    = string
  default = "ap-southeast-2"
}

variable "aws_profile" {
  type    = string
  default = "ledgerline"
}

variable "image_tag" {
  description = "Image to run: a commit SHA already pushed to ECR by CI."
  type        = string
}

variable "instance_type" {
  description = "t3.small (2 GB) fits Postgres + app + Caddy; it's on the new Free plan's eligible list."
  type        = string
  default     = "t3.small"
}

data "aws_caller_identity" "current" {}

locals {
  registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
  image    = "${local.registry}/ledgerline:${var.image_tag}"
}

# Canonical publishes the current Ubuntu image ID in Parameter Store, so this
# always picks the latest patched 24.04 LTS image.
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_vpc" "default" {
  default = true
}

# ---------------------------------------------------------------- firewall

resource "aws_security_group" "vm" {
  name        = "ledgerline-vm-demo"
  description = "Ledgerline single-server demo: web only, no SSH"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.vm.id
  description       = "HTTP: Lets Encrypt challenges and redirect to HTTPS"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.vm.id
  description       = "HTTPS"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "http3" {
  security_group_id = aws_security_group.vm.id
  description       = "HTTP/3"
  ip_protocol       = "udp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.vm.id
  description       = "Packages, image pulls, Lets Encrypt"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ---------------------------------------------------------------- identity

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "vm" {
  name               = "ledgerline-vm-demo"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.vm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" # Systems Manager instead of SSH
}

resource "aws_iam_role_policy_attachment" "ecr" {
  role       = aws_iam_role.vm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly" # pull images
}

resource "aws_iam_instance_profile" "vm" {
  name = "ledgerline-vm-demo"
  role = aws_iam_role.vm.name
}

# ---------------------------------------------------------------- the server

resource "aws_instance" "vm" {
  ami                    = data.aws_ssm_parameter.ubuntu.value
  instance_type          = var.instance_type
  iam_instance_profile   = aws_iam_instance_profile.vm.name
  vpc_security_group_ids = [aws_security_group.vm.id]

  metadata_options {
    http_tokens = "required" # IMDSv2 only: blocks a class of credential-theft attacks (SSRF)
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
  }

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    registry  = local.registry
    image     = local.image
    compose   = file("${path.module}/../../deploy/vm/compose.yaml")
    caddyfile = file("${path.module}/../../deploy/vm/Caddyfile")
  })
  user_data_replace_on_change = true # changed setup = a fresh server (cattle, not pets)

  tags = { Name = "ledgerline-vm-demo" }
}

# ---------------------------------------------------------------- outputs

output "url" {
  description = "Ready a few minutes after apply (packages, image pull, certificate)."
  value       = "https://${replace(aws_instance.vm.public_ip, ".", "-")}.sslip.io"
}

output "instance_id" {
  value = aws_instance.vm.id
}
