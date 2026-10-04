# AWS project: Orders ETL into private RDS

**Goal:** a retailer sends an orders CSV. Extract it from S3, clean it with Python, load accepted orders into **Amazon RDS PostgreSQL in private subnets**, then query the database and publish a sales report to S3.

One small EC2 instance runs the job in a public subnet. Its database connections use private VPC networking. Only SSH from your own public IPv4 is allowed into EC2, and only EC2's security group is allowed into RDS. This keeps the first lab to one EC2 and one RDS database. Estimated learning time: 3–5 hours plus AWS provisioning waits.

## Settings used in this lab

These values match the hands-on session. Replace account-specific names and the endpoint if you recreate the project in another account.

| Setting | Value |
| --- | --- |
| AWS Region | `us-east-1` |
| S3 bucket | `s3-retail-store-bucket` |
| RDS instance identifier | `retail-etl-db` |
| RDS endpoint | `retail-etl-db.cc5y6uoy2skb.us-east-1.rds.amazonaws.com` |
| PostgreSQL port | `5432` |
| Database inside RDS | `retail` |
| Master database username | `lab_admin` |
| Restricted ETL database username | `sales_etl` |
| EC2 IAM role | `retail-etl-role` |
| Local SSH key filename | `kp-retail-etl.pem` |
| Project folder on EC2 | `/home/ec2-user/retail-project` |
| RDS certificate filename | `global-bundle.pem` |
| Sample batch date | `2026-10-03` |

The RDS instance name, database name, database usernames and EC2 IAM role are separate things. The IAM role grants S3 permissions; PostgreSQL usernames/passwords grant SQL access. Keep passwords and the PEM private key out of this README and out of source control.

## 0. Check your budget first

Use only instance types/storage covered by your **remaining** Free Tier entitlement or credits. Check Billing, credit expiry/balance and the RDS creation estimate before creating resources. RDS eligibility depends on account plan and creation date; “micro” does not itself mean free. Do not upgrade your account plan for this project.

Use a supported PostgreSQL engine version in standard support; avoid an old version that incurs Extended Support charges. Choose **Single-AZ**, a covered small instance class such as db.t3.micro/db.t4g.micro if your console offers it, and minimum allowed general-purpose storage (commonly 20 GiB). Disable storage autoscaling for the lab if possible and choose no paid monitoring extras or unnecessary log exports. Verify EC2, its EBS disk, RDS compute/storage/backups, S3 and the public IPv4 are all covered.

No NAT gateway, Elastic IP, RDS Proxy, read replica or load balancer is required. The S3 gateway endpoint has no additional endpoint charge. EC2 and RDS are still billable services outside covered offers. Alerts are notifications, not a hard cost cap. Delete lab resources after finishing; stopping RDS/EC2 does not remove storage charges, and RDS can restart automatically after its permitted stop period.

## 1. Build the VPC

Use one Region throughout; commands below use `us-east-1`. Choose two normal Availability Zones available in your account. Replace AZ names if needed.

| Resource | Example |
| --- | --- |
| VPC | retail-lab-vpc, 10.30.0.0/16, IPv4-only, default tenancy |
| Public subnet | retail-public-a, 10.30.1.0/24, AZ A |
| Private subnet A | retail-private-a, 10.30.11.0/24, AZ A |
| Private subnet B | retail-private-b, 10.30.12.0/24, AZ B |

Choose a different private CIDR if this range overlaps a network you will connect. In **VPC → Create VPC**, select **VPC only** and create the VPC. Enable its DNS resolution and DNS hostnames. Create the three subnets manually under **Subnets**. Leave subnet auto-assigned public IPv4 disabled; enable it only for EC2 at launch.

Create an internet gateway `retail-igw` and attach it to the VPC. Create two custom route tables and explicitly associate the subnets:

| Table | Association | Routes |
| --- | --- | --- |
| retail-public-rt | Public subnet only | 10.30.0.0/16 → local; 0.0.0.0/0 → internet gateway |
| retail-private-rt | Both private subnets | 10.30.0.0/16 → local only |

Keep the main route table with only its local route. Keep the default NACL, which permits traffic both ways. Inspect its rules: it works at subnet level, whereas security groups work at resource network interfaces. RDS needs two private subnets in different AZs in its subnet group even though you will launch just one Single-AZ DB instance.

**Checkpoint:** explain why the database needs no NAT gateway for EC2 to reach it. Its connection stays inside the VPC via the local route.

