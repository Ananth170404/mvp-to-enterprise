# ---------------------------------------------------------------- documents

# Uploaded invoices. Container disks vanish on every deploy, so files live
# here (STORAGE_BACKEND=s3). Versioning means an overwritten or deleted
# invoice can be recovered for 30 days.
module "documents" {
  source                  = "../private_bucket"
  name                    = "${local.name}-documents-${local.account_id}"
  noncurrent_version_days = 30
}
