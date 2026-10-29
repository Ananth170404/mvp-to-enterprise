# Step 3: Postgres on Amazon RDS.
#
# A managed database: AWS runs Postgres, patches it, takes automated backups
# and gives point-in-time recovery (guide 2.7). We choose the size and the
# safety settings.

# Free-plan accounts can't use every RDS option. AWS rejects the request with
# FreeTierRestrictionError (we hit it for backup retention). Instead of
# scattering free-plan workarounds, one switch picks safe values for either.
variable "free_plan" {
  description = "true while the AWS account is on the Free plan; set false after upgrading."
  type        = bool
  default     = true
}

locals {
  db = var.free_plan ? {
    backup_retention_days = 1     # free plan maximum
    storage_type          = "gp2" # the type the free tier covers
    max_storage_gb        = 0     # 0 = storage autoscaling off (stay within free storage)
    } : {
    backup_retention_days = 7
    storage_type          = "gp3"
    max_storage_gb        = 50
  }
}

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_db_instance" "main" {
  identifier = local.name

  engine                     = "postgres"
  engine_version             = "16" # same major version as local development
  auto_minor_version_upgrade = true # security patches applied in the maintenance window

  instance_class        = "db.t4g.micro" # smallest; plenty for staging
  allocated_storage     = 20
  max_allocated_storage = local.db.max_storage_gb # >0: storage grows automatically up to this
  storage_type          = local.db.storage_type
  storage_encrypted     = true

  db_name  = "ledgerline"
  username = "ledgerline" # schema owner; used only by migrations (guide 2.3)

  # RDS generates the master password and keeps it in Secrets Manager, rotated
  # automatically. It never appears in Terraform code or state.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false # no public IP, ever
  multi_az               = false # one AZ is fine for staging; production: true

  # Backups: daily snapshots + point-in-time recovery to any second within the
  # retention window. Retention decides how FAR BACK you can restore (1 day on
  # the free plan, 7 after upgrading). Data loss (RPO) stays about 5 minutes
  # either way, because transaction logs are archived continuously (guide 2.7).
  backup_retention_period = local.db.backup_retention_days
  backup_window           = "16:00-17:00"         # UTC = 21:30-22:30 IST, 03:00-04:00 Sydney
  maintenance_window      = "sun:17:30-sun:18:30" # UTC, after the backup
  copy_tags_to_snapshot   = true

  # Staging gets torn down to save credits, so deletion is allowed, but a
  # final snapshot is always taken first. Production: deletion_protection = true.
  deletion_protection       = false
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-final"

  apply_immediately = true # staging: don't wait for the maintenance window
}

output "db_endpoint" {
  description = "Hostname of the database (reachable only from inside the VPC)."
  value       = aws_db_instance.main.address
}

output "db_master_secret_arn" {
  description = "Secrets Manager secret holding the master (schema owner) credentials."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "subnet_ids" {
  value = data.aws_subnets.default.ids
}