## 2. Create S3 access, security groups and an EC2 role

Create a globally unique S3 bucket in your lab Region. Keep all Block Public Access settings on, ACLs disabled, versioning off for this lab and default SSE-S3 encryption.

Under **VPC → Endpoints**, create an AWS-service endpoint for `com.amazonaws.REGION.s3`, choosing **Gateway**, not Interface. Select this VPC and associate **retail-public-rt**: the ETL instance will be in the public subnet. Keep the default endpoint policy initially. Check the public table now contains the automatically added S3 `pl-… → vpce-…` route. Your private RDS table needs no S3 route because RDS is not uploading these reports itself.

Create two security groups in this VPC. Create both before configuring references:

| Group | Direction | Type | Protocol | Port range | Source or destination | Rule description |
| --- | --- | --- | --- | --- | --- | --- |
| retail-etl-sg | Inbound | SSH | TCP | 22 | Source: My IP, your current public IPv4 `/32` | Allow SSH access from my public IP |
| retail-etl-sg | Outbound | PostgreSQL | TCP | 5432 | Destination: Custom, select the actual `retail-db-sg` ID | Allow ETL worker to connect to RDS PostgreSQL |
| retail-etl-sg | Outbound | HTTPS | TCP | 443 | Destination: Anywhere-IPv4, `0.0.0.0/0` | Allow HTTPS for package downloads, RDS CA download and S3 access |
| retail-db-sg | Inbound | PostgreSQL | TCP | 5432 | Source: Custom, select the actual `retail-etl-sg` ID | Allow PostgreSQL connections only from the ETL security group |

Group descriptions:

- `retail-etl-sg`: Controls SSH access and outbound database and HTTPS connections for the EC2 ETL worker.
- `retail-db-sg`: Allows PostgreSQL connections to private RDS from the EC2 ETL worker.

Remove the ETL group's default allow-all outbound rule. The DB group may retain its default outbound rule for this initial lab; inspect and distinguish it from the tightly restricted inbound rule. Do not allow database inbound from 0.0.0.0/0 or your laptop's IP. Do not attach additional permissive launch-wizard groups. Security groups are stateful, so response traffic needs no separate inbound client-port rule.

For IAM, replace every `REPLACE_WITH_BUCKET_NAME` in `ec2-policy.json` with `s3-retail-store-bucket` (or your own bucket name). Create an IAM role trusted by the **EC2 AWS service**, named `retail-etl-role`, and attach that policy. It allows raw-object reads, report writes and deletion of an old report completion marker. It does not need RDS administrator API permissions: SQL authentication is handled separately by PostgreSQL. The policy does not grant bucket listing or uploads to `raw/`; upload the source CSV through the S3 console using your own authorized account.

**Checkpoint:** route tables choose the network path, security groups permit the TCP traffic, IAM permits S3 actions, and the PostgreSQL login permits database actions.

## 3. Create the private RDS database

1. **RDS → Subnet groups → Create DB subnet group**: name `retail-db-subnets`, choose this VPC and **both private subnets**, each in its own AZ.
2. **RDS → Create database → Full configuration** (called **Standard create** in some console versions) → **PostgreSQL**. Choose regular PostgreSQL rather than Aurora. Select a current supported engine version, a Free Tier option if offered, or the smallest settings covered by your credits. If only large instance classes appear, check that you selected Single-AZ and **Burstable classes (includes t classes)** under Instance configuration; do not select a large instance just to proceed.
3. Select **Single DB instance / Single-AZ**. This subnet group does not itself create a second database or standby.
4. Set DB identifier `retail-etl-db`; choose master username `lab_admin` and a strong self-managed password. Keep it in your own password manager. Do not enable paid secret management for this beginner lab.
5. Scroll past Instance configuration and Storage to **Connectivity**. Choose **Don't connect to an EC2 compute resource** so you can use the manually configured groups. Choose `retail-lab-vpc`, DB subnet group `retail-db-subnets`, **Public access = No**, **Choose existing** security group **retail-db-sg only**, port **5432**, IPv4 networking. The port may appear under Additional connectivity configuration. If available choose AZ A to keep EC2 and RDS in the same AZ.
6. Under additional database configuration set the **initial database name to `retail`**. Keep encryption enabled. Use minimum storage covered by your account and no optional paid extras. Keep a short/default backup retention covered by your offer; do not add unnecessary snapshots.
7. Wait for Available. Go to **RDS → Databases → retail-etl-db → Connectivity & security → Endpoint**. Copy the DNS endpoint, without `:5432` or a URL scheme. Use that endpoint, not a hardcoded private IP: RDS addresses can change. Under **Configuration**, check **Master username** and **DB name**. If DB name is a dash because you omitted the initial name, the Python setup below can create `retail`.

