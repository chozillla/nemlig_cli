#!/usr/bin/env bash
# Deploy n8n to Azure Container Instances with HTTPS (Caddy sidecar + Let's Encrypt)
# Usage: ./deploy-n8n-azure.sh
#
# Prerequisites:
#   - Azure CLI installed (brew install azure-cli)
#   - Logged in (az login)
#   - Custom domain purchased from a registrar (e.g. simply.com for .dk)
#
# Architecture:
#   Internet → :443 (Caddy + auto TLS) → localhost:5678 (n8n)
#   Both containers share a network namespace in ACI (like a k8s pod).
#   Custom domain DNS managed via Azure DNS zone.
#
# Cost: ~$15-30/month (1 vCPU, 1.5GB RAM + storage) + ~$0.50/month for DNS zone

set -euo pipefail

# ---- Configuration (edit these) ----
RESOURCE_GROUP="rg-n8n"
LOCATION="northeurope"            # Closest Azure region to Denmark
STORAGE_ACCOUNT="n8nstorage$$"    # Must be globally unique, lowercase, no dashes
ACR_NAME="n8nacr$$"               # Azure Container Registry name (globally unique)
N8N_SHARE="n8ndata"
CADDY_SHARE="caddydata"
CONTAINER_GROUP="n8n"
DNS_LABEL="nemlig-n8n"            # Your instance will be at: nemlig-n8n.northeurope.azurecontainer.io
CUSTOM_DOMAIN="${CUSTOM_DOMAIN:-ugemad.dk}"  # Custom domain — set to "" to use Azure FQDN only
N8N_ENCRYPTION_KEY=""             # Set this! Used to encrypt credentials in n8n
MEAL_PLANNER_TOKEN="${MEAL_PLANNER_TOKEN:-$(openssl rand -hex 16)}"  # Set via env or auto-generate

# ---- Derived ----
ACI_FQDN="$DNS_LABEL.$LOCATION.azurecontainer.io"
# Use custom domain for Caddy if set, otherwise fall back to Azure FQDN
if [ -n "$CUSTOM_DOMAIN" ]; then
    FQDN="$CUSTOM_DOMAIN"
else
    FQDN="$ACI_FQDN"
fi

# Generate encryption key if not set
if [ -z "$N8N_ENCRYPTION_KEY" ]; then
    N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)
    echo "Generated N8N_ENCRYPTION_KEY: $N8N_ENCRYPTION_KEY"
    echo "IMPORTANT: Save this key! You need it to decrypt your n8n credentials."
    echo ""
fi

# Generate meal planner access token if not set
if [ -z "$MEAL_PLANNER_TOKEN" ]; then
    MEAL_PLANNER_TOKEN=$(openssl rand -hex 16)
fi

echo "=== Deploying n8n + Caddy (HTTPS) to Azure Container Instances ==="
echo "Resource Group: $RESOURCE_GROUP"
echo "Location:       $LOCATION"
if [ -n "$CUSTOM_DOMAIN" ]; then
    echo "Custom Domain:  https://$CUSTOM_DOMAIN"
    echo "Azure FQDN:     https://$ACI_FQDN (redirects to custom domain)"
    TOTAL_STEPS=10
else
    echo "URL:            https://$ACI_FQDN"
    TOTAL_STEPS=8
fi
echo ""

# 1. Create resource group
echo "[1/$TOTAL_STEPS] Creating resource group..."
az group create \
    --name "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --output none

# 2. Create Azure Container Registry
echo "[2/$TOTAL_STEPS] Creating container registry..."
ACR_NAME=$(echo "${ACR_NAME}" | tr -d '-' | head -c 24)
az acr create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$ACR_NAME" \
    --sku Basic \
    --admin-enabled true \
    --output none

# 3. Build n8n image in ACR
echo "[3/$TOTAL_STEPS] Building n8n amd64 image in ACR..."
TMPDIR_BUILD=$(mktemp -d)
echo 'FROM n8nio/n8n:latest' > "$TMPDIR_BUILD/Dockerfile"
az acr build \
    --registry "$ACR_NAME" \
    --image n8n:latest \
    --platform linux/amd64 \
    -f "$TMPDIR_BUILD/Dockerfile" \
    "$TMPDIR_BUILD" \
    --output none
rm -rf "$TMPDIR_BUILD"

# 4. Build Caddy image with embedded Caddyfile + meal planner HTML
echo "[4/$TOTAL_STEPS] Building Caddy reverse-proxy image in ACR..."
TMPDIR_CADDY=$(mktemp -d)
if [ -n "$CUSTOM_DOMAIN" ]; then
    # Custom domain config: serve on custom domain, redirect Azure FQDN → custom domain
    cat > "$TMPDIR_CADDY/Caddyfile" <<EOF
