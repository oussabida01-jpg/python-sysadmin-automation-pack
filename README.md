# ⚡ Python SysAdmin & Ops Automation Pack

A production-tested collection of robust Python automation scripts designed for systems administrators, DevOps practitioners, and operations engineers.

[![Gumroad](https://img.shields.io/badge/Get%20Full%20Toolkit%20(25%2B%20Scripts)-%2419-ff90e8?style=for-the-badge&logo=gumroad)](https://abidawave.gumroad.com/l/python-automation-toolkit)

---

## 📦 What's in this Free Showcase

This repository contains 3 standalone utilities demonstrating production architecture, robust CLI parsing, and strict error handling:

1. **`server_resource_monitor.py`**: Real-time cross-platform hardware sampling (CPU, RAM, Disk partitions) with configurable warning triggers.
2. **`bulk_file_renamer.py`**: Safe regex-based batch renamer featuring automated file collision checks and audit logging.
3. **`stale_file_purger.py`**: Automated directory maintenance utility that scans targets and purges log or temp files older than a specified retention threshold.

---

## 🚀 Looking for the Full Production Suite?

The complete **Python Automation Toolkit** includes **25+ fully-implemented, tested scripts** covering:

- 📄 **Document & PDF Workflows**: Multi-format PDF mergers, watermark stamping, dynamic invoice parsers, and batch DOCX converters.
- 📊 **Data & Excel Pipelines**: Multi-sheet consolidators, automated KPI reporters, deeply nested JSON flattener, and SQL-to-styled-Excel exporters.
- 🌐 **Web & API Integrations**: Price trackers with alert triggers, broken link crawlers, rate-limited REST API consumers, and webhook endpoints.
- 🗄️ **Database & Backup Operations**: Automated database dumpers (SQLite/MySQL/PostgreSQL), stale file retention purgers, and S3-compatible cloud uploaders.
- ⚙️ **System & Network Utilities**: Port connectivity checkers, duplicate MD5 file cleaners, and web server log analyzers.

👉 **[Download the Full 25+ Scripts Bundle on Gumroad ($19)](https://abidawave.gumroad.com/l/python-automation-toolkit)**

---

## 🛠️ Quickstart

```bash
# Clone the repository
git clone [https://github.com/oussabida01-jpg/python-sysadmin-automation-pack.git](https://github.com/oussabida01-jpg/python-sysadmin-automation-pack.git)
cd python-sysadmin-automation-pack

# Install dependencies
pip install psutil

# 1. Run server monitor
python server_resource_monitor.py --help

# 2. Run regex file renamer (dry-run mode)
python bulk_file_renamer.py --dry-run

# 3. Run stale file purger (example: purge logs older than 30 days)
python stale_file_purger.py --path /var/log/app --days 30
