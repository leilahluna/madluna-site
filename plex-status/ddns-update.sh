#!/bin/bash
# ===========================================
# Cloudflare DDNS Updater for plex.madluna.ca
# ===========================================
# This script checks your public IP every time it runs.
# If it changed, it updates the DNS record on Cloudflare.
#
# WHAT IT DOES:
# 1. Gets your current public IP
# 2. Compares it to the last known IP (stored in a local file)
# 3. If different, calls Cloudflare API to update plex.madluna.ca
#
# WHAT YOU NEED TO FILL IN:
# - API_TOKEN: Your Cloudflare API token (from the "Edit zone DNS" template)
# - ZONE_ID:   Your Cloudflare zone ID (found on the madluna.ca overview page, right sidebar)
# - RECORD_ID: We'll get this automatically on first run
# ===========================================

API_TOKEN="cfut_k0nH41TNeIyTLpJS4seEfDT18S03i61yj10WLW8T665a71d7"
ZONE_ID="3eb6b110df6ad189c11fd2f13089177d"
RECORD_NAME="plex.madluna.ca"
IP_FILE="/tmp/ddns-current-ip.txt"
LOG_FILE="/var/log/ddns-update.log"

# --- Get current public IP ---
CURRENT_IP=$(curl -s ifconfig.me)

if [ -z "$CURRENT_IP" ]; then
    echo "$(date) - ERROR: Could not get public IP" >> "$LOG_FILE"
    exit 1
fi

# --- Check if IP changed ---
OLD_IP=""
if [ -f "$IP_FILE" ]; then
    OLD_IP=$(cat "$IP_FILE")
fi

if [ "$CURRENT_IP" = "$OLD_IP" ]; then
    # IP hasn't changed, nothing to do
    exit 0
fi

# --- Get the DNS record ID (finds it automatically) ---
RECORD_ID=$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records?type=A&name=${RECORD_NAME}" \
    -H "Authorization: Bearer ${API_TOKEN}" \
    -H "Content-Type: application/json" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)

if [ -z "$RECORD_ID" ]; then
    echo "$(date) - ERROR: Could not find DNS record for ${RECORD_NAME}" >> "$LOG_FILE"
    exit 1
fi

# --- Update the DNS record ---
RESPONSE=$(curl -s -X PUT "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records/${RECORD_ID}" \
    -H "Authorization: Bearer ${API_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{
        \"type\": \"A\",
        \"name\": \"${RECORD_NAME}\",
        \"content\": \"${CURRENT_IP}\",
        \"ttl\": 300,
        \"proxied\": false
    }")

# --- Check if it worked ---
SUCCESS=$(echo "$RESPONSE" | grep -o '"success":true')

if [ -n "$SUCCESS" ]; then
    echo "$CURRENT_IP" > "$IP_FILE"
    echo "$(date) - Updated ${RECORD_NAME}: ${OLD_IP} -> ${CURRENT_IP}" >> "$LOG_FILE"
else
    echo "$(date) - ERROR: Failed to update DNS. Response: ${RESPONSE}" >> "$LOG_FILE"
fi
