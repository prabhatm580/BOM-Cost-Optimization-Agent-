#!/bin/sh
# CF deployment helper — sets API endpoint and SSO login
export CF_HOME=/tmp/cf-home
CF_ENDPOINT="[REDACTED]"
/tmp/cf8 api "https://${CF_ENDPOINT}"