**Checkpoint:** public accessibility is No, subnet group contains only private subnets, and inbound 5432 trusts only the ETL group.

## 4. Launch EC2 and prepare Python

Launch one small covered EC2 in **retail-public-a** using the standard Amazon Linux 2023 x86_64 AMI. Enable auto-assigned public IPv4, select **retail-etl-sg only**, attach **retail-etl-role**, and require IMDSv2. Use a small covered root volume with delete-on-termination; choose Standard CPU credit mode if available for your burstable type. Create/download a PEM key pair and protect it on your laptop. No user data is needed.

To attach the IAM role during launch, expand **Advanced details → IAM instance profile** and select `retail-etl-role`. For an existing EC2 instance, select it under **EC2 → Instances → Actions → Security → Modify IAM role**, select `retail-etl-role`, and choose **Update IAM role**. If the role does not appear, refresh and check it was created for the EC2 service with an instance profile.

On your laptop, open Git Bash or PowerShell/OpenSSH in the extracted project folder containing the four code files and `kp-retail-etl.pem`. On Linux/macOS run `chmod 400 kp-retail-etl.pem` first. Find your instance's **Public IPv4 address** under **EC2 → Instances → select instance → Details**. Replace `EC2_PUBLIC_IP` in both commands below; do not use the private `10.30.x.x` address from your laptop.

Run these commands on your **laptop**, not inside the EC2 shell:

```powershell
ssh -i kp-retail-etl.pem ec2-user@EC2_PUBLIC_IP "mkdir -p /home/ec2-user/retail-project"
scp -i kp-retail-etl.pem pipeline.py setup_db.py schema.sql requirements.txt ec2-user@EC2_PUBLIC_IP:/home/ec2-user/retail-project/
ssh -i kp-retail-etl.pem ec2-user@EC2_PUBLIC_IP
```

If prompted to trust the host, verify the address belongs to your instance before entering `yes`. The colon before `/home/...` identifies a remote destination. Omitting the destination makes `scp` treat the last filename as a destination and can produce `schema.sql: Not a directory`. Keep the PEM key on your laptop; it is not one of the files to upload.

On EC2 install the lab dependencies. These downloads intentionally use the public subnet's internet route:

```bash
cd /home/ec2-user/retail-project
sudo dnf install -y python3.11 python3.11-pip
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
curl --fail --location https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem --output global-bundle.pem
```

Use Python 3.11 explicitly; AL2023's default system `python3` is 3.9. Do not change the system Python symlink. The pinned PostgreSQL driver targets Python 3.10+. AWS CLI v2 is already supplied in the standard AL2023 AMI.

### Create or check the database using Python

You do not need `psql` for this runbook. Set non-secret connection settings in the **EC2 SSH session**. Replace the endpoint/master username if yours differ:

```bash
export PGHOST='retail-etl-db.cc5y6uoy2skb.us-east-1.rds.amazonaws.com'
export PGDATABASE='postgres'
export PGSSLROOTCERT='/home/ec2-user/retail-project/global-bundle.pem'
export PGUSER='lab_admin'
```

`postgres` is the existing default database used for this initial check. Paste the entire block below, including the final `PY`. It prompts for your RDS master password and creates `retail` only if it is absent:

```bash
python - <<'PY'
import getpass
import os
from pipeline import connect_db

conn = connect_db(
    user=os.environ['PGUSER'],
    password=getpass.getpass('RDS master password: ')
)
try:
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = 'retail'")
        if cur.fetchone():
            print('retail database already exists.')
        else:
            cur.execute('CREATE DATABASE retail')
            print('retail database created.')
finally:
    conn.close()
PY
```

Autocommit is needed because PostgreSQL does not permit `CREATE DATABASE` inside a transaction. This creates a logical database inside the existing RDS instance, not another RDS instance.

After the check succeeds, set up the restricted database user and table:

```bash
export PGDATABASE='retail'
python setup_db.py
export PGUSER='sales_etl'
```

