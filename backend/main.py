import os
import json
import re
import tempfile
import subprocess
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

# ── Env vars (set via deploy-backend.yml → Azure Container Apps) ──────────────
GEMINI_KEY          = os.environ.get("GEMINI_KEY", "")
DH_USERNAME         = os.environ.get("DH_USERNAME", "")
DH_TOKEN            = os.environ.get("DH_TOKEN", "")
AZURE_RG            = os.environ.get("AZURE_RG", "")
ACA_ENV             = os.environ.get("ACA_ENV", "")
ACR_NAME            = os.environ.get("ACR_NAME", "")
AZURE_CLIENT_ID     = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")
AZURE_TENANT_ID     = os.environ.get("AZURE_TENANT_ID", "")
AZURE_SUB_ID        = os.environ.get("AZURE_SUB_ID", "")

GEMINI_MODEL = "gemini-2.5-flash"

# ── Azure login helper ─────────────────────────────────────────────────────────
def do_azure_login():
    """Login to Azure using service principal credentials."""
    if not all([AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID]):
        raise HTTPException(500, "Azure credentials missing: AZURE_CLIENT_ID, AZURE_CLIENT_SECRET or AZURE_TENANT_ID not set")

    login = subprocess.run([
        "az", "login", "--service-principal",
        "--username", AZURE_CLIENT_ID,
        "--password", AZURE_CLIENT_SECRET,
        "--tenant",   AZURE_TENANT_ID
    ], capture_output=True, text=True)

    if login.returncode != 0:
        raise HTTPException(500, f"Azure login failed: {login.stderr}")

    if AZURE_SUB_ID:
        subprocess.run([
            "az", "account", "set",
            "--subscription", AZURE_SUB_ID
        ], capture_output=True, text=True)


# ── Models ─────────────────────────────────────────────────────────────────────
class GenerateRequest(BaseModel):
    requirement: str

class DeployRequest(BaseModel):
    project: dict


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/")
@app.get("/health")
def health():
    return {
        "status":  "ok",
        "gemini":  bool(GEMINI_KEY),
        "docker":  bool(DH_USERNAME and DH_TOKEN),
        "azure":   bool(AZURE_RG and ACA_ENV),
        "acr":     bool(ACR_NAME),
        "az_creds": bool(AZURE_CLIENT_ID and AZURE_CLIENT_SECRET and AZURE_TENANT_ID),
    }


# ── Status ─────────────────────────────────────────────────────────────────────
@app.get("/status")
async def status():
    results = {}

    # Check Gemini
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}",
                json={
                    "contents": [{"role": "user", "parts": [{"text": "say ok"}]}],
                    "generationConfig": {"maxOutputTokens": 5}
                }
            )
            results["gemini"] = "✅ Connected" if res.is_success else f"❌ Error {res.status_code}: {res.text[:100]}"
    except Exception as e:
        results["gemini"] = f"❌ {str(e)}"

    # Check Docker Hub
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(f"https://hub.docker.com/v2/users/{DH_USERNAME}/")
            results["dockerhub"] = f"✅ Connected ({DH_USERNAME})" if res.status_code in [200, 401] else f"❌ Error {res.status_code}"
    except Exception as e:
        results["dockerhub"] = f"❌ {str(e)}"

    results["azure_rg"]   = f"✅ {AZURE_RG}"  if AZURE_RG  else "❌ Not set"
    results["azure_env"]  = f"✅ {ACA_ENV}"   if ACA_ENV   else "❌ Not set"
    results["acr"]        = f"✅ {ACR_NAME}"  if ACR_NAME  else "❌ Not set"
    results["az_creds"]   = "✅ Set" if all([AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID]) else "❌ Missing"

    # Try Azure login and list apps
    try:
        do_azure_login()
        apps = subprocess.run([
            "az", "containerapp", "list",
            "--resource-group", AZURE_RG,
            "--query", "[].{name:name, url:properties.configuration.ingress.fqdn}",
            "-o", "json"
        ], capture_output=True, text=True, timeout=15)

        if apps.returncode == 0:
            app_list = json.loads(apps.stdout or "[]")
            results["deployed_apps"] = app_list if app_list else "No apps deployed yet"
            results["azure_cli"] = "✅ Logged in"
        else:
            results["azure_cli"] = "❌ Login failed"
            results["deployed_apps"] = []
    except Exception as e:
        results["azure_cli"] = f"⚠️ {str(e)[:80]}"
        results["deployed_apps"] = []

    all_ok = all([
        "✅" in str(results.get("gemini", "")),
        "✅" in str(results.get("dockerhub", "")),
        bool(AZURE_RG), bool(ACA_ENV), bool(ACR_NAME),
        bool(AZURE_CLIENT_ID and AZURE_CLIENT_SECRET),
    ])
    results["overall"] = "✅ All systems ready! Type your requirement to deploy." if all_ok else "⚠️ Some issues found — check above"

    return results


