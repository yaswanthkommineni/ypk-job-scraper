# ypk-job-scraper

A continuously-running pipeline that fetches job postings directly from ATS (Applicant Tracking System) APIs, bypassing delayed aggregators like LinkedIn. The goal is to see new jobs as soon as they appear, giving you a competitive edge in the job market.

## How It Works

1. Reads a config file containing a list of companies (with ATS platform info) and candidate profiles.
2. Hits ATS APIs directly (respecting rate limits) to fetch the latest jobs.
3. Filters jobs through a multi-stage pipeline — first a basic pre-filter, then advanced scoring against your profiles.
4. Outputs high-quality matches to a file.

See [context.md](context.md) for full architectural details.

## Setup

### Step 1: Install dependencies

```bash
pip install "jobhive-py[scrapers]"
```

> **Note (macOS):** If you are using a MacBook, run this inside a virtual environment:
>
> ```bash
> python3 -m venv venv
> source venv/bin/activate
> pip install "jobhive-py[scrapers]"
> ```

### Step 2: Configure

Set up your config file with companies and profiles. *(Format TBD — see context.md)*

### Step 3: Run

```bash
python main.py
```