$CUSTOM_DOMAIN {
    handle /meal-planner/$MEAL_PLANNER_TOKEN* {
        root * /srv
        rewrite * /meal-planner.html
        file_server
    }
    handle /meal-planner* {
        respond "Not found" 404
    }
    handle {
        reverse_proxy localhost:5678
    }
}

$ACI_FQDN {
    redir https://$CUSTOM_DOMAIN{uri} permanent
}
EOF
else
    cat > "$TMPDIR_CADDY/Caddyfile" <<EOF
$ACI_FQDN {
    handle /meal-planner/$MEAL_PLANNER_TOKEN* {
        root * /srv
        rewrite * /meal-planner.html
        file_server
    }
    handle /meal-planner* {
        respond "Not found" 404
    }
    handle {
        reverse_proxy localhost:5678
    }
}
EOF
fi
cp meal-planner.html "$TMPDIR_CADDY/meal-planner.html"
cat > "$TMPDIR_CADDY/Dockerfile" <<'EOF'
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY meal-planner.html /srv/meal-planner.html
EOF
az acr build \
    --registry "$ACR_NAME" \
    --image caddy-proxy:latest \
    --platform linux/amd64 \
    -f "$TMPDIR_CADDY/Dockerfile" \
    "$TMPDIR_CADDY" \
    --output none
rm -rf "$TMPDIR_CADDY"

# Get ACR credentials
ACR_SERVER=$(az acr show --name "$ACR_NAME" --query "loginServer" -o tsv)
ACR_USER=$(az acr credential show --name "$ACR_NAME" --query "username" -o tsv)
ACR_PASS=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)

# 5. Create storage account + file shares
echo "[5/$TOTAL_STEPS] Creating storage..."
STORAGE_ACCOUNT=$(echo "${STORAGE_ACCOUNT}" | tr -d '-' | head -c 24)
az storage account create \
    --name "$STORAGE_ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --output none

STORAGE_KEY=$(az storage account keys list \
    --resource-group "$RESOURCE_GROUP" \
    --account-name "$STORAGE_ACCOUNT" \
    --query "[0].value" -o tsv)

echo "[6/$TOTAL_STEPS] Creating file shares..."
az storage share create --name "$N8N_SHARE" --account-name "$STORAGE_ACCOUNT" --account-key "$STORAGE_KEY" --quota 1 --output none
az storage share create --name "$CADDY_SHARE" --account-name "$STORAGE_ACCOUNT" --account-key "$STORAGE_KEY" --quota 1 --output none

# 7. Generate YAML deployment (multi-container group: n8n + caddy)
echo "[7/$TOTAL_STEPS] Deploying container group (n8n + Caddy)..."
DEPLOY_YAML=$(mktemp /tmp/n8n-deploy-XXXX.yaml)
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
    - name: n8n
      properties:
        image: $ACR_SERVER/n8n:latest
        resources:
          requests:
            cpu: 0.75
            memoryInGb: 1.0
        ports:
          - port: 5678
            protocol: TCP
        environmentVariables:
          - name: N8N_HOST
            value: "$ACI_FQDN"
          - name: N8N_PORT
            value: "5678"
          - name: N8N_PROTOCOL
            value: "https"
          - name: N8N_SECURE_COOKIE
            value: "true"
          - name: WEBHOOK_URL
            value: "https://$FQDN/"
          - name: N8N_EDITOR_BASE_URL
            value: "https://$FQDN/"
          - name: GENERIC_TIMEZONE
            value: "Europe/Copenhagen"
          - name: TZ
            value: "Europe/Copenhagen"
          - name: N8N_ENCRYPTION_KEY
            secureValue: "$N8N_ENCRYPTION_KEY"
        volumeMounts:
          - name: n8ndata
            mountPath: /home/node/.n8n
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
    - name: n8ndata
      azureFile:
        shareName: $N8N_SHARE
        storageAccountName: $STORAGE_ACCOUNT
        storageAccountKey: $STORAGE_KEY
    - name: caddydata
      azureFile:
        shareName: $CADDY_SHARE
        storageAccountName: $STORAGE_ACCOUNT
        storageAccountKey: $STORAGE_KEY
YAML

az container create \
    --resource-group "$RESOURCE_GROUP" \
    --file "$DEPLOY_YAML" \
    --output none
rm -f "$DEPLOY_YAML"

