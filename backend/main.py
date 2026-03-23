import os
import json
import re
import tempfile
import subprocess
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="AutoDeploy Agent Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Secrets from Azure Container Apps environment variables
GEMINI_KEY         = os.environ.get("GEMINI_KEY", "")
DH_USERNAME        = os.environ.get("DH_USERNAME", "")
DH_TOKEN           = os.environ.get("DH_TOKEN", "")
AZURE_RG           = os.environ.get("AZURE_RG", "")
ACA_ENV            = os.environ.get("ACA_ENV", "")
ACR_NAME           = os.environ.get("ACR_NAME", "")
AZURE_CREDENTIALS = os.environ.get("AZURE_CREDENTIALS", "")

# Login to Azure on startup
# Try managed identity first (best for ACA), fallback to service principal
def azure_login():
    # Try managed identity first
    r = subprocess.run(
        ["az", "login", "--identity"],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        print("Azure login successful via managed identity")
        return

    # Fallback to service principal
    if not AZURE_CREDENTIALS:
        print("WARNING: AZURE_CREDENTIALS not set and managed identity failed")
        return
    try:
        creds = json.loads(AZURE_CREDENTIALS)
        r = subprocess.run([
            "az", "login", "--service-principal",
            "--username", creds.get("clientId", ""),
            "--password", creds.get("clientSecret", ""),
            "--tenant",   creds.get("tenantId", "")
        ], capture_output=True, text=True)
        if r.returncode == 0:
            subprocess.run(["az", "account", "set",
                "--subscription", creds.get("subscriptionId", "")],
                capture_output=True, text=True)
            print("Azure login successful via service principal")
        else:
            print(f"Azure login failed: {r.stderr}")
    except Exception as e:
        print(f"Azure login error: {e}")

azure_login()

AZURE_CLIENT_ID    = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET= os.environ.get("AZURE_CLIENT_SECRET", "")
AZURE_TENANT_ID    = os.environ.get("AZURE_TENANT_ID", "")

# Auto-login to Azure on startup using service principal
def az_login():
    if AZURE_CLIENT_ID and AZURE_CLIENT_SECRET and AZURE_TENANT_ID:
        result = subprocess.run([
            "az", "login",
            "--service-principal",
            "--username", AZURE_CLIENT_ID,
            "--password", AZURE_CLIENT_SECRET,
            "--tenant", AZURE_TENANT_ID
        ], capture_output=True, text=True)
        if result.returncode == 0:
            print("Azure login successful")
        else:
            print("Azure login failed:", result.stderr)
    else:
        print("Azure credentials not set - skipping login")

az_login()

GEMINI_MODEL = "gemini-2.5-flash"

class GenerateRequest(BaseModel):
    requirement: str

class DeployRequest(BaseModel):
    project: dict

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/")
@app.get("/health")
def health():
    return {
        "status": "ok",
        "gemini":  bool(GEMINI_KEY),
        "docker":  bool(DH_USERNAME and DH_TOKEN),
        "azure":   bool(AZURE_RG and ACA_ENV),
        "acr":     bool(ACR_NAME),
    }

# ── Full status check ─────────────────────────────────────────────────────────
@app.get("/status")
async def status():
    results = {}

    # Check Gemini
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}",
                json={"contents": [{"role": "user", "parts": [{"text": "say ok"}]}],
                      "generationConfig": {"maxOutputTokens": 5}}
            )
            results["gemini"] = "✅ Connected" if res.is_success else f"❌ Error {res.status_code}"
    except Exception as e:
        results["gemini"] = f"❌ {str(e)}"

    # Check Docker Hub credentials
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"https://hub.docker.com/v2/users/{DH_USERNAME}/",
            )
            results["dockerhub"] = "✅ Connected" if res.status_code in [200, 401] else f"❌ Error {res.status_code}"
            results["dockerhub_user"] = DH_USERNAME
    except Exception as e:
        results["dockerhub"] = f"❌ {str(e)}"

    # Check Azure config
    results["azure_rg"]  = f"✅ {AZURE_RG}"  if AZURE_RG  else "❌ Not set"
    results["azure_env"] = f"✅ {ACA_ENV}"   if ACA_ENV   else "❌ Not set"

    # Try Azure CLI login first
    azure_login()

    # Check Azure CLI
    try:
        check = subprocess.run(
            ["az", "account", "show", "--query", "name", "-o", "tsv"],
            capture_output=True, text=True, timeout=10
        )
        results["azure_cli"] = "✅ Logged in" if check.returncode == 0 else "⚠️ Not logged in (will login on deploy)"
    except Exception as e:
        results["azure_cli"] = "⚠️ Not checked"

    # List existing container apps
    try:
        apps = subprocess.run(
            ["az", "containerapp", "list",
             "--resource-group", AZURE_RG,
             "--query", "[].{name:name, url:properties.configuration.ingress.fqdn}",
             "-o", "json"],
            capture_output=True, text=True, timeout=15
        )
        if apps.returncode == 0:
            app_list = json.loads(apps.stdout or "[]")
            results["deployed_apps"] = app_list if app_list else "No apps deployed yet"
        else:
            results["deployed_apps"] = "No apps yet (login happens on deploy)"
    except Exception as e:
        results["deployed_apps"] = "No apps yet"

    all_ok = all("✅" in str(v) for v in [results["gemini"], results["dockerhub"], results["azure_rg"], results["azure_env"]])
    results["overall"] = "✅ All systems ready! Type your requirement to deploy." if all_ok else "⚠️ Some issues found"

    return results

