"""
Use CF API and direct HTTP to test CAP service connectivity.
"""
import json, http.client, ssl

# Read CF token from file
token_file = '/tmp/cf_token.txt'
with open(token_file) as f:
    raw_token = [REDACTED]