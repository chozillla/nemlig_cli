#!/usr/bin/env bash
# Deploy meal planner (Python server + Caddy HTTPS) to Azure Container Instances
# Usage: ./deploy-azure.sh
#
# Prerequisites:
#   - Azure CLI (brew install azure-cli) + logged in (az login)
#   - .env file with NEMLIG_USER, NEMLIG_PASS, AZURE_API_KEY, AZURE_ENDPOINT, AZURE_DEPLOYMENT
#
# Architecture:
#   Internet → :443 (Caddy auto-TLS) → localhost:8000 (server.py)
#
# Cost: ~$10-20/month (1 vCPU, 1.5GB RAM) + ~$0.50/month DNS

set -euo pipefail

# ── Configuration ────────────────────────────────────────────
RESOURCE_GROUP="rg-n8n"
LOCATION="northeurope"
STORAGE_ACCOUNT="mealplanstor75922"
ACR_NAME="mealplanacr75922"
CADDY_SHARE="caddydata"
CONTAINER_GROUP="mealplanner"
DNS_LABEL="nemlig-mealplanner"
CUSTOM_DOMAIN="${CUSTOM_DOMAIN:-ugemad.dk}"

# Load .env if present
if [ -f .env ]; then
    set -a; source .env; set +a
    echo "Loaded .env"
fi

# Validate required env vars
for var in NEMLIG_USER NEMLIG_PASS AZURE_API_KEY AZURE_ENDPOINT AZURE_DEPLOYMENT; do
    if [ -z "${!var:-}" ]; then
        echo "ERROR: $var not set. Check your .env file." >&2
        exit 1
    fi
done

API_TOKEN="${API_TOKEN:-$(openssl rand -hex 16)}"

# ── Derived ──────────────────────────────────────────────────
ACI_FQDN="$DNS_LABEL.$LOCATION.azurecontainer.io"
FQDN="${CUSTOM_DOMAIN:-$ACI_FQDN}"
TOTAL_STEPS=$( [ -n "$CUSTOM_DOMAIN" ] && echo 8 || echo 6 )

echo "=== Deploying Meal Planner to Azure ==="
echo "Domain: https://$FQDN"
echo ""

# 1. Resource group
echo "[1/$TOTAL_STEPS] Creating resource group..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

# 2. Container registry
echo "[2/$TOTAL_STEPS] Creating container registry..."
ACR_NAME=$(echo "$ACR_NAME" | tr -d '-' | head -c 24)
az acr create --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" --sku Basic --admin-enabled true --output none 2>/dev/null || true

# 3. Build Python server image
echo "[3/$TOTAL_STEPS] Building server image in ACR..."
TMPDIR_APP=$(mktemp -d)
cp server.py meal-planner.html index.html "$TMPDIR_APP/"
cat > "$TMPDIR_APP/Dockerfile" <<'DOCKERFILE'
FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY server.py meal-planner.html index.html ./
EXPOSE 8000
CMD ["python", "server.py"]
DOCKERFILE
az acr build --registry "$ACR_NAME" --image mealplanner:latest --platform linux/amd64 \
    -f "$TMPDIR_APP/Dockerfile" "$TMPDIR_APP" --output none
rm -rf "$TMPDIR_APP"

# 4. Build Caddy image
echo "[4/$TOTAL_STEPS] Building Caddy reverse-proxy image..."
TMPDIR_CADDY=$(mktemp -d)
cat > "$TMPDIR_CADDY/Caddyfile" <<EOF
$FQDN {
    reverse_proxy localhost:8000
}
EOF
if [ -n "$CUSTOM_DOMAIN" ] && [ "$FQDN" != "$ACI_FQDN" ]; then
    cat >> "$TMPDIR_CADDY/Caddyfile" <<EOF

$ACI_FQDN {
    redir https://$CUSTOM_DOMAIN{uri} permanent
}
EOF
fi
cat > "$TMPDIR_CADDY/Dockerfile" <<'DOCKERFILE'
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
DOCKERFILE
az acr build --registry "$ACR_NAME" --image caddy-proxy:latest --platform linux/amd64 \
    -f "$TMPDIR_CADDY/Dockerfile" "$TMPDIR_CADDY" --output none