# ── Generate project via Gemini ───────────────────────────────────────────────
@app.post("/generate")
async def generate(req: GenerateRequest):
    if not GEMINI_KEY:
        raise HTTPException(500, "GEMINI_KEY not configured on backend")

    system_prompt = f"""You are an autonomous DevOps coding agent. Generate a complete deployable Python project.

Respond with ONLY valid JSON — no markdown fences, no extra text:
{{
  "projectName": "lowercase-kebab-name",
  "description": "one line description",
  "port": 8080,
  "files": {{
    "main.py": "complete python code",
    "requirements.txt": "one dependency per line",
    "Dockerfile": "complete dockerfile"
  }},
  "summary": "brief description of what was built"
}}

Rules:
- Python: production-ready, proper error handling, logging to stdout
- Dockerfile: python:3.11-slim base, non-root user, EXPOSE correct port, CMD uvicorn or flask run
- requirements.txt: all pip packages needed, one per line
- projectName: lowercase letters, numbers, hyphens only, max 32 chars
- Port should match what the app listens on (default 8080)
- Do NOT include any GitHub Actions workflow — deployment is handled separately"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"

    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(url, json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": f"Requirement: {req.requirement}"}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096}
        })

    if not res.is_success:
        raise HTTPException(500, f"Gemini error: {res.text}")

    data = res.json()
    text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()

    try:
        project = json.loads(text)
    except Exception:
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            project = json.loads(match.group(0))
        else:
            raise HTTPException(500, "AI returned invalid JSON. Please try again.")

    return project

# ── Deploy: build via ACR Task → push to ACR → deploy to ACA ─────────────────
@app.post("/deploy")
async def deploy(req: DeployRequest):
    if not AZURE_RG or not ACA_ENV:
        raise HTTPException(500, "Azure credentials not configured")

    ACR_NAME = os.environ.get("ACR_NAME", "")
    if not ACR_NAME:
        raise HTTPException(500, "ACR_NAME not configured")

    project      = req.project
    project_name = project.get("projectName", "my-app")
    files        = project.get("files", {})
    port         = project.get("port", 8080)
    image_tag    = f"{ACR_NAME}.azurecr.io/{project_name}:latest"

    # Login to Azure before doing anything
    if not AZURE_CREDENTIALS:
        raise HTTPException(500, "AZURE_CREDENTIALS not set in container environment")
    try:
        creds = json.loads(AZURE_CREDENTIALS)
        login = subprocess.run([
            "az", "login", "--service-principal",
            "--username", creds.get("clientId", ""),
            "--password", creds.get("clientSecret", ""),
            "--tenant",   creds.get("tenantId", "")
        ], capture_output=True, text=True)
        if login.returncode != 0:
            raise HTTPException(500, f"Azure login failed: {login.stderr}")
        subprocess.run([
            "az", "account", "set",
            "--subscription", creds.get("subscriptionId", "")
        ], capture_output=True, text=True)
    except json.JSONDecodeError:
        raise HTTPException(500, "AZURE_CREDENTIALS is not valid JSON")

    # Write files to temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        for filename, content in files.items():
            filepath = os.path.join(tmpdir, filename)
            dirpath = os.path.dirname(filepath)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            with open(filepath, "w") as f:
                f.write(content)

        # Build image using ACR Task (runs in Azure cloud - no Docker daemon needed)
        build_result = subprocess.run([
            "az", "acr", "build",
            "--registry", ACR_NAME,
            "--image", f"{project_name}:latest",
            "--file", os.path.join(tmpdir, "Dockerfile"),
            tmpdir
        ], capture_output=True, text=True, timeout=300)

        if build_result.returncode != 0:
            raise HTTPException(500, f"ACR build failed: {build_result.stderr}")

    # Deploy to Azure Container Apps using az CLI
    check = subprocess.run(
        ["az", "containerapp", "show",
         "--name", project_name,
         "--resource-group", AZURE_RG,
         "--query", "name", "-o", "tsv"],
        capture_output=True, text=True
    )

    if check.returncode == 0 and check.stdout.strip():
        # Delete old container app first
        print(f"Deleting old container app: {project_name}")
        delete = subprocess.run([
            "az", "containerapp", "delete",
            "--name", project_name,
            "--resource-group", AZURE_RG,
            "--yes"
        ], capture_output=True, text=True)
        if delete.returncode != 0:
            raise HTTPException(500, f"Failed to delete old container: {delete.stderr}")
        # Wait for deletion to complete
        import time
        time.sleep(10)

    # Create fresh container app
    print(f"Creating new container app: {project_name}")
    create = subprocess.run([
        "az", "containerapp", "create",
        "--name", project_name,
        "--resource-group", AZURE_RG,
        "--environment", ACA_ENV,
        "--image", image_tag,
        "--target-port", str(port),
        "--ingress", "external",
        "--min-replicas", "0",
        "--max-replicas", "3",
        "--cpu", "0.5",
        "--memory", "1.0Gi"
    ], capture_output=True, text=True)

    if create.returncode != 0:
        raise HTTPException(500, f"Azure deploy failed: {create.stderr}")

    # Get app URL
    url_result = subprocess.run([
        "az", "containerapp", "show",
        "--name", project_name,
        "--resource-group", AZURE_RG,
        "--query", "properties.configuration.ingress.fqdn",
        "-o", "tsv"
    ], capture_output=True, text=True)

    app_url = f"https://{url_result.stdout.strip()}" if url_result.stdout.strip() else "Check Azure Portal"

    return {
        "success":     True,
        "projectName": project_name,
        "image":       image_tag,
        "appUrl":      app_url,
    }