The setup prompts for the master password and a new restricted `sales_etl` password. Password input is hidden and passwords are not written to source code or shell history. The ETL account can SELECT/INSERT/UPDATE the orders table; it cannot administer the database. Connections use **TLS with verify-full**, checking the RDS endpoint against the CA certificate.

Keep both passwords in your password manager. Enter the master password for setup and the new `sales_etl` password for the pipeline/database checks. You do not find `sales_etl` in the AWS IAM console: `setup_db.py` creates it inside PostgreSQL.

| Variable | Purpose | Where the value comes from |
| --- | --- | --- |
| `PGHOST` | RDS DNS hostname | RDS instance → Connectivity & security → Endpoint |
| `PGDATABASE` | Database to connect to | `retail`, created above or during RDS creation |
| `PGSSLROOTCERT` | Absolute certificate path on EC2 | The `global-bundle.pem` downloaded into the project folder |
| `PGUSER` | PostgreSQL username | RDS Configuration → Master username for setup; `sales_etl` afterward |

Our Python script explicitly reads `PGHOST`; setting only a custom variable named `RDSHOST` does not configure it. `export` settings and virtual-environment activation apply to the current shell. A new SSH login requires setting them again. The complete run command below includes all four variables to avoid the missing-host/certificate error observed during the lab.

## 5. Run Extract → Transform → Load

In **S3 → Buckets → s3-retail-store-bucket**, create/open the folder `raw`, then create/open `2026-10-03`. Choose **Upload → Add files**, select the supplied `orders.csv` from your laptop, and choose **Upload**. The object key must be exactly:

```text
raw/2026-10-03/orders.csv
```

It contains 13 synthetic rows, including mixed-case text, whitespace, a duplicate, invalid values, and cancelled/pending orders. The folder date identifies ingestion, not necessarily each order's date.

Run this complete block on **EC2**, including after a new SSH login:

```bash
cd /home/ec2-user/retail-project
source .venv/bin/activate
export PGHOST='retail-etl-db.cc5y6uoy2skb.us-east-1.rds.amazonaws.com'
export PGSSLROOTCERT='/home/ec2-user/retail-project/global-bundle.pem'
export PGDATABASE='retail'
export PGUSER='sales_etl'

python pipeline.py --bucket s3-retail-store-bucket --region us-east-1 --batch-date 2026-10-03
```

Enter the **sales_etl** password when prompted. The code is divided into readable stages:

| Stage | What happens |
| --- | --- |
| Extract | AWS CLI downloads the raw CSV from S3 using the EC2 role |
| Transform | Python validates dates, quantities/prices/statuses, normalizes text, and quarantines bad rows and duplicates |
| Load | Parameterized SQL inserts/updates accepted orders in private RDS, using order_id as the primary key |
| Report | PostgreSQL calculates completed-sales revenue and groups it by date/city |
| Publish | CSV report, rejection reasons, row metrics and a final completion marker go to S3 |

Each accepted load is one database transaction. A failed SQL load rolls back. `ON CONFLICT DO UPDATE` permits safe reruns and corrections to existing order IDs. Within one CSV the first valid occurrence wins. Across files, later accepted records update the same order ID. This is not CDC or timestamp-based conflict resolution.

The table's generated revenue column calculates `quantity × unit_price` for completed orders and zero for pending/cancelled orders. PostgreSQL NUMERIC and Python Decimal preserve exact monetary values.

View `reports/2026-10-03/` in the S3 console. For the original sample file and an initially empty `lab.orders` table, expect:

| Metric | Value |
| --- | ---: |
| Input rows | 13 |
| Accepted rows | 7 |
| Rejected rows | 6, including 1 duplicate |
| Database rows (initial load) | 7 |
| Completed revenue (initial load) | PKR 18,000 |

City totals: Karachi PKR 10,000; Islamabad PKR 5,000; Lahore PKR 3,000. Run the same command again: database rows remain 7 and revenue remains PKR 18,000. The report reflects the entire current orders table, not only the newest CSV.

S3 publication and RDS commits are separate operations. If report upload fails after the database commits, fix the issue and rerun; upserts prevent duplicate orders. The script removes a previous `_SUCCESS.json` before republishing and creates a new one only after outputs are uploaded. Run one job at a time; this lab does not implement concurrent-job locking or a distributed transaction.

### Understand the successful run

The user shared this completion line from the actual hands-on run on 4 October 2026:

```text
INFO Complete: {"accepted_rows": 7, "database_revenue_pkr": "18000.00", "database_rows": 7, "duplicate_rows": 1, "input_rows": 13, "rejected_rows": 6}
```