rm -rf "$TMPDIR_CADDY"

# Get ACR credentials
ACR_SERVER=$(az acr show --name "$ACR_NAME" --query "loginServer" -o tsv)
ACR_USER=$(az acr credential show --name "$ACR_NAME" --query "username" -o tsv)
ACR_PASS=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)

# 5. Storage for Caddy TLS certs
echo "[5/$TOTAL_STEPS] Creating storage..."
STORAGE_ACCOUNT=$(echo "$STORAGE_ACCOUNT" | tr -d '-' | head -c 24)
az storage account create --name "$STORAGE_ACCOUNT" --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" --sku Standard_LRS --output none 2>/dev/null || true
STORAGE_KEY=$(az storage account keys list --resource-group "$RESOURCE_GROUP" \
    --account-name "$STORAGE_ACCOUNT" --query "[0].value" -o tsv)
az storage share create --name "$CADDY_SHARE" --account-name "$STORAGE_ACCOUNT" \
    --account-key "$STORAGE_KEY" --quota 1 --output none 2>/dev/null || true

# 6. Deploy container group
echo "[6/$TOTAL_STEPS] Deploying containers..."
DEPLOY_YAML=$(mktemp /tmp/deploy-XXXX.yaml)
cat > "$DEPLOY_YAML" <<YAML
apiVersion: 2021-09-01
location: $LOCATION
name: $CONTAINER_GROUP
type: Microsoft.ContainerInstance/containerGroups
properties:
  imageRegistryCredentials:
    - server: $ACR_SERVER
      username: $ACR_USER
      password: $ACR_PASS
  containers:
    - name: server
      properties:
        image: $ACR_SERVER/mealplanner:latest
        resources:
          requests:
            cpu: 0.75
            memoryInGb: 1.0
        ports:
          - port: 8000
            protocol: TCP
        environmentVariables:
          - name: NEMLIG_USER
            secureValue: "$NEMLIG_USER"
          - name: NEMLIG_PASS
            secureValue: "$NEMLIG_PASS"
          - name: API_TOKEN
            secureValue: "$API_TOKEN"
          - name: AZURE_API_KEY
            secureValue: "$AZURE_API_KEY"
          - name: AZURE_ENDPOINT
            value: "$AZURE_ENDPOINT"
          - name: AZURE_DEPLOYMENT
            value: "$AZURE_DEPLOYMENT"
    - name: caddy
      properties:
        image: $ACR_SERVER/caddy-proxy:latest
        resources:
          requests:
            cpu: 0.25
            memoryInGb: 0.5
        ports:
          - port: 443
            protocol: TCP
          - port: 80
            protocol: TCP
        volumeMounts:
          - name: caddydata
            mountPath: /data
  osType: Linux
  ipAddress:
    type: Public
    dnsNameLabel: $DNS_LABEL
    ports:
      - port: 443
        protocol: TCP
      - port: 80
        protocol: TCP
  volumes:
    - name: caddydata
      azureFile:
        shareName: $CADDY_SHARE
        storageAccountName: $STORAGE_ACCOUNT
        storageAccountKey: $STORAGE_KEY
YAML

az container create --resource-group "$RESOURCE_GROUP" --file "$DEPLOY_YAML" --output none
rm -f "$DEPLOY_YAML"

# Verify
STATE=$(az container show --resource-group "$RESOURCE_GROUP" --name "$CONTAINER_GROUP" \
    --query "instanceView.state" -o tsv)
IP=$(az container show --resource-group "$RESOURCE_GROUP" --name "$CONTAINER_GROUP" \
    --query "ipAddress.ip" -o tsv)

