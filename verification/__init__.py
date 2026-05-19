"""Verification / testing utilities for the YPK Job Scraper.

This package collects standalone scripts used to verify or test individual
parts of the system in isolation. Each script is also runnable directly:

    python verification/validate_profiles.py   # validates profiles + alias files
    python verification/audit_slugs.py         # audits every (ats, slug) in config.yml
    python verification/try_fetch.py <ats> <slug>   # one-off fetch test

They are deliberately kept out of the runtime pipeline (`main.py`). Importing
them from runtime code is fine — for example:

    from verification.validate_profiles import validate_profiles, ValidationError
"""
