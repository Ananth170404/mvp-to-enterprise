# Chapter 2, part 2: Staging on AWS with Terraform

> **In this part:** taking the production-ready app from [part 1](02-production-readiness.md) and running it on AWS as a real staging environment. It covers what Terraform is and how a Terraform project is laid out; how to set up a new AWS account and log in safely; and the environment itself, built in six steps: remote state, a registry, a database, secrets, CI publishing images, and the running app behind HTTPS. Then come day-to-day operations, the problems we actually hit, and what it all costs.

Everything here was built and deployed for real while writing it, in October 2026, on an AWS account created with the new sign-up experience. Where AWS, Terraform or Windows got in the way, the problem and the fix are written down, because you will probably hit them too.

---

## Contents

- [2B.1 What we're building](#2b1-what-were-building)
- [2B.2 Infrastructure as code, and Terraform in one sitting](#2b2-infrastructure-as-code-and-terraform-in-one-sitting)
- [2B.3 The AWS account](#2b3-the-aws-account)
- [2B.4 How the infra folder is laid out](#2b4-how-the-infra-folder-is-laid-out)
- [2B.5 Step 1: Remote state](#2b5-step-1-remote-state)
- [2B.6 Step 2: Image registry and file storage](#2b6-step-2-image-registry-and-file-storage)
- [2B.7 Step 3: Network and database](#2b7-step-3-network-and-database)
- [2B.8 Step 4: Secrets](#2b8-step-4-secrets)
- [2B.9 Step 5: Letting CI publish images (OIDC, and the fallback we used)](#2b9-step-5-letting-ci-publish-images)
- [2B.10 Step 6: Running the app](#2b10-step-6-running-the-app)
- [2B.11 Day-to-day operations](#2b11-day-to-day-operations)
- [2B.12 Troubleshooting: what actually went wrong](#2b12-troubleshooting-what-actually-went-wrong)
- [2B.13 Cost](#2b13-cost)
- [2B.14 By stage and product type](#2b14-by-stage-and-product-type)
- [2B.15 Checklist](#2b15-checklist)

---

## 2B.1 What we're building

A staging environment is a copy of production that real people can use before customers do (part 1, [2.2](02-production-readiness.md#22-environments)). It must be built **the same way** production will be: same image, same kind of database, same way of handling secrets and deploys. Otherwise it proves nothing.

```
                     HTTPS                     HTTP (only from CloudFront)
  browser ──────────────▶ CloudFront ──────────────▶ load balancer
                          (free certificate)          │  health check: /readyz
                                                      ▼
                                              app container (ECS Fargate)
                                               │            │
                              Postgres (RDS) ◀─┘            └─▶ S3 (uploaded files)
                                    ▲
                                    │ runs before every deploy
                            migration task (same image)

  GitHub Actions: test ─▶ build image ─▶ push to ECR ─▶ migrate ─▶ roll out ─▶ verify
```

Every arrow is something that has to be allowed explicitly, and every box is defined in code.

| Piece | AWS service | Why |
|---|---|---|
| Container registry | ECR | Somewhere to keep the tested images, tagged by commit |
| Database | RDS for PostgreSQL | Patched, backed up and restorable by AWS |
| File storage | S3 | Container disks vanish on every deploy |
| Secrets | SSM Parameter Store, Secrets Manager | Passwords outside the code, the image and Terraform's records |
| Running containers | ECS on Fargate | Runs containers without servers to manage, and replaces them when they die |
| Load balancer | Application Load Balancer (ALB) | One stable address in front of containers that come and go |
| HTTPS | CloudFront | A valid certificate without owning a domain |
| Logs | CloudWatch Logs | Everything the containers print |

> **Why not something simpler?** A single small server running Docker Compose would cost about a third as much, and it's a legitimate MVP choice ([2.12](02-production-readiness.md#212-cost)). This guide uses managed services because they are what most teams grow into, and the patterns (separate migrations, health-checked rolling deploys, injected secrets) carry over to any platform. Chapter 4 compares the options properly.

---

## 2B.2 Infrastructure as code, and Terraform in one sitting

### The problem it solves

You could build all of the above by clicking through the AWS console. It would work, once. Then:

- Nobody can say exactly how it's configured, including you, three months later.
- Building production "the same way" means repeating 200 clicks from memory.
- A change can't be reviewed, because there's nothing to review.
- Deleting staging to save money is scary, because you couldn't rebuild it.

**Infrastructure as code (IaC)** means describing the infrastructure in text files that live in the repository next to the app. A tool reads them and makes the cloud match. Changes go through the same pull requests and history as code.

> **If it isn't in code, it's a pet. If it's in code, you can delete it on Friday and rebuild it on Monday.**

### What Terraform is

Terraform (by HashiCorp) is the most widely used IaC tool. You write **declarative** configuration: you describe *what should exist*, not the steps to create it. Terraform works out the steps.

```hcl
resource "aws_s3_bucket" "documents" {
  bucket = "ledgerline-staging-documents-123456789012"
}
```

This says "a bucket with this name should exist". If it doesn't, Terraform creates it. If it exists with different settings, Terraform changes them. If you delete these lines, Terraform deletes the bucket.

The language is **HCL** (HashiCorp Configuration Language). Files end in `.tf`.

Alternatives, briefly:

| Tool | Notes |
|---|---|
| **OpenTofu** | An open-source fork of Terraform under the Linux Foundation. The same language; most of this guide works unchanged |
| **Pulumi** | Infrastructure in TypeScript, Python, Go and so on. Good if you want loops and tests in a general-purpose language |
| **AWS CDK / CloudFormation** | AWS's own tools; AWS only |
| **Console clicks plus documentation** | Fine for a one-off experiment, never for something you'll rebuild |

Pick one and stay with it. The concepts below (state, plan and apply, modules) exist in all of them.

### The building blocks

| Block | What it is | Example from this project |
|---|---|---|
| `provider` | A plugin that knows how to talk to one platform. Terraform downloads it on `init` | `provider "aws" { region = "ap-southeast-2" }` |
| `resource` | Something Terraform creates and manages | `resource "aws_db_instance" "main" { ... }` |
| `data` | Something Terraform only **reads**: it already exists, or someone else manages it | `data "aws_vpc" "default" { default = true }` |
| `ephemeral` | A value that exists only during one run and is never saved | `ephemeral "random_password" "site" { ... }` |
| `variable` | An input, with a type and a default | `variable "region" { default = "ap-southeast-2" }` |
| `locals` | Named values computed inside the configuration (helper variables) | `local.name = "ledgerline-staging"` |
| `output` | A value printed after `apply`, and readable later with `terraform output` | `output "staging_url" { ... }` |
| `module` | A reusable folder of Terraform: its own variables, resources and outputs. Terraform's version of a function | `module "documents" { source = "../modules/private_bucket" }` |

Resources refer to each other by address, `type.name.attribute`:

```hcl
resource "aws_ecr_repository" "app" { name = "ledgerline" }

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url   # read an attribute of another resource
}
```

These references are also how Terraform works out the **order** to do things in. If B mentions A, A is created first. You almost never write the order yourself.

### State: how Terraform remembers

Terraform keeps a **state file** that maps each resource in your code to the real thing in the cloud. For example, `aws_db_instance.main` maps to database `ledgerline-staging`. Without it, Terraform wouldn't know what it created, and would try to create everything again.

State matters more than beginners expect:

- **Lose it, and Terraform forgets everything it built.** The resources keep running, unmanaged.
- **Two people applying at once can corrupt it**, so it needs a lock.
- **It can contain secrets.** Any password passed as an ordinary value is stored in it as plain text. Treat state as sensitive, and keep secrets out of it ([2B.8](#2b8-step-4-secrets) shows how).
- **Never commit it to git.** Add `*.tfstate` and `*.tfstate.*` to `.gitignore`.

So state lives in a **remote backend**: here, a private, versioned, encrypted S3 bucket with locking. That's [Step 1](#2b5-step-1-remote-state).

### The workflow

```bash
terraform init      # download providers and modules, connect to the state backend (once per folder, and after adding providers)
terraform fmt       # format the files (like a code formatter)
terraform validate  # check syntax and references, without touching the cloud
terraform plan      # compare code with reality and show what WOULD change
terraform apply     # show the plan again, ask "yes", then make the changes
terraform output    # print the outputs
terraform destroy   # delete everything this folder manages
```

**Read every plan.** The summary line is the most important line Terraform prints:

```
Plan: 3 to add, 1 to change, 0 to destroy.
```

`to destroy` on a database, or a `-/+` (replace) on something holding data, means stop and understand why before typing `yes`. Some changes can't be made in place, so Terraform deletes the resource and creates a new one. For a database, that's data loss, even though the code change looked harmless.

### Files Terraform creates on your machine

| File or folder | What it is | Commit it? |
|---|---|---|
| `.terraform/` | Downloaded providers (the AWS provider is ~780 MB on Windows) and backend settings | **Never.** GitHub rejects files over 100 MB, and it's machine-specific |
| `.terraform.lock.hcl` | The exact provider versions and checksums used. Terraform's equivalent of a dependency lock file | **Yes.** Everyone, including CI, gets the same provider versions |
| `terraform.tfstate`, `*.tfstate.backup` | Local state (only where there's no remote backend) | **Never.** Keep it safe outside git |
| `*.tfplan` | A saved plan | No |
| `backend.hcl` | Your backend settings (this project's convention) | No. It names your account's bucket |

The `.gitignore` for a Terraform project:

```gitignore
.terraform/
*.tfstate
*.tfstate.*
*.tfplan
crash.log
backend.hcl
```

> **Gitignore patterns must start at the beginning of the line.** We lost an evening to a `.gitignore` whose Terraform lines had been pasted with leading spaces, so they matched nothing. `git add -A` then staged the 776 MB provider, and GitHub rejected the push. Check with `git check-ignore -v ledgerline/infra/staging/.terraform`: it prints the matching rule, or nothing.

### Versions used here

- Terraform **1.15 or newer**. Write-only arguments (Step 4) need 1.11; this project pins `>= 1.15`.
- AWS provider **`~> 6.23`**, meaning 6.23 or newer, but below 7. Version 6.23 is the first that understands profiles created by `aws login`.
- Random provider **`~> 3.7`**, for ephemeral passwords.

`~>` allows the last listed part of the version to rise. That gives you bug fixes without surprise major versions.

---

## 2B.3 The AWS account

### Two ways to sign up (as of October 2026)

Since September 2026, AWS offers two sign-up paths:

| | **Sign up for AWS (new)** | **Sign up for AWS (advanced)**, the classic account |
|---|---|---|
| Owner | Your AWS Builder ID; **no root user** to protect | A root user (email and password) you must lock down yourself |
| Structure | An AWS Organization made for you, with *projects*, each its own account | One account; add Organizations yourself |
| Region | **Assigned per project** (from your location; India was assigned Sydney, `ap-southeast-2`), and can't be changed | Any region |
| Guard rails | AWS-managed **service control policies (SCPs)** you can't edit | None until you add them |
| Free plan | $100 in credits for 182 days, limited to the services AWS lists | Older free tier model |
| Settings | settings.aws.com | Console and Billing |

The new experience is safer for beginners, but its guard rails block some standard practices. In this project, it blocked keyless CI login ([Step 5](#2b9-step-5-letting-ci-publish-images)) and some database settings ([Step 3](#2b7-step-3-network-and-database)). The policies are published in [AWS's documentation](https://docs.aws.amazon.com/accounts/latest/reference/scps-and-rcps-for-projects.html). Read them before choosing an architecture.

> **"Activate advanced features" is a one-way door.** It lifts most restrictions (including the one on OIDC providers) but can't be undone. Chapter 0's rule applies: don't walk through a one-way door to fix one inconvenience when a two-way door exists.

### Logging in from the terminal

Never create long-lived access keys for yourself, and never create keys for a root user. With a recent AWS CLI v2, `aws login` opens the browser, signs you in, and stores **temporary** credentials (about 12 hours) under a named profile:

```bash
aws --version                     # AWS CLI v2, recent enough to have `aws login`
aws login --profile ledgerline
aws sts get-caller-identity --profile ledgerline   # who am I? (account and role)
```

Every command in this guide passes `--profile ledgerline` (and `--region`), and Terraform's provider block names the same profile. When the session expires, run `aws login` again.

> **Never paste credentials anywhere**: not into a chat, a ticket, an AI assistant or a file. That includes the output of `aws configure export-credentials` and the `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` environment variables. Even "temporary" credentials are full access until they expire. If you do paste them, end the session and log in again.

### First use of a service can fail once

The first time an account uses ECS, AWS creates a **service-linked role** for it behind the scenes. A request made seconds later can fail with *"Unable to assume the service linked role"*, because IAM changes take a moment to reach every region (*eventual consistency*). Retrying succeeded. If a first-ever use of a service fails with a role error, wait a minute and retry before debugging anything.

> **Worked example: the Ledgerline account.** It was created with the new experience: a project account in Sydney (`ap-southeast-2`) on the Free plan. Before writing any Terraform, we ran a throwaway Fargate task with the CLI to check that containers were allowed. They were, after one retry for the service-linked role. Testing the riskiest assumption first is Chapter 1's lesson applied to infrastructure.

### Tools

| Tool | Used for |
|---|---|
| Terraform ≥ 1.15 | Everything in `infra/` |
| AWS CLI v2 | Login, one-off commands, checks |
| GitHub CLI (`gh`) | Setting secrets and variables, watching CI runs |
| Docker | Local development (images for AWS are built by CI) |

---

## 2B.4 How the infra folder is laid out

```
ledgerline/infra/
├── bootstrap/
│   └── main.tf              Step 1: the S3 bucket that stores Terraform state (local state, run once)
├── modules/
│   └── private_bucket/
│       └── main.tf          A reusable "secure S3 bucket" (used for documents)
└── staging/                 One environment; production would be a sibling folder
    ├── main.tf              Step 2: versions, backend, provider, variables, ECR, documents bucket
    ├── network.tf           Step 3: default VPC lookup, security groups for app and database
    ├── database.tf          Step 3: RDS Postgres, the free_plan switch
    ├── secrets.tf           Step 4: SSM parameters with write-only values
    ├── github_ci.tf         Step 5 and 6: the CI identity and what it may do
    ├── app.tf               Step 6: logs, IAM roles, task definitions, load balancer, ECS, CloudFront
    ├── backend.hcl.example  Template for backend.hcl (copy, fill in, never commit)
    └── .terraform.lock.hcl  Provider versions (created by init; committed)
```

Three things to know about this layout:

1. **Every `.tf` file in a folder is part of one configuration.** Terraform reads them all together, in no particular order. The split into `network.tf`, `database.tf` and so on is purely for people, and `app.tf` can refer to `aws_db_instance.main` from `database.tf` freely. A good rule: one file per concern, named after it.
2. **Each folder has its own state.** `bootstrap/` and `staging/` are applied separately, with separate `init`, `plan` and `apply`. A mistake in staging can't touch the bootstrap bucket, and production (later) can't be touched by a staging apply. Splitting state by environment limits the blast radius.
3. **Modules are folders too**, but they aren't applied on their own. They're called from an environment folder.

The app code also changed for AWS. Configuration now accepts database connection *parts*, the CI workflow gained publish and deploy jobs, and there's a deploy script:

```
ledgerline/app/config.py          database_url_from_env(): build the URL from DB_HOST, DB_USER, DB_PASSWORD...
ledgerline/app/main.py            login cookie marked Secure on deployed environments
ledgerline/scripts/deploy_ecs.sh  migrate, then roll out, then verify (run by CI)
.github/workflows/ci.yml          build job pushes the image; deploy-staging job deploys it
```

---

## 2B.5 Step 1: Remote state

**Goal:** a safe home for Terraform's state before anything else exists.

### The chicken-and-egg problem

State should live in S3, but the S3 bucket must be created by something, and Terraform needs somewhere to keep *that* state. The standard answer is a tiny **bootstrap** configuration that creates only the state bucket. It keeps its own small state locally. You run it once, and keep its `terraform.tfstate` somewhere safe outside git, such as a password manager or a private backup.

### What the bucket needs

[`bootstrap/main.tf`](../ledgerline/infra/bootstrap/main.tf):

| Setting | Why |
|---|---|
| Name includes the account ID: `ledgerline-tfstate-<account-id>` | S3 bucket names are global across all AWS customers |
| **Versioning** on; old versions expire after 90 days | A corrupted or bad state can be rolled back |
| **Encryption** (`AES256`) | State is sensitive |
| **Public access block**, `BucketOwnerEnforced` | No way to make it public by accident |
| A bucket policy denying non-HTTPS access | Encrypted in transit, always |
| `lifecycle { prevent_destroy = true }` | `terraform destroy` refuses to delete it |
| `default_tags` on the provider | Every resource is labelled with its project and owner in cost reports |

```hcl
resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket

  # Refuse `terraform destroy` on this bucket: losing state means Terraform
  # forgets everything it created.
  lifecycle {
    prevent_destroy = true
  }
}
```

```bash
cd ledgerline/infra/bootstrap
terraform init
terraform plan          # expect: 7 to add
terraform apply
terraform output state_bucket
```

### Pointing an environment at the bucket

Every other folder declares an empty `backend "s3" {}` block. The settings come from a separate file, because a backend block can't use variables, and the bucket name is specific to your account:

```hcl
# staging/backend.hcl (from backend.hcl.example; not committed)
bucket       = "ledgerline-tfstate-YOUR_ACCOUNT_ID"
key          = "staging/terraform.tfstate"   # one key per environment
region       = "ap-southeast-2"
profile      = "ledgerline"
use_lockfile = true                          # S3-native locking: two applies can't run at once
encrypt      = true
```

```bash
cd ledgerline/infra/staging
cp backend.hcl.example backend.hcl            # Windows: copy backend.hcl.example backend.hcl
terraform init -backend-config=backend.hcl
```

> **PowerShell quoting.** PowerShell splits `-backend-config=backend.hcl` at the `=`, and Terraform answers *"No positional arguments are expected"*. Quote the whole argument: `terraform init "-backend-config=backend.hcl"`.

`use_lockfile = true` uses S3's own conditional writes to lock state. Older guides use a DynamoDB table for locking. Terraform no longer needs one, so it's one less resource to create.

---

## 2B.6 Step 2: Image registry and file storage

**Goal:** somewhere for CI to put images, and somewhere for the app to put files.

### The container registry (ECR)

From [`staging/main.tf`](../ledgerline/infra/staging/main.tf):

```hcl
resource "aws_ecr_repository" "app" {
  name                 = "ledgerline"
  image_tag_mutability = "IMMUTABLE"   # a tag, once pushed, can never point at different code

  image_scanning_configuration {
    scan_on_push = true                # basic vulnerability scan of every pushed image
  }
}
```

- **Immutable tags** make "build once, promote the same image" trustworthy. `ledgerline:1bf430f…` means the same bytes forever. The trade-off is that pushing the same tag twice fails. That's deliberate: CI should never rebuild a commit and silently replace what was tested.
- **A lifecycle policy** keeps the 20 most recent images and deletes older ones, so storage costs stay flat.
- **Scan on push** runs a basic CVE scan. Read the findings with:

  ```bash
  aws ecr describe-image-scan-findings --repository-name ledgerline --image-id imageTag=<sha> \
    --query imageScanFindings.findingSeverityCounts
  ```

  Ledgerline's first image reported 3 HIGH, 9 MEDIUM and 3 LOW findings, mostly from packages in the Debian base image. Triage is Chapter 8's job. For now, the point is that you can see them.

### The documents bucket, via a module

Every bucket that holds real data needs the same settings: private, encrypted, versioned, HTTPS-only. Repeating them for each bucket invites the one bucket that forgot one. So they live in a **module**, [`modules/private_bucket`](../ledgerline/infra/modules/private_bucket/main.tf):

```hcl
module "documents" {
  source                  = "../modules/private_bucket"
  name                    = "${local.name}-documents-${local.account_id}"
  noncurrent_version_days = 30    # overwritten or deleted invoices recoverable for 30 days
}
```

Inside, the module has `variable` blocks for its inputs (`name`, `noncurrent_version_days`, `force_destroy`), resources for the bucket and its settings, and `output` blocks (`name`, `arn`) that the caller reads as `module.documents.name`.

The app writes uploaded invoices here because **container disks are thrown away on every deploy**. The app refuses to start on a deployed environment without `STORAGE_BACKEND=s3` (part 1, [2.3](02-production-readiness.md#23-configuration-and-secrets)).

```bash
terraform plan     # Step 2: expect 9 to add
terraform apply
```

---

## 2B.7 Step 3: Network and database

**Goal:** a managed Postgres the app can reach and nothing else can.

### Networking: the default VPC, on purpose

A **VPC** is your private network in AWS, and **subnets** are its slices, one per availability zone (AZ, a separate data centre). Every account comes with a *default VPC* whose subnets are **public**: resources in them *can* have internet addresses.

[`staging/network.tf`](../ledgerline/infra/staging/network.tf) uses the default VPC rather than building one:

| | Default VPC (this staging) | Custom VPC with private subnets (production, Chapter 4) |
|---|---|---|
| Cost | Free | NAT gateways ~$33+/month each, or VPC endpoints |
| Isolation | Done with security groups and "not publicly accessible" | Stronger: resources have no route to or from the internet |
| Effort | Two `data` lookups | A module and some networking knowledge |

The trade-off is acceptable for staging *because* every resource is locked down individually.

### Security groups refer to each other, not to IP addresses

A **security group** is a firewall attached to a resource. The rules here say *who* may connect, by naming another security group:

```hcl
resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app.id   # "from anything in the app group"
}
```

"The app may talk to the database" stays true however many containers run and whatever addresses they get. By the end of Step 6, the chain is:

```
CloudFront's address list ──80──▶ [alb] ──8000──▶ [app] ──5432──▶ [db]
```

Each group allows only the one before it.

### The database

[`staging/database.tf`](../ledgerline/infra/staging/database.tf), the decisions that matter:

| Setting | Value | Why |
|---|---|---|
| `engine_version` | `"16"` | The same major version as local development. Minor versions update automatically in the maintenance window |
| `instance_class` | `db.t4g.micro` | The smallest, and plenty for staging |
| `storage_encrypted` | `true` | Encryption at rest costs nothing |
| `manage_master_user_password` | `true` | **RDS generates the owner's password and keeps it in Secrets Manager.** It never appears in code or Terraform state |
| `username` | `ledgerline` | The schema owner, used **only** by migrations. The app uses the restricted user `ledgerline_app` (part 1, 2.3) |
| `publicly_accessible` | `false` | No public address, ever |
| `multi_az` | `false` | One AZ is fine for staging; production: `true` |
| `backup_retention_period` | 1 on the Free plan, else 7 | Point-in-time restore window ([2.7](02-production-readiness.md#27-backups-and-recovery)) |
| `deletion_protection` / `skip_final_snapshot` | `false` / `false` | Staging may be deleted, but **a final snapshot is always taken first** |

### Free-plan limits, handled with one switch

The first `apply` failed:

```
Error: creating RDS DB Instance: FreeTierRestrictionError: ... backup retention period ...
```

Free-plan accounts reject some RDS options: retention above 1 day, gp3 storage, and storage autoscaling. Instead of scattering workarounds through the code, one variable picks a consistent set of values:

```hcl
variable "free_plan" {
  description = "true while the AWS account is on the Free plan; set false after upgrading."
  type        = bool
  default     = true
}

locals {
  db = var.free_plan ? {
    backup_retention_days = 1     # free plan maximum
    storage_type          = "gp2" # the type the free tier covers
    max_storage_gb        = 0     # 0 = storage autoscaling off
    } : {
    backup_retention_days = 7
    storage_type          = "gp3"
    max_storage_gb        = 50
  }
}
```

After upgrading the account, set `free_plan = false` and apply. The intent is written down in one place, rather than lost in a comment beside each setting.

```bash
terraform plan     # Step 3: expect 6 to add (subnet group, 2 security groups, 2 rules, database)
terraform apply    # RDS takes 5–10 minutes
aws rds describe-db-instances --db-instance-identifier ledgerline-staging \
  --query "DBInstances[0].[DBInstanceStatus,EngineVersion,PubliclyAccessible,StorageEncrypted,BackupRetentionPeriod]"
```

> **Stopping the database to save money** works (`aws rds stop-db-instance`), but **AWS starts a stopped instance again automatically after 7 days.** Stop it again, or plan around it. Storage is charged while it's stopped.

---

## 2B.8 Step 4: Secrets

**Goal:** generated passwords that exist only where they're used: not in git, the image, CI logs, or Terraform state.

### Where secrets live

| Store | Used for | Cost |
|---|---|---|
| **SSM Parameter Store**, `SecureString` | Secrets you create: the app database password, the site password, the Gemini key | Standard parameters are free |
| **Secrets Manager** | The database owner's password, created **and rotated** by RDS | $0.40 per secret per month |

ECS injects both kinds into containers as environment variables at start-up ([Step 6](#2b10-step-6-running-the-app)), so the app reads them like any other configuration. No AWS code is needed in the app.

### Keeping generated secrets out of Terraform state

The obvious approach leaks:

```hcl
# DON'T: the password is saved, in plain text, in Terraform state
resource "random_password" "x" { length = 32 }
resource "aws_ssm_parameter" "x" { value = random_password.x.result }
```

Terraform 1.11+ has two features that together fix this:

- **Ephemeral resources** produce a value during one run that is never written to state or plan files.
- **Write-only arguments** (names ending in `_wo`) are sent to the provider and never stored. Because Terraform can't see the stored value, you tell it when to send a new one, by changing a version number.

[`staging/secrets.tf`](../ledgerline/infra/staging/secrets.tf):

```hcl
ephemeral "random_password" "app_db" {
  length           = 32
  special          = true
  override_special = "-_=+.:!#%*"   # avoids quotes and backslashes that trip up shells and tools
}

resource "aws_ssm_parameter" "app_db_password" {
  name             = "/ledgerline/staging/app_db_password"
  type             = "SecureString"
  value_wo         = ephemeral.random_password.app_db.result   # sent to AWS, never stored
  value_wo_version = var.secret_versions.app_db_password       # bump to rotate
}
```

```hcl
variable "secret_versions" {
  type = map(number)
  default = {
    app_db_password = 1
    site_password   = 1
    gemini_api_key  = 1
  }
}
```

To rotate a secret, change its number to 2, then `apply` and deploy. Terraform generates and sends a new value; the next deploy's migration task sets it on the database, and the new containers read it.

### Secrets you provide yourself

The Gemini API key isn't generated. Terraform creates the parameter with a placeholder, and you set the real value from the CLI, so the key never touches code, state or CI:

```bash
aws ssm put-parameter --name /ledgerline/staging/gemini_api_key --type SecureString --overwrite --value "<your key>" \
  --profile ledgerline --region ap-southeast-2
```

Remember Chapter 1's warning: **free-tier Gemini keys may be used to improve Google's models. Use them only with synthetic data.**

### Reading a secret (when you must)

```bash
aws ssm get-parameter --name /ledgerline/staging/site_password --with-decryption \
  --query Parameter.Value --output text --profile ledgerline --region ap-southeast-2
```

### A matching change in the app: connection details as parts

Platforms inject secrets **one value per variable**, so the app gets `DB_HOST`, `DB_USER` and `DB_PASSWORD`, not a ready-made URL. [`app/config.py`](../ledgerline/app/config.py) gained `database_url_from_env()`:

- It uses `DATABASE_URL` if set (local development and CI), or builds the URL from the parts.
- `prefix="MIGRATION_"` reads `MIGRATION_DB_USER` and `MIGRATION_DB_PASSWORD` for the schema owner.
- It **percent-encodes** the user and password. Generated passwords contain `@ : / %`, which would otherwise corrupt the URL. A unit test with the password `p@ss:w/rd%#?&=` covers this.
- It defaults to `sslmode=require`: connections to RDS are always encrypted.

```bash
terraform plan     # Step 4: expect 3 to add (the ephemeral passwords don't count; they're never stored)
terraform apply
```

---

## 2B.9 Step 5: Letting CI publish images

**Goal:** after every push to `main` that passes CI, the tested image appears in ECR, without anyone pushing by hand.

Why CI and not a laptop? Partly principle: the image that was tested is the one that ships. Partly experience: pushing from Windows failed (see [troubleshooting](#2b12-troubleshooting-what-actually-went-wrong)).

CI needs AWS permissions. There are two ways to give them, and the difference matters.

### The right way: OIDC (no stored keys)

**OpenID Connect (OIDC)** lets AWS trust GitHub's identity directly:

1. When a workflow job runs, GitHub can issue it a short-lived **signed token** that says, in effect, *"I am a workflow in repository `owner/repo`, running for `refs/heads/main`."*
2. The job sends that token to AWS STS (`AssumeRoleWithWebIdentity`).
3. AWS checks the signature against GitHub's published keys, then checks the token's claims against the **role's trust policy**.
4. If they match, AWS returns credentials for that role that **expire within an hour**.

There is nothing stored in GitHub to leak, rotate or forget about. This is the recommended setup for any CI system that supports it (GitHub, GitLab, Bitbucket, CircleCI and others).

**Terraform for OIDC:**

```hcl
# Once per AWS account: "trust tokens signed by GitHub Actions".
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # No thumbprint needed: AWS verifies GitHub's certificate itself.
}

# Who may use the role: only this repository, only on main.
data "aws_iam_policy_document" "github_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:OWNER/REPO:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_ci" {
  name                 = "ledgerline-staging-github-ci"
  assume_role_policy   = data.aws_iam_policy_document.github_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "github_ci_ecr" {
  role   = aws_iam_role.github_ci.id
  policy = data.aws_iam_policy_document.github_ci_ecr.json   # the same push/deploy permissions as below
}
```

**The workflow side:**

```yaml
jobs:
  build:
    permissions:
      contents: read
      id-token: write          # allow this job to request an OIDC token
    steps:
      - uses: aws-actions/configure-aws-credentials@<sha> # v6
        with:
          role-to-assume: ${{ vars.AWS_ROLE_ARN }}      # the role's ARN; not a secret
          aws-region: ${{ vars.AWS_REGION }}
```

**The `sub` claim is where people get it wrong.** Its format depends on how the job runs:

| Job runs for | `sub` claim |
|---|---|
| A push to a branch | `repo:OWNER/REPO:ref:refs/heads/main` |
| A tag | `repo:OWNER/REPO:ref:refs/tags/v1.2.0` |
| A job with `environment: staging` | `repo:OWNER/REPO:environment:staging`, **instead of** the branch form |
| A pull request | `repo:OWNER/REPO:pull_request` |

So a deploy job that uses a GitHub *environment* (as ours does) needs the environment form in the trust policy. Copying the branch form from a blog post gives *"Not authorized to perform sts:AssumeRoleWithWebIdentity"*. The repository name must also match GitHub's exactly, including upper and lower case.

Production setups usually have **two roles**: a push-only role trusted for `ref:refs/heads/main`, and a deploy role trusted only for `environment:production`, where the GitHub environment requires a reviewer's approval.

### What actually happened: the account wouldn't allow it

```
Error: creating IAM OIDC Provider: ... AccessDenied: ... not authorized to perform:
iam:CreateOpenIDConnectProvider ... with an explicit deny in a service control policy
```

Accounts created with the new sign-up experience have an AWS-managed policy that denies `iam:*Provider*` on both the Free and Paid plans. OIDC providers are blocked until "activate advanced features", which is irreversible. **An explicit deny in an SCP beats any permission you have**, even "full access". No IAM change inside the account can get around it.

Our options were:

| Option | Security | Effort | Choice |
|---|---|---|---|
| Activate advanced features, then use OIDC | Best | Irreversible account change | Not now (a one-way door) |
| An IAM user with a narrowly scoped access key, stored in GitHub secrets | Acceptable, with care | Low | **Chosen** |
| Push and deploy from a laptop only | No stored keys, but manual, and failed on Windows | Medium | No |

> **The platform you're on can rule out the best practice.** Recognise when that's happened, take the safest alternative, write down why, and plan the way back. That's normal engineering, not failure.

### The fallback we used: a narrowly scoped IAM user

[`staging/github_ci.tf`](../ledgerline/infra/staging/github_ci.tf):

```hcl
resource "aws_iam_user" "github_ci" {
  name = "ledgerline-staging-github-ci"
  path = "/ci/"
}

data "aws_iam_policy_document" "github_ci_ecr" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]   # can't be limited to one repository
    resources = ["*"]
  }
  statement {
    sid = "PushToLedgerlineRepository"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
      "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
      "ecr:PutImage", "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]    # this one repository only
  }
}

resource "aws_iam_user_policy" "github_ci_ecr" {
  name   = "push-images"
  user   = aws_iam_user.github_ci.name
  policy = data.aws_iam_policy_document.github_ci_ecr.json
}
```

**The access key is created by hand, not by Terraform.** Terraform has an `aws_iam_access_key` resource, but it stores the secret key in state. Instead, create the key with the CLI and pass it straight into GitHub's encrypted secrets, without displaying it or saving it to a file. In PowerShell:

```powershell
$k = (aws iam create-access-key --user-name ledgerline-staging-github-ci --profile ledgerline | ConvertFrom-Json).AccessKey
gh secret set AWS_ACCESS_KEY_ID --body $k.AccessKeyId
gh secret set AWS_SECRET_ACCESS_KEY --body $k.SecretAccessKey
gh variable set AWS_REGION --body ap-southeast-2
Remove-Variable k
```

In bash:

```bash
read -r id secret < <(aws iam create-access-key --user-name ledgerline-staging-github-ci \
  --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text --profile ledgerline)
gh secret set AWS_ACCESS_KEY_ID --body "$id"
gh secret set AWS_SECRET_ACCESS_KEY --body "$secret"
unset id secret
```

**How we limit the risk of a stored key:**

- **Least privilege:** it can push to one repository and deploy one environment ([Step 6](#2b10-step-6-running-the-app)), and nothing else.
- **Where it's exposed:** GitHub never gives secrets to workflows triggered by pull requests from forks, and only the main-branch publish and deploy steps reference them.
- **Rotation:** create a second key (a user can have two), update the GitHub secrets, confirm CI works, then delete the old key:

  ```bash
  aws iam list-access-keys --user-name ledgerline-staging-github-ci
  aws iam delete-access-key --user-name ledgerline-staging-github-ci --access-key-id <old id>
  ```

- **Detection:** the secret scan in CI (gitleaks) and pre-commit hooks catch a key committed by mistake.
- **The way back:** when the account can use OIDC, replace the user with roles and delete the key.

### The workflow changes

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml), at the end of the `build` job. The image pushed is **the exact one built and smoke-tested earlier in the same job**:

```yaml
  build:
    needs: [lint-and-test, migrations, security]
    outputs:
      image: ${{ steps.push.outputs.image }}
    env:
      # Publish only on pushes to main, and only once AWS is configured.
      PUBLISH: ${{ github.event_name == 'push' && github.ref == 'refs/heads/main' && vars.AWS_REGION != '' }}
    steps:
      # ... build, smoke test, "no test tools in the image" ...
      - name: Log in to AWS
        if: env.PUBLISH == 'true'
        uses: aws-actions/configure-aws-credentials@<sha> # v6.3.0
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ${{ vars.AWS_REGION }}
      - name: Log in to Amazon ECR
        id: ecr
        if: env.PUBLISH == 'true'
        uses: aws-actions/amazon-ecr-login@<sha> # v2.1.7
      - name: Push the tested image, tagged with the commit SHA
        id: push
        if: env.PUBLISH == 'true'
        run: |
          image="${{ steps.ecr.outputs.registry }}/ledgerline:${{ github.sha }}"
          docker tag "ledgerline:${{ github.sha }}" "$image"
          docker push "$image"
          echo "image=$image" >> "$GITHUB_OUTPUT"
```

`PUBLISH` keeps the condition in one place. Before AWS is configured (no `AWS_REGION` variable), and on every pull request, these steps are skipped rather than failing.

**Variables vs secrets in GitHub:** *variables* (`vars.*`) are plain configuration that's visible in logs: the region, the cluster name, the URL. *Secrets* (`secrets.*`) are encrypted and masked in logs. Don't make everything a secret: masking non-secret values makes logs harder to read and hides mistakes.

### Checking it worked

```bash
gh run watch
aws ecr describe-images --repository-name ledgerline --query "imageDetails[].imageTags"
```

> **A green run isn't proof.** Our first run after this change passed in 66 seconds, and the registry stayed empty. The new steps had never made it into `ci.yml`, so there was nothing to fail. `gh run view <id> --verbose` lists every step. Check that the steps you added *ran*, not just that nothing failed.

---

## 2B.10 Step 6: Running the app

**Goal:** the app running on Fargate behind HTTPS, deployed by CI with migrations first, and a deploy that fails loudly if the new version doesn't come up.

### ECS in five words

| Term | Meaning |
|---|---|
| **Cluster** | A named group. On Fargate it's just a label; there are no servers in it to manage |
| **Task definition** | A versioned recipe: image, CPU and memory, environment, secrets, ports, health check, logging. Each change makes a new *revision* (`ledgerline-staging-app:7`) |
| **Task** | One running copy of a task definition: one or more containers |
| **Service** | "Keep N tasks of this definition running, behind this load balancer". It replaces tasks that die and handles rolling deploys |
| **Fargate** | AWS runs the task on capacity you never see. You pay per vCPU and GB per second |

The app runs as a **service**. The migration runs as a **one-off task** that exits when it's done.

### Two IAM roles, two jobs

This trips up nearly everyone the first time:

| Role | Used by | When | Ledgerline's permissions |
|---|---|---|---|
| **Execution role** | ECS itself | *Before* your code starts | Pull the image, write logs (`AmazonECSTaskExecutionRolePolicy`), read the specific SSM parameters and the RDS secret |
| **Task role** | Your code | *While* it runs | Read and write objects in the documents bucket |

Both trust `ecs-tasks.amazonaws.com`, limited to this account with `aws:SourceAccount`. If the app gets *"AccessDenied"* calling S3, look at the task role. If the task never starts because of secrets or the image, look at the execution role.

### The task definitions

From [`staging/app.tf`](../ledgerline/infra/staging/app.tf), the app container:

```hcl
container_definitions = jsonencode([{
  name         = "app"
  image        = local.template_image          # replaced by CI on every deploy (see below)
  portMappings = [{ containerPort = 8000 }]   # uvicorn's port inside the image

  environment = [                              # plain settings
    { name = "APP_ENV",         value = "staging" },
    { name = "DB_HOST",         value = aws_db_instance.main.address },
    { name = "DB_USER",         value = "ledgerline_app" },
    { name = "STORAGE_BACKEND", value = "s3" },
    { name = "S3_BUCKET",       value = module.documents.name },
    { name = "LLM_PROVIDER",    value = var.llm_provider },   # "fake" until a Gemini key is set
    { name = "DEV_LOGIN",       value = "true" },             # allowed only because SITE_PASSWORD is set
    # ...
  ]

  secrets = [                                  # injected at start-up; never visible in the task definition
    { name = "DB_PASSWORD",    valueFrom = aws_ssm_parameter.app_db_password.arn },
    { name = "SITE_PASSWORD",  valueFrom = aws_ssm_parameter.site_password.arn },
    { name = "GEMINI_API_KEY", valueFrom = aws_ssm_parameter.gemini_api_key.arn },
  ]

  healthCheck = {                              # liveness: restart if the process stops answering
    command = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=3)"]
  }
  stopTimeout = 30                             # uvicorn finishes in-flight requests within 25 s
  logConfiguration = { logDriver = "awslogs", options = { awslogs-group = "/ecs/ledgerline-staging", ... } }
}])
```

**Ports:** the container listens on **8000**. Locally, Compose may map it to a different host port (8007 on our development machine). That's only the host side, and it doesn't affect AWS.

The **migration task** uses the same image with a different command, and it's the only thing that gets the owner's password:

```hcl
command = ["sh", "-c", "alembic upgrade head && python -m app.db_roles && python -m app.seed"]
secrets = [
  # RDS stores the owner's credentials as JSON; ":password::" selects one key.
  { name = "MIGRATION_DB_PASSWORD", valueFrom = "${local.db_secret_arn}:password::" },
  { name = "DB_PASSWORD",           valueFrom = aws_ssm_parameter.app_db_password.arn },
]
```

It does three things, in order:

1. It migrates the schema as the owner.
2. It creates or refreshes the restricted `ledgerline_app` user with the current password. This is how rotation reaches the database.
3. It seeds demo users. Seeding refuses to run in production.

### The load balancer and its health check

```hcl
resource "aws_lb_target_group" "app" {
  port        = 8000
  target_type = "ip"                 # Fargate tasks register by IP address
  health_check {
    path = "/readyz"                 # readiness: only send traffic to tasks that can reach the database
  }
  deregistration_delay = 30          # let in-flight requests finish during a deploy
}
```

This is part 1's liveness and readiness split ([2.8](02-production-readiness.md#28-health-checks-shutdowns-and-interrupted-work)) in practice. The container health check (`/livez`) decides *restart*. The load balancer's check (`/readyz`) decides *traffic*. Both endpoints are excluded from the staging password gate, because AWS calls them without a password.

The load balancer uses two subnets, the minimum, because each one costs a public IPv4 address. Its security group accepts port 80 **only from CloudFront**, using the AWS-managed prefix list `com.amazonaws.global.cloudfront.origin-facing`. Nobody can bypass HTTPS by calling the load balancer directly.

### HTTPS without a domain: CloudFront

The staging site is protected by a shared password sent with HTTP Basic auth. Over plain HTTP, that password crosses the internet readable by anyone on the path. A certificate normally needs a domain you own. CloudFront gives every distribution a `*.cloudfront.net` address with a valid certificate, free:

```hcl
resource "aws_cloudfront_distribution" "app" {
  origin {
    domain_name = aws_lb.app.dns_name
    custom_origin_config { origin_protocol_policy = "http-only" }   # the ALB has no certificate
  }
  default_cache_behavior {
    viewer_protocol_policy   = "redirect-to-https"
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id            # Managed-CachingDisabled
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id # Managed-AllViewer
    # ...
  }
  viewer_certificate { cloudfront_default_certificate = true }
}
```

Two settings matter for an app (as opposed to a static site):

- **Caching disabled.** Logged-in pages must never be served to someone else from a cache.
- **Forward all viewer headers** (`Managed-AllViewer`). CloudFront strips the `Authorization` header by default, and without it, every request fails the password check with a 401.

The hop from CloudFront to the load balancer is still HTTP *inside AWS's network*. With a real domain (Chapter 4), you'd add an ACM certificate to the load balancer and make that hop HTTPS too.

The app also marks its login cookie `Secure` on deployed environments, so browsers never send it over plain HTTP:

```python
response.set_cookie(COOKIE, user_id, httponly=True, samesite="lax", secure=settings.is_deployed)
```

### The service, and who owns what

```hcl
resource "aws_ecs_service" "app" {
  desired_count = 0                          # the first deploy migrates, THEN starts the app

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true                  # reach ECR, S3, Gemini without a NAT gateway
  }

  deployment_minimum_healthy_percent = 100  # rolling: new task healthy before the old one stops
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true                          # new version keeps failing → go back automatically
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]   # CI owns these
  }
}
```

The most important design decision in this step is the **split of ownership**:

> **Terraform decides *how* the app runs** (size, settings, secrets, network). **CI decides *which image* runs.**

If Terraform owned the image tag, every deploy would need a Terraform apply, and every Terraform apply would roll the app back to whatever tag was in the code. So:

- Terraform's task definition uses a placeholder image (`ledgerline:template`). It's the *template* CI copies.
- CI copies the **latest** revision, swaps in the new image, and registers it as a new revision.
- `ignore_changes` stops Terraform from undoing CI's choice of revision and task count.
- A settings change made in Terraform (a new environment variable, say) creates a new template revision, and **takes effect on the next deploy**.

The service starts at `desired_count = 0` so the first deploy can migrate the empty database *before* any app container starts. Otherwise the app would start against missing tables and fail its health checks.

### The deploy script

[`scripts/deploy_ecs.sh`](../ledgerline/scripts/deploy_ecs.sh) does four things:

```
1. register   copy the latest app and migrate task definitions, swap the image, register both
2. migrate    run the migration task (in the service's subnets and security group), wait,
              print its logs, and STOP if it failed: the app is not touched
3. roll out   update the service to the new revision; wait until it's stable
4. verify     check the running revision is the new one, not a rollback
```

Step 4 exists because **"stable" doesn't mean "succeeded".** When the circuit breaker rolls back a failing version, the service becomes stable again, on the *old* version. A script that only waited for stability would report success for a deploy that never happened:

```bash
running="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[0].deployments[?status==`PRIMARY`].taskDefinition | [0]' --output text)"
if [[ "$running" != "$app_td" ]]; then
  echo "::error::The new version failed its health checks and was rolled back. Running: $running"
  exit 1
fi
```

The script was tested against a fake `aws` command for all three outcomes (success, failed migration, rollback) before it ever ran against AWS.

Migrations run **before** the new code, while the old version is still serving traffic. That's only safe because migrations follow expand-and-contract (part 1, [2.6](02-production-readiness.md#26-database-migrations-in-production)).

### The deploy job and its permissions

```yaml
  deploy-staging:
    needs: [build]
    if: needs.build.outputs.image != '' && vars.ECS_CLUSTER != ''   # only after an image was pushed and ECS exists
    environment:
      name: staging                    # shows deployments in GitHub's UI; can require approval in production
      url: ${{ vars.STAGING_URL }}
    concurrency:
      group: deploy-staging
      cancel-in-progress: false        # never cancel a deploy halfway, mid-migration
    steps:
      - uses: actions/checkout@<sha>
      - uses: aws-actions/configure-aws-credentials@<sha>
        with: { ... same secrets ... }
      - run: bash scripts/deploy_ecs.sh
        env:
          IMAGE: ${{ needs.build.outputs.image }}
          CLUSTER: ${{ vars.ECS_CLUSTER }}
```

The workflow-level concurrency changed too. A newer push used to **cancel** an older run. On `main` that could kill a deploy in the middle of a migration, so now only pull-request runs are cancelled, and runs on `main` queue:

```yaml
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

The CI user's deploy permissions (in `github_ci.tf`) are scoped as tightly as ECS allows:

| Action | Limited to |
|---|---|
| `ecs:DescribeTaskDefinition`, `ecs:RegisterTaskDefinition` | `*` (ECS doesn't support narrower) |
| `ecs:RunTask` | The migrate task family, in this cluster only |
| `ecs:DescribeTasks` | Tasks in this cluster |
| `ecs:DescribeServices`, `ecs:UpdateService` | This one service |
| `logs:GetLogEvents` | The migration log streams |
| `iam:PassRole` | The two task roles, and only when handed to ECS tasks |

`iam:PassRole` is the one people miss. Registering a task definition that names a role means *handing that role to ECS*. Without a limit, a CI identity that can pass any role can effectively give itself any permission.

### Doing it

```bash
cd ledgerline/infra/staging
terraform fmt && terraform validate
terraform plan            # Step 6: about 20 to add
terraform apply           # CloudFront takes 5–10 minutes
terraform output staging_url

# Tell CI where to deploy (variables, not secrets: nothing here is sensitive)
gh variable set ECS_CLUSTER --body ledgerline-staging
gh variable set STAGING_URL --body "$(terraform output -raw staging_url)"
```

PowerShell: `gh variable set STAGING_URL --body (terraform -chdir=ledgerline/infra/staging output -raw staging_url)`, run from the repository root.

Then commit and push to `main`. The run goes test → build → push → **migrate → roll out → verify**. Open the URL. The browser asks for a login: any username, and the `site_password` parameter as the password.

> **Worked example: Ledgerline's first deploy.** The first push after `apply` built and pushed a new image, ran the migration task (schema to `0002`, `ledgerline_app` created, demo users seeded), started one task, and passed the readiness check. Staging was live at a `*.cloudfront.net` address behind the password. That's six steps and seven Terraform files. No long-lived AWS keys exist on any laptop, and nothing in the environment was created by clicking in the console.

---

## 2B.11 Day-to-day operations

### Deploy

Push to `main`. That's all. To redeploy without a code change, push an empty commit: `git commit --allow-empty -m "Redeploy" && git push`. (Re-running an old workflow run would try to push an image tag that already exists, and immutable tags refuse.)

### Roll back

Point the service at the previous task definition revision. **No migration runs**, which is right: the old code must work with the current schema (expand-and-contract).

```bash
aws ecs list-task-definitions --family-prefix ledgerline-staging-app --sort DESC --max-items 5
aws ecs update-service --cluster ledgerline-staging --service ledgerline-staging-app \
  --task-definition ledgerline-staging-app:<previous revision>
aws ecs wait services-stable --cluster ledgerline-staging --services ledgerline-staging-app
```

Don't roll back by re-running an old deploy job. Its migration step would run old code's `alembic upgrade head` against a newer database, and Alembic would refuse, because it doesn't know the newer revision.

### Read logs

```bash
aws logs tail /ecs/ledgerline-staging --follow --profile ledgerline --region ap-southeast-2
aws logs tail /ecs/ledgerline-staging --since 1h --log-stream-name-prefix migrate
```

Logs are kept for 14 days (`retention_in_days`). Unlimited retention is a classic bill that grows without anyone noticing.

### Check what's running

```bash
aws ecs describe-services --cluster ledgerline-staging --services ledgerline-staging-app \
  --query "services[0].{running:runningCount,desired:desiredCount,taskDef:taskDefinition,rollout:deployments[0].rolloutState}"
```

### Pause and resume (to save credits)

```bash
# Pause
aws ecs update-service --cluster ledgerline-staging --service ledgerline-staging-app --desired-count 0
aws rds stop-db-instance --db-instance-identifier ledgerline-staging

# Resume: start the database, wait until it's available, then deploy
aws rds start-db-instance --db-instance-identifier ledgerline-staging
aws rds wait db-instance-available --db-instance-identifier ledgerline-staging
git commit --allow-empty -m "Resume staging" && git push      # deploy sets the count back to 1
```

While paused, you still pay for the load balancer, its public IPs and storage (see [Cost](#2b13-cost)). The database restarts itself after 7 days stopped.

### Switch from the fake LLM to Gemini

```bash
aws ssm put-parameter --name /ledgerline/staging/gemini_api_key --type SecureString --overwrite --value "<key>"
terraform apply -var llm_provider=gemini     # new template revision
git commit --allow-empty -m "Use Gemini in staging" && git push
```

To make it permanent, put `llm_provider = "gemini"` in a committed `staging.auto.tfvars` file.

### Rotate secrets

| Secret | How |
|---|---|
| App DB password, site password | Bump its number in `secret_versions`, `apply`, then deploy |
| DB owner password | RDS rotates it automatically (every 7 days by default). The migration task reads the current value each run |
| CI access key | Create a second key, update the GitHub secrets, check that CI works, delete the old key |
| Gemini key | `put-parameter --overwrite`, then deploy |

### Tear it all down

```bash
cd ledgerline/infra/staging
terraform destroy      # takes a final DB snapshot first; the documents bucket refuses if it has files
```

- **Delete the CI user's access key first** (`aws iam delete-access-key ...`). Terraform didn't create it, so it can't delete it, and the user can't be deleted while a key exists.
- The documents bucket refuses to be deleted while it holds files (`force_destroy = false`). That's deliberate for anything holding real data. Empty it on purpose, or set `force_destroy = true` for throwaway environments.
- The final snapshot `ledgerline-staging-final` remains and costs a little. Delete it when you're sure.
- **Destroy `bootstrap/` last, or never.** It holds the state, and `prevent_destroy` makes you remove the guard first, on purpose.

---

## 2B.12 Troubleshooting: what actually went wrong

Every row happened while building this staging environment.

| Symptom | Cause | Fix |
|---|---|---|
| Console instructions don't match the screen | The new sign-up experience (September 2026) has a different console and settings site | Use settings.aws.com and AWS's current docs; don't trust older tutorials, including AI answers |
| *"Unable to assume the service linked role"* on first ECS use | IAM eventual consistency: the role was created seconds earlier | Wait and retry once |
| `terraform init -backend-config=backend.hcl`: *"No positional arguments are expected"* | PowerShell splits arguments at `=` | `terraform init "-backend-config=backend.hcl"` |
| `docker login` to ECR: HTTP 400 when piping the token in PowerShell 5 | Most likely PowerShell 5's pipeline encoding: it adds characters Docker doesn't expect | Use `--password` (local only), or better, push from CI |
| `docker login`: *"error saving credentials … The stub received bad data"* | The ECR token is ~4,000 characters, too long for the Windows Credential Manager, even with `credsStore` removed | Push from CI (what we did), or install Amazon's `docker-credential-ecr-login` helper, or push from WSL |
| *FreeTierRestrictionError* creating RDS | Free-plan limits (retention > 1 day, gp3, storage autoscaling) | The `free_plan` switch ([2B.7](#2b7-step-3-network-and-database)) |
| *"explicit deny in a service control policy"* creating the OIDC provider | New-experience accounts deny `iam:*Provider*` | The IAM user fallback ([2B.9](#2b9-step-5-letting-ci-publish-images)) |
| Push rejected: *"File … terraform-provider-aws … is 775.85 MB"* | `.terraform/` committed; `.gitignore` lines had leading spaces | Fix `.gitignore`, `git reset --soft origin/main`, `git rm -r --cached` the folder, commit again |
| CI green but the registry empty | The publish steps were never added to `ci.yml` | `gh run view <id> --verbose`: check that the steps *ran* |
| *"warning: LF will be replaced by CRLF"* | Git on Windows converts line endings in your working copy | Harmless with `core.autocrlf=true`. Add `*.sh text eol=lf` to `.gitattributes` so shell scripts never get CRLF |
| `deploy_ecs.sh`: *"Permission denied"* in CI (potential) | The executable bit is lost when files come from Windows | The workflow runs `bash scripts/deploy_ecs.sh` |
| `terraform output`: *"No outputs found"* | Run from the wrong folder | `terraform -chdir=ledgerline/infra/staging output` |
| `rds start-db-instance`: *InvalidDBInstanceState* | It's already running | Nothing to do; check with `describe-db-instances` |
| Tests: 19 instead of 20 | Compose reused an old image | `docker compose build` before `docker compose run` |

And one found by tooling rather than by failure:

> **A CI check that could never fail.** The step "the image contains no test tools" was written as:
>
> ```bash
> ! docker run --rm "$image" python -c "import pytest"
> ! docker run --rm "$image" python -c "import reportlab"
> ```
>
> In bash, **a command starting with `!` never triggers "exit on error"**, so the first line couldn't fail the step. Only the last line's result counted. `shellcheck` (which `actionlint` runs on workflow scripts when it's installed) flagged it as SC2251. The fix is an explicit `if … then exit 1`. Lesson: **lint your CI scripts too**, and occasionally break a check on purpose to see that it goes red.

---

## 2B.13 Cost

Approximate on-demand prices for **Sydney (`ap-southeast-2`)**, October 2026, for this exact setup running 24/7. Prices vary by region and change, so check the AWS pricing pages before relying on them.

| Item | Approx. per month |
|---|---|
| Fargate task: 0.25 vCPU, 0.5 GB | ~$11 |
| Application Load Balancer (base + light traffic) | ~$19–21 |
| RDS `db.t4g.micro`, single-AZ, 20 GB gp2 | ~$21–23 |
| Public IPv4 addresses: 2 for the load balancer, 1 for the task ($0.005/hour each) | ~$11 |
| Secrets Manager (1 secret, created by RDS) | $0.40 |
| ECR, S3, CloudWatch Logs, SSM parameters, CloudFront at staging traffic | ~$1–2 |
| **Total** | **~$63–68 (about $2.10/day)** |

**Paused** (service at 0, database stopped), the load balancer, its IPs and storage still cost **about $1/day**. To get to zero, `terraform destroy` and rebuild when needed. With everything in code, that takes about 15 minutes.

On the new Free plan, all of this is drawn from the $100 credit. Running 24/7, staging would use it up in about 7 weeks. Paused when unused, it lasts the full 182 days.

What's deliberately **not** here, and would be in production:

| Item | Why it's skipped | Production cost |
|---|---|---|
| NAT gateway (private subnets) | Public IPs plus security groups instead | ~$40+/month per AZ in Sydney, before data |
| Multi-AZ database | Staging can be down for a few minutes | ~2× the database |
| Two or more app tasks | One is enough to test | Per task |
| Container Insights, WAF | Chapter 3 and Chapter 8 decide | Varies |

---

## 2B.14 By stage and product type

### By stage

| | MVP | Early production (this chapter) | Growth | Enterprise |
|---|---|---|---|---|
| Infrastructure | A PaaS or one VM; console is fine | **Terraform, remote state, one folder per environment** | Modules shared across services; CI runs `plan` on pull requests | Separate AWS accounts per environment; policy-as-code checks on plans |
| Network | Default | **Default VPC, tight security groups** | Own VPC, private subnets, NAT or VPC endpoints | Multiple accounts, central networking, private connectivity to customers |
| CI identity | A personal token | **OIDC (or a scoped key where OIDC is blocked)** | OIDC, separate push and deploy roles | OIDC, deploy approval via protected environments, audit trail |
| Deploys | Manual | **CI: migrate, roll, verify; circuit-breaker rollback** | Blue-green or canary | Progressive delivery with automated analysis |
| HTTPS | Platform-provided | **CloudFront default certificate** | Own domain, ACM certificates end to end | Plus WAF, DDoS protection, customer-specific domains |

### By product type

- **B2B SaaS:** customers' security reviews will ask about environment separation, encryption, secrets handling and who can deploy. This setup answers most of them. Separate production accounts come next (Chapter 13).
- **AI / LLM products:** the model API key is a secret like any other, with its own rotation. Staging should default to a fake or cheap model, so a load test doesn't become a large invoice. Outbound internet access from containers is required, which shapes the network design.
- **Agentic workflows:** agents call tools, and tools need credentials. Give the agent's runtime its own task role with only the permissions its tools need. Never reuse the app's role.
- **Internal tools:** often fine with a simpler path: one VM or a PaaS, behind the company's single sign-on. The principles still apply: infrastructure in code, secrets out of code, the tested image is the deployed image.
- **Consumer apps:** a CDN (CloudFront) in front matters for real performance, not just HTTPS. Expect to add caching rules for static assets and WAF rate limits early.

---

## 2B.15 Checklist

Staging on a cloud provider is ready when:

- [ ] All infrastructure is defined in code, in the same repository, and `plan` shows no changes against what's running
- [ ] Terraform state is remote, versioned, encrypted and locked; `.terraform/`, state files and backend settings are git-ignored
- [ ] Each environment has its own state
- [ ] No long-lived personal access keys exist; people log in with temporary credentials (`aws login` or SSO)
- [ ] CI authenticates with OIDC, or, where the platform blocks it, a narrowly scoped key with a written rotation procedure and a plan to move to OIDC
- [ ] Image tags are immutable, and the deployed image is the one CI tested
- [ ] The database isn't publicly accessible; security groups allow only the previous hop
- [ ] Generated secrets never appear in code, CI logs or Terraform state
- [ ] Migrations run as a separate task before each rollout, as the only user that can change the schema
- [ ] A deploy fails loudly if migrations fail or the new version is rolled back
- [ ] Liveness and readiness are wired to the right mechanisms (restart vs traffic)
- [ ] The site is HTTPS-only, and staging isn't usable without a password
- [ ] You've rolled back once, read the logs once, and paused and resumed once
- [ ] Log retention is set, and you know the monthly cost of running and of pausing

---

**Next:** [Chapter 3, Observability](03-observability.md): staging is running, but right now the only way to know whether it works is to open it and look. Next we make the system tell *us*.