# ── Generate ───────────────────────────────────────────────────────────────────
@app.post("/generate")
async def generate(req: GenerateRequest):
    if not GEMINI_KEY:
        raise HTTPException(500, "GEMINI_KEY not configured")

    system_prompt = f"""You are an autonomous DevOps coding agent. Generate a complete deployable Python project.

Respond with ONLY valid JSON — no markdown fences, no extra text outside the JSON:
{{
  "projectName": "lowercase-kebab-name-max-32-chars",
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
- Dockerfile: use python:3.11-slim as base, create non-root user, EXPOSE the correct port, CMD to run the app
- requirements.txt: all pip packages needed, one per line
- projectName: lowercase letters, numbers, hyphens ONLY, max 32 chars, no spaces
- Port must match what the app listens on (use 8080 as default)
- Do NOT include GitHub Actions workflows — deployment is handled by the backend"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"

    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(url, json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": f"Requirement: {req.requirement}"}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096}
        })

    if not res.is_success:
        raise HTTPException(500, f"Gemini error: {res.text}")

    text = res.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    text = text.strip()
    # Strip markdown fences if present
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()

    try:
        project = json.loads(text)
    except Exception:
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            project = json.loads(match.group(0))
        else:
            raise HTTPException(500, "AI returned invalid JSON. Please try again.")

    return project


# ── Deploy ─────────────────────────────────────────────────────────────────────
@app.post("/deploy")
async def deploy(req: DeployRequest):
    # Validate all required config
    if not ACR_NAME:
        raise HTTPException(500, "ACR_NAME not configured")
    if not AZURE_RG or not ACA_ENV:
        raise HTTPException(500, "AZURE_RG or ACA_ENV not configured")
    if not all([AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID]):
        raise HTTPException(500, "Azure credentials not configured (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID)")

    project      = req.project
    project_name = project.get("projectName", "my-app")
    files        = project.get("files", {})
    port         = project.get("port", 8080)
    image_tag    = f"{ACR_NAME}.azurecr.io/{project_name}:latest"

    # Step 1: Login to Azure
    do_azure_login()

    # Step 2: Write files to temp dir and build via ACR Tasks
    with tempfile.TemporaryDirectory() as tmpdir:
        for filename, content in files.items():
            filepath = os.path.join(tmpdir, filename)
            dirpath = os.path.dirname(filepath)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            with open(filepath, "w") as f:
                f.write(content)

        # Build image using ACR Tasks (runs in Azure cloud — no Docker daemon needed)
        build = subprocess.run([
            "az", "acr", "build",
            "--registry", ACR_NAME,
            "--image", f"{project_name}:latest",
            "--file", os.path.join(tmpdir, "Dockerfile"),
            tmpdir
        ], capture_output=True, text=True, timeout=300)

        if build.returncode != 0:
            raise HTTPException(500, f"ACR build failed: {build.stderr}")

    # Step 3: Delete old container app if exists
    check = subprocess.run([
        "az", "containerapp", "show",
        "--name", project_name,
        "--resource-group", AZURE_RG,
        "--query", "name", "-o", "tsv"
    ], capture_output=True, text=True)

    if check.returncode == 0 and check.stdout.strip():
        delete = subprocess.run([
            "az", "containerapp", "delete",
            "--name", project_name,
            "--resource-group", AZURE_RG,
            "--yes"
        ], capture_output=True, text=True)
        if delete.returncode != 0:
            raise HTTPException(500, f"Failed to delete old app: {delete.stderr}")
        # Wait for deletion
        import time
        for _ in range(20):
            still = subprocess.run([
                "az", "containerapp", "show",
                "--name", project_name,
                "--resource-group", AZURE_RG,
                "--query", "name", "-o", "tsv"
            ], capture_output=True, text=True)
            if still.returncode != 0 or not still.stdout.strip():
                break
            time.sleep(10)

    # Step 4: Create new container app
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
        "--memory", "1.0Gi",
        "--registry-server", f"{ACR_NAME}.azurecr.io"
    ], capture_output=True, text=True)

    if create.returncode != 0:
        raise HTTPException(500, f"Azure deploy failed: {create.stderr}")

    # Step 5: Get app URL
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