# 7-8. DNS setup for custom domain
if [ -n "$CUSTOM_DOMAIN" ]; then
    echo "[7/$TOTAL_STEPS] Creating DNS zone for $CUSTOM_DOMAIN..."
    az network dns zone create --resource-group "$RESOURCE_GROUP" --name "$CUSTOM_DOMAIN" \
        --output none 2>/dev/null || echo "  DNS zone exists"

    echo "[8/$TOTAL_STEPS] Setting DNS records..."
    # Remove stale A records before adding the new one
    OLD_IPS=$(az network dns record-set a show -g "$RESOURCE_GROUP" -z "$CUSTOM_DOMAIN" -n "@" \
        --query "ARecords[].ipv4Address" -o tsv 2>/dev/null || true)
    for OLD_IP in $OLD_IPS; do
        if [ "$OLD_IP" != "$IP" ]; then
            echo "  Removing stale A record: $OLD_IP"
            az network dns record-set a remove-record -g "$RESOURCE_GROUP" -z "$CUSTOM_DOMAIN" \
                -n "@" -a "$OLD_IP" --output none 2>/dev/null || true
        fi
    done
    az network dns record-set a add-record --resource-group "$RESOURCE_GROUP" \
        --zone-name "$CUSTOM_DOMAIN" --record-set-name "@" --ipv4-address "$IP" \
        --output none 2>/dev/null || true
    # Keep TTL low for faster propagation
    az network dns record-set a update -g "$RESOURCE_GROUP" -z "$CUSTOM_DOMAIN" \
        -n "@" --set ttl=60 --output none 2>/dev/null || true
    az network dns record-set cname set-record --resource-group "$RESOURCE_GROUP" \
        --zone-name "$CUSTOM_DOMAIN" --record-set-name "www" --cname "$CUSTOM_DOMAIN" \
        --output none 2>/dev/null || true

    NAMESERVERS=$(az network dns zone show --resource-group "$RESOURCE_GROUP" \
        --name "$CUSTOM_DOMAIN" --query "nameServers" -o tsv)

    # Update /etc/hosts if it has an entry for this domain
    if grep -q "$CUSTOM_DOMAIN" /etc/hosts 2>/dev/null; then
        echo "  Updating /etc/hosts..."
        sudo sed -i '' "s/.*$CUSTOM_DOMAIN/$IP $CUSTOM_DOMAIN/" /etc/hosts 2>/dev/null || \
            echo "  NOTE: Run 'sudo sed -i \"\" \"s/.*$CUSTOM_DOMAIN/$IP $CUSTOM_DOMAIN/\" /etc/hosts' to update /etc/hosts"
    fi
fi

echo ""
echo "=== Deployed! ==="
echo "Status: $STATE | IP: $IP"
echo ""
echo "URL:           https://$FQDN/meal-planner"
echo "Azure FQDN:    https://$ACI_FQDN"
echo "API Token:     $API_TOKEN"
echo ""
if [ -n "$CUSTOM_DOMAIN" ]; then
    # Check if domain currently resolves to the correct IP
    CURRENT_IP=$(dig +short "$CUSTOM_DOMAIN" 2>/dev/null | head -1)
    if [ -n "$CURRENT_IP" ] && [ "$CURRENT_IP" != "$IP" ]; then
        echo "╔══════════════════════════════════════════════════════════════╗"
        echo "║  WARNING: DNS mismatch!                                    ║"
        echo "║                                                            ║"
        echo "║  $CUSTOM_DOMAIN currently resolves to $CURRENT_IP"
        echo "║  but the new container IP is $IP"
        echo "║                                                            ║"
        echo "║  Update the A record at your domain registrar NOW,         ║"
        echo "║  or the site will be unreachable.                          ║"
        echo "╚══════════════════════════════════════════════════════════════╝"
        echo ""
    elif [ -z "$CURRENT_IP" ]; then
        echo "NOTE: $CUSTOM_DOMAIN does not resolve yet."
        echo "Set the A record at your registrar to: $IP"
        echo ""
    else
        echo "DNS OK: $CUSTOM_DOMAIN → $IP"
        echo ""
    fi
fi
echo "Commands:"
echo "  az container logs -g $RESOURCE_GROUP -n $CONTAINER_GROUP --container-name server"
echo "  az container logs -g $RESOURCE_GROUP -n $CONTAINER_GROUP --container-name caddy"
echo "  az container restart -g $RESOURCE_GROUP -n $CONTAINER_GROUP"
echo "  az group delete -g $RESOURCE_GROUP --yes  # delete everything"
echo ""
echo "API TOKEN: $API_TOKEN"