| Log or output file | Meaning |
| --- | --- |
| `delete: .../_SUCCESS.json` | Remove the previous completion marker before publishing; this does not delete database orders |
| `daily_sales.csv` | Completed-order counts and revenue grouped by order date and city |
| `rejected_orders.csv` | Rejected CSV rows with their rejection reasons |
| `metrics.json` | Input, accepted, rejected and duplicate counts, plus database totals |
| Upload `metrics.json` to `_SUCCESS.json` | Copy the metrics into a completion marker after all report uploads succeed |

`work/2026-10-03/output/` is a local folder on EC2. `s3://s3-retail-store-bucket/reports/2026-10-03/` is the S3 destination. Thirteen input rows equal seven accepted plus six rejected. The one duplicate is included within those six rejected rows. Pending/cancelled orders can be valid stored rows, but contribute zero revenue. `database_rows` and `database_revenue_pkr` describe the entire current orders table, not only this batch.

## 6. Inspect the data directly in RDS

Use the **EC2 SSH terminal**. This query connects to PostgreSQL and reads `lab.orders`; it does not read the source CSV or S3 reports. If you have opened a new SSH session, restore the complete connection settings first:

```bash
cd /home/ec2-user/retail-project
source .venv/bin/activate
export PGHOST='retail-etl-db.cc5y6uoy2skb.us-east-1.rds.amazonaws.com'
export PGSSLROOTCERT='/home/ec2-user/retail-project/global-bundle.pem'
export PGDATABASE='retail'
export PGUSER='sales_etl'
```

Paste the entire block and enter the `sales_etl` password when prompted:

```bash
python - <<'PY'
from pipeline import connect_db

conn = connect_db()
try:
    with conn.cursor() as cur:
        cur.execute('''
            SELECT order_id, order_date, city, category,
                   quantity, unit_price, status, revenue
            FROM lab.orders
            ORDER BY order_id;
        ''')
        print('\nORDERS STORED IN RDS')
        print(' | '.join(column[0] for column in cur.description))
        for row in cur.fetchall():
            print(' | '.join(str(value) for value in row))

        cur.execute('''
            SELECT COUNT(*), COALESCE(SUM(revenue), 0)
            FROM lab.orders;
        ''')
        count, revenue = cur.fetchone()
        print(f'\nTotal database rows: {count}')
        print(f'Total revenue: PKR {revenue}')
finally:
    conn.close()
PY
```

Expected totals for the unchanged sample are **7 rows** and **PKR 18000.00**. In `lab.orders`, `lab` is the schema and `orders` is the table. `retail` is the database containing that schema. The primary key `order_id` prevents duplicate stored orders; the generated `revenue` column applies the completed-order business rule.

To query city totals, you can replace the first SELECT in the block with:

```sql
SELECT city, COUNT(*) AS completed_orders, SUM(revenue) AS revenue_pkr
FROM lab.orders
WHERE status = 'completed'
GROUP BY city
ORDER BY city;
```

This SQL belongs inside `cur.execute(...)`, not directly at the Bash prompt. The totals should be Islamabad **5000.00**, Karachi **10000.00**, and Lahore **3000.00** for the sample. A second pipeline run should leave the stored row count and revenue unchanged. Confirm this with the direct RDS check rather than relying only on S3 output.

## 7. Five short learning experiments

1. **Private database:** confirm a direct laptop-to-RDS connection cannot reach it. EC2-to-RDS succeeds using its private VPC path.
2. **Security group:** remove only the DB group's inbound 5432 rule. Start a new pipeline connection: it should time out. Restore it. Security-group edits may leave an existing connection alive, so test a new connection.
3. **Authentication:** enter a wrong ETL database password. Expect an authentication error instead of a connection timeout. This means the network path works.
4. **S3 route versus internet route:** after dependencies/CA are installed, remove only `0.0.0.0/0 → IGW` from the public table. SSH from your laptop will stop working. Restore that route through the VPC console to regain access. Before trying this, optionally predict which paths would still work from an existing local job: VPC-local RDS and the S3 endpoint route. Do not depend on a live SSH session surviving.
5. **Safe rerun:** upload the same CSV again and rerun; totals do not double. Then change order 1007 from pending to completed and rerun: revenue becomes **PKR 21,500** and database rows remain 7. Restore the original CSV for a final clean baseline.