# 8. Verify container deployment
echo "[8/$TOTAL_STEPS] Verifying deployment..."
STATE=$(az container show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_GROUP" \
    --query "instanceView.state" -o tsv)

IP=$(az container show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$CONTAINER_GROUP" \
    --query "ipAddress.ip" -o tsv)

echo "Container status: $STATE (IP: $IP)"

# 9-10. Set up Azure DNS zone for custom domain
if [ -n "$CUSTOM_DOMAIN" ]; then
    echo "[9/$TOTAL_STEPS] Creating Azure DNS zone for $CUSTOM_DOMAIN..."
    az network dns zone create \
        --resource-group "$RESOURCE_GROUP" \
        --name "$CUSTOM_DOMAIN" \
        --output none 2>/dev/null || echo "  DNS zone already exists, skipping..."

    echo "[10/$TOTAL_STEPS] Creating DNS records..."
    # A record: root domain → container IP
    az network dns record-set a add-record \
        --resource-group "$RESOURCE_GROUP" \
        --zone-name "$CUSTOM_DOMAIN" \
        --record-set-name "@" \
        --ipv4-address "$IP" \
        --output none 2>/dev/null || \
    az network dns record-set a update \
        --resource-group "$RESOURCE_GROUP" \
        --zone-name "$CUSTOM_DOMAIN" \
        --name "@" \
        --output none 2>/dev/null || true

    # www CNAME → root domain
    az network dns record-set cname set-record \
        --resource-group "$RESOURCE_GROUP" \
        --zone-name "$CUSTOM_DOMAIN" \
        --record-set-name "www" \
        --cname "$CUSTOM_DOMAIN" \
        --output none 2>/dev/null || true

    # Get the nameservers to configure at registrar
    NAMESERVERS=$(az network dns zone show \
        --resource-group "$RESOURCE_GROUP" \
        --name "$CUSTOM_DOMAIN" \
        --query "nameServers" -o tsv)
fi

echo ""
echo "=== n8n Deployed with HTTPS! ==="
echo "Status: $STATE"
echo "IP:     $IP"
echo ""
if [ -n "$CUSTOM_DOMAIN" ]; then
    echo "Custom Domain:  https://$CUSTOM_DOMAIN"
    echo "Azure FQDN:     https://$ACI_FQDN (redirects to custom domain)"
    echo ""
    echo "Meal Planner:   https://$CUSTOM_DOMAIN/meal-planner/$MEAL_PLANNER_TOKEN"
    echo ""
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
    elif [ -z "$CURRENT_IP" ]; then
        echo "NOTE: $CUSTOM_DOMAIN does not resolve yet."
        echo "Set the A record at your registrar to: $IP"
    else
        echo "DNS OK: $CUSTOM_DOMAIN → $IP"
    fi
    echo ""
    echo "  Until DNS propagates, use: https://$ACI_FQDN"
else
    echo "URL:    https://$ACI_FQDN"
    echo ""
    echo "Meal Planner: https://$ACI_FQDN/meal-planner/$MEAL_PLANNER_TOKEN"
fi
echo ""
echo "Caddy will auto-provision a Let's Encrypt TLS certificate on first request."
echo "This may take 30-60 seconds on the very first visit."
echo ""
echo "Next steps:"
echo "  1. Open https://$FQDN and create your admin account"
echo "  2. Import workflows: Workflows > Import > n8n_meal_plan_workflow.json"
echo "  3. Set n8n Variables (Settings > Variables):"
echo "     NEMLIG_USER, NEMLIG_PASS, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT"
echo "     API_TOKEN = $MEAL_PLANNER_TOKEN"
echo "  4. Configure Azure OpenAI API key credential on node 3"
echo "  5. Activate all workflows!"
echo ""
echo "Useful commands:"
echo "  az container logs -g $RESOURCE_GROUP -n $CONTAINER_GROUP --container-name n8n    # n8n logs"
echo "  az container logs -g $RESOURCE_GROUP -n $CONTAINER_GROUP --container-name caddy  # Caddy logs"
echo "  az container restart -g $RESOURCE_GROUP -n $CONTAINER_GROUP                      # Restart"
echo "  az container stop -g $RESOURCE_GROUP -n $CONTAINER_GROUP                         # Stop (saves cost)"
echo "  az container start -g $RESOURCE_GROUP -n $CONTAINER_GROUP                        # Start again"
echo "  az group delete -g $RESOURCE_GROUP --yes                                         # Delete everything"
echo ""
echo "SAVE THESE KEYS:"
echo "  ENCRYPTION KEY:      $N8N_ENCRYPTION_KEY"
echo "  MEAL PLANNER TOKEN:  $MEAL_PLANNER_TOKEN"
