import os
import json
import re
import time
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

# ── Environment variables ──────────────────────────────────────────────────────
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

GEMINI_MODEL = "Gemini 2.5 Flash Lite"


# ── Azure login ────────────────────────────────────────────────────────────────
def do_azure_login():
    """Try managed identity first, fall back to service principal."""

    # Managed identity (preferred — no credentials needed)
    r = subprocess.run(
        ["az", "login", "--identity"],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        print("Azure login via managed identity ✅")
        if AZURE_SUB_ID:
            subprocess.run(
                ["az", "account", "set", "--subscription", AZURE_SUB_ID],
                capture_output=True, text=True
            )
        return

    # Service principal fallback
    if not all([AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID]):
        raise HTTPException(500, "Azure login failed: no managed identity and service principal credentials missing")

    r2 = subprocess.run([
        "az", "login", "--service-principal",
        "--username", AZURE_CLIENT_ID,
        "--password", AZURE_CLIENT_SECRET,
        "--tenant",   AZURE_TENANT_ID
    ], capture_output=True, text=True)

    if r2.returncode != 0:
        raise HTTPException(500, f"Azure login failed: {r2.stderr}")

    if AZURE_SUB_ID:
        subprocess.run(
            ["az", "account", "set", "--subscription", AZURE_SUB_ID],
            capture_output=True, text=True
        )
    print("Azure login via service principal ✅")


# ── Request models ─────────────────────────────────────────────────────────────
class GenerateRequest(BaseModel):
    requirement: str

class DeployRequest(BaseModel):
    project: dict


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/")
@app.get("/health")
def health():
    return {
        "status":   "ok",
        "gemini":   bool(GEMINI_KEY),
        "docker":   bool(DH_USERNAME and DH_TOKEN),
        "azure":    bool(AZURE_RG and ACA_ENV),
        "acr":      bool(ACR_NAME),
        "az_creds": bool(AZURE_CLIENT_ID and AZURE_CLIENT_SECRET and AZURE_TENANT_ID),
    }


# ── Status ─────────────────────────────────────────────────────────────────────
@app.get("/status")
async def status():
    results = {}

    # Gemini
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}",
                json={
                    "contents": [{"role": "user", "parts": [{"text": "say ok"}]}],
                    "generationConfig": {"maxOutputTokens": 5}
                }
            )
        results["gemini"] = "✅ Connected" if res.is_success else f"❌ Error {res.status_code}"
    except Exception as e:
        results["gemini"] = f"❌ {str(e)}"

    # Docker Hub
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(f"https://hub.docker.com/v2/users/{DH_USERNAME}/")
        results["dockerhub"] = f"✅ Connected ({DH_USERNAME})" if res.status_code in [200, 401] else f"❌ {res.status_code}"
    except Exception as e:
        results["dockerhub"] = f"❌ {str(e)}"

    results["azure_rg"]  = f"✅ {AZURE_RG}"  if AZURE_RG  else "❌ Not set"
    results["azure_env"] = f"✅ {ACA_ENV}"   if ACA_ENV   else "❌ Not set"
    results["acr"]       = f"✅ {ACR_NAME}"  if ACR_NAME  else "❌ Not set"
    results["az_creds"]  = "✅ Set" if all([AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID]) else "⚠️ Using managed identity"

    # Azure CLI + list apps
    try:
        do_azure_login()
        apps = subprocess.run([
            "az", "containerapp", "list",
            "--resource-group", AZURE_RG,
            "--query", "[].{name:name, url:properties.configuration.ingress.fqdn}",
            "-o", "json"
        ], capture_output=True, text=True, timeout=15)

        app_list = json.loads(apps.stdout or "[]") if apps.returncode == 0 else []
        results["azure_cli"]     = "✅ Logged in"
        results["deployed_apps"] = app_list if app_list else "No apps deployed yet"
    except Exception as e:
        results["azure_cli"]     = f"⚠️ {str(e)[:80]}"
        results["deployed_apps"] = "Login needed"

    all_ok = all([
        "✅" in str(results.get("gemini", "")),
        "✅" in str(results.get("dockerhub", "")),
        bool(AZURE_RG), bool(ACA_ENV), bool(ACR_NAME),
    ])
    results["overall"] = "✅ All systems ready! Type your requirement to deploy." if all_ok else "⚠️ Some issues found"

    return results


# ── Generate ───────────────────────────────────────────────────────────────────
@app.post("/generate")
async def generate(req: GenerateRequest):
    if not GEMINI_KEY:
        raise HTTPException(500, "GEMINI_KEY not configured")

    system_prompt = """You are an autonomous DevOps coding agent. Generate a complete deployable Python project.

Respond with ONLY valid JSON — no markdown fences, no extra text:
{
  "projectName": "lowercase-kebab-max-32-chars",
  "description": "one line description",
  "port": 8080,
  "files": {
    "main.py": "complete python code",
    "requirements.txt": "one dependency per line",
    "Dockerfile": "complete dockerfile"
  },
  "summary": "what was built"
}

Rules:
- Python: production-ready, error handling, logging to stdout
- Dockerfile: python:3.11-slim base, non-root user, EXPOSE port, CMD to run app
- requirements.txt: all pip packages, one per line
- projectName: lowercase letters, numbers, hyphens ONLY, max 32 chars
- Default port: 8080
- Do NOT include GitHub Actions workflows"""

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
    text = re.sub(r'^```json\s*', '', text.strip())
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'\s*```$', '', text).strip()

    try:
        return json.loads(text)
    except Exception:
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return json.loads(match.group(0))
        raise HTTPException(500, "AI returned invalid JSON. Please try again.")


# ── Deploy ─────────────────────────────────────────────────────────────────────
@app.post("/deploy")
async def deploy(req: DeployRequest):

    # Validate config
    if not ACR_NAME:
        raise HTTPException(500, "ACR_NAME not set")
    if not AZURE_RG or not ACA_ENV:
        raise HTTPException(500, "AZURE_RG or ACA_ENV not set")

    project      = req.project
    project_name = project.get("projectName", "my-app")
    files        = project.get("files", {})
    port         = project.get("port", 8080)
    image_tag    = f"{ACR_NAME}.azurecr.io/{project_name}:latest"

    # Step 1: Login to Azure
    do_azure_login()

    # Step 2: Write files and build via ACR Tasks
    with tempfile.TemporaryDirectory() as tmpdir:
        for filename, content in files.items():
            filepath = os.path.join(tmpdir, filename)
            dirpath = os.path.dirname(filepath)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            with open(filepath, "w") as f:
                f.write(content)

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

        # Wait until deletion confirmed (max 3 min)
        for i in range(18):
            still = subprocess.run([
                "az", "containerapp", "show",
                "--name", project_name,
                "--resource-group", AZURE_RG,
                "--query", "name", "-o", "tsv"
            ], capture_output=True, text=True)
            if still.returncode != 0 or not still.stdout.strip():
                print(f"Deletion confirmed after {i+1} checks")
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
        raise HTTPException(500, f"Deploy failed: {create.stderr}")

    # Step 5: Get app URL
    url_res = subprocess.run([
        "az", "containerapp", "show",
        "--name", project_name,
        "--resource-group", AZURE_RG,
        "--query", "properties.configuration.ingress.fqdn",
        "-o", "tsv"
    ], capture_output=True, text=True)

    app_url = f"https://{url_res.stdout.strip()}" if url_res.stdout.strip() else "Check Azure Portal"

    return {
        "success":     True,
        "projectName": project_name,
        "image":       image_tag,
        "appUrl":      app_url,
    }