The public worker still has an internet route, so removing only the S3 endpoint association may make S3 fall back to the internet rather than fail. Endpoint presence must be checked in the actual route table. An optional stronger experiment is to restrict worker outbound HTTPS to the regional S3 prefix list after installing dependencies, then disassociate the endpoint and compare failures. Restore its HTTPS access afterward if you need package updates.

These exercises cover core VPC/ETL behavior. NAT, peering, VPN, Flow Logs and a private ETL worker can be later extensions; they are not required for this short project. Inspect the default NACL and explain why its stateless return rules differ from security groups before experimenting with custom NACLs.

## 8. Troubleshoot and clean up

| Error | First checks |
| --- | --- |
| EC2 SSH timeout | Public IP, IGW route, route/subnet association, current My IP /32 |
| `schema.sql: Not a directory` from `scp` | Use all four filenames followed by `ec2-user@EC2_PUBLIC_IP:/home/ec2-user/retail-project/`; use the actual key filename `kp-retail-etl.pem` |
| `Set PGHOST to the RDS DNS endpoint and PGSSLROOTCERT to the CA file` | Run all four exports again after a new SSH login; `PGHOST` must be set and `PGSSLROOTCERT` must point to an existing file. Check `ls -l /home/ec2-user/retail-project/global-bundle.pem`; download it again using the command in section 4 if missing |
| RDS connect timeout | Available status, DNS endpoint, same VPC, RDS group inbound 5432 from ETL group and ETL outbound 5432 to DB group |
| Database does not exist | Run the Python database check/create in section 4 using `PGDATABASE=postgres` and the master user, then change `PGDATABASE` back to `retail` |
| `psql: command not found` | Use the Python setup/query blocks in this README; a separate psql installation is not needed |
| `No module named psycopg2` | Activate `.venv`, then run `python -m pip install -r requirements.txt` |
| Authentication failed | PostgreSQL username/password, not IAM access keys |
| Certificate verification failed | RDS CA file downloaded; PGHOST is the actual DNS endpoint; verify-full settings |
| S3 AccessDenied | Exact bucket/key ARN in EC2 policy, attached role/profile, bucket or endpoint policy |

Keep screenshots of your route tables, private RDS settings, group references, successful load/report and unchanged rerun totals.

For cleanup, preserve any synthetic results you want, then **delete RDS** and wait for deletion. For this disposable lab, select no final snapshot and no retained automated backups if you do not need them; only do this for this lab's synthetic data. Terminate EC2 and verify its EBS volume was removed. Empty/delete the S3 bucket, delete the S3 endpoint and DB subnet group, remove cross-references and delete lab security groups, then delete lab subnets/custom route tables, detach/delete the IGW and delete the VPC. Remove the lab IAM role/policy/instance profile and EC2 key-pair entry. Check for leftover snapshots, retained backups, disks or accidentally created NAT gateways/Elastic IPs. Do not delete unrelated resources. Recheck billing after its reporting delay.

## Sources and validation

Checked 4 October 2026, Asia/Karachi:

- Course chapter: https://elearning.morpheralabs.com/docs/part-08-aws-for-data-engineers/aws-services/networking-services/chapter-05-amazon-vpc
- RDS VPC and subnet groups: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html
- RDS offers: https://aws.amazon.com/rds/free/
- RDS PostgreSQL TLS: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html
- S3 gateway endpoints: https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-s3.html
- Psycopg installation: https://www.psycopg.org/docs/install.html
- AL2023 Python: https://docs.aws.amazon.com/linux/al2023/ug/python.html
- Attach an EC2 IAM role: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/attach-iam-role.html
- Find RDS connection details: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_CommonTasks.Connect.EndpointAndPort.html
- PostgreSQL CREATE DATABASE: https://www.postgresql.org/docs/current/sql-createdatabase.html
- PostgreSQL environment variables: https://www.postgresql.org/docs/current/libpq-envars.html

Local checks validate parsing/cleaning, sample totals, corrected-order behavior, Python syntax and IAM JSON. The user ran the pipeline in their own AWS account and shared successful S3 publication logs and database totals matching the sample: 7 rows and PKR 18000.00. The assistant has not independently accessed the AWS account or provisioned a live database. Direct SQL inspection, gateway-endpoint routing, security-group failure experiments and unchanged rerun totals must be confirmed separately using the steps above; the successful log alone does not prove the S3 network path or that a rerun occurred.
