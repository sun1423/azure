# V2
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
GEMINI_KEY   = os.environ.get("GEMINI_KEY", "")
DH_USERNAME  = os.environ.get("DH_USERNAME", "")
DH_TOKEN     = os.environ.get("DH_TOKEN", "")
AZURE_RG     = os.environ.get("AZURE_RG", "")
ACA_ENV      = os.environ.get("ACA_ENV", "")

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
    }

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

# ── Deploy: build image → push Docker Hub → deploy to ACA ────────────────────
@app.post("/deploy")
async def deploy(req: DeployRequest):
    if not DH_USERNAME or not DH_TOKEN:
        raise HTTPException(500, "Docker Hub credentials not configured")
    if not AZURE_RG or not ACA_ENV:
        raise HTTPException(500, "Azure credentials not configured")

    project      = req.project
    project_name = project.get("projectName", "my-app")
    files        = project.get("files", {})
    port         = project.get("port", 8080)
    image_tag    = f"{DH_USERNAME}/{project_name}:latest"

    # Write files to temp directory and build Docker image
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write all project files
        for filename, content in files.items():
            filepath = os.path.join(tmpdir, filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True) if os.path.dirname(filepath) else None
            with open(filepath, "w") as f:
                f.write(content)

        # Step 1: Docker login
        login_result = subprocess.run(
            ["docker", "login", "-u", DH_USERNAME, "--password-stdin"],
            input=DH_TOKEN, capture_output=True, text=True
        )
        if login_result.returncode != 0:
            raise HTTPException(500, f"Docker login failed: {login_result.stderr}")

        # Step 2: Build Docker image
        build_result = subprocess.run(
            ["docker", "build", "-t", image_tag, tmpdir],
            capture_output=True, text=True
        )
        if build_result.returncode != 0:
            raise HTTPException(500, f"Docker build failed: {build_result.stderr}")

        # Step 3: Push to Docker Hub
        push_result = subprocess.run(
            ["docker", "push", image_tag],
            capture_output=True, text=True
        )
        if push_result.returncode != 0:
            raise HTTPException(500, f"Docker push failed: {push_result.stderr}")

    # Step 4: Deploy to Azure Container Apps using az CLI
    # Check if app exists
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
