"""Offline, CI-safe release-gate checks for the Blessing AI repository.

This package contains the repo-deterministic tier of the unified release gate.
It runs locally and in CI without network access to exchanges or cloud services,
and produces structured evidence dicts that a later combiner script will merge
with results from the contract and soak tiers.
"""
