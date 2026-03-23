import os
import base64
import httpx
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="AutoDeploy Agent Backend")

# Allow all origins (GitHub Pages, localhost)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Secrets from environment variables (set in Azure Container Apps)
GEMINI_KEY   = os.environ.get("GEMINI_KEY", "")
GH_PAT       = os.environ.get("GH_PAT", "")
GH_USERNAME  = os.environ.get("GH_USERNAME", "")
GH_REPO      = os.environ.get("GH_REPO", "")
DH_USERNAME  = os.environ.get("DH_USERNAME", "")
AZURE_RG     = os.environ.get("AZURE_RG", "")
ACA_ENV      = os.environ.get("ACA_ENV", "")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL   = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
GH_API       = "https://api.github.com"

# ── Models ────────────────────────────────────────────────────────────────────
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
        "gemini": bool(GEMINI_KEY),
        "github": bool(GH_PAT),
        "repo": f"{GH_USERNAME}/{GH_REPO}" if GH_USERNAME and GH_REPO else "not set"
    }

# ── Generate project ──────────────────────────────────────────────────────────
@app.post("/generate")
async def generate(req: GenerateRequest):
    if not GEMINI_KEY:
        raise HTTPException(500, "GEMINI_KEY not configured")

    system_prompt = f"""You are an autonomous DevOps coding agent. Generate a complete deployable Python project.

Respond with ONLY valid JSON — no markdown fences, no explanation:
{{
  "projectName": "lowercase-kebab-name",
  "description": "one line description",
  "files": {{
    "main.py": "complete python code",
    "requirements.txt": "one dependency per line",
    "Dockerfile": "complete dockerfile",
    ".github/workflows/docker-build.yml": "complete github actions workflow"
  }},
  "port": 8080,
  "dockerRunCommand": "docker run -p 8080:8080 {DH_USERNAME}/PROJNAME:latest",
  "summary": "what was built"
}}

Rules:
- Python: production-ready, proper error handling, logging
- Dockerfile: python:3.11-slim base, non-root user, EXPOSE correct port
- requirements.txt: all pip packages
- GitHub Actions workflow:
  * trigger: push to main
  * steps: checkout → docker/login-action@v3 with DOCKERHUB_USERNAME + DOCKERHUB_TOKEN secrets → docker/build-push-action@v5 push {DH_USERNAME}/projectName:latest and :${{{{ github.sha }}}}
  * Then: azure/login@v2 with AZURE_CREDENTIALS secret
  * Then install containerapp: az extension add --name containerapp --upgrade -y
  * Then deploy:
    IMAGE="{DH_USERNAME}/projectName:${{{{ github.sha }}}}"
    EXISTS=$(az containerapp show --name projectName --resource-group ${{{{ secrets.AZURE_RESOURCE_GROUP }}}} --query name -o tsv 2>/dev/null || echo "")
    if [ -z "$EXISTS" ]; then
      az containerapp create --name projectName --resource-group ${{{{ secrets.AZURE_RESOURCE_GROUP }}}} --environment ${{{{ secrets.ACA_ENVIRONMENT }}}} --image $IMAGE --target-port 8080 --ingress external --min-replicas 0 --max-replicas 3
    else
      az containerapp update --name projectName --resource-group ${{{{ secrets.AZURE_RESOURCE_GROUP }}}} --image $IMAGE
    fi
- projectName: lowercase letters, numbers, hyphens only, max 32 chars
- Replace projectName with actual name everywhere in the workflow"""

    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(GEMINI_URL, json={
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
        import re
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            project = json.loads(match.group(0))
        else:
            raise HTTPException(500, "AI returned invalid JSON. Please try again.")

    return project

# ── Deploy to GitHub ──────────────────────────────────────────────────────────
@app.post("/deploy")
async def deploy(req: DeployRequest):
    if not GH_PAT:
        raise HTTPException(500, "GH_PAT not configured")

    project = req.project
    project_name = project.get("projectName", "my-app")
    files = project.get("files", {})

    headers = {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    async with httpx.AsyncClient(timeout=30) as client:
        # Ensure repo exists
        check = await client.get(f"{GH_API}/repos/{GH_USERNAME}/{GH_REPO}", headers=headers)
        if check.status_code == 404:
            create = await client.post(f"{GH_API}/user/repos", headers=headers, json={
                "name": GH_REPO,
                "description": "AutoDeploy Agent projects",
                "private": False,
                "auto_init": True
            })
            if not create.is_success:
                raise HTTPException(500, f"Could not create repo: {create.text}")
            import asyncio
            await asyncio.sleep(2)

        # Push each file
        pushed = []
        for filename, content in files.items():
            # Workflow goes to root .github/workflows/ with project prefix
            if filename.startswith(".github/"):
                repo_path = filename.replace(
                    ".github/workflows/",
                    f".github/workflows/{project_name}-"
                )
            else:
                repo_path = f"{project_name}/{filename}"

            encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

            # Get existing SHA
            existing = await client.get(
                f"{GH_API}/repos/{GH_USERNAME}/{GH_REPO}/contents/{repo_path}",
                headers=headers
            )
            sha = existing.json().get("sha") if existing.is_success else None

            body = {
                "message": f"🤖 AutoDeploy: {'update' if sha else 'add'} {filename} for {project_name}",
                "content": encoded,
            }
            if sha:
                body["sha"] = sha

            put = await client.put(
                f"{GH_API}/repos/{GH_USERNAME}/{GH_REPO}/contents/{repo_path}",
                headers=headers,
                json=body
            )
            if not put.is_success:
                raise HTTPException(500, f"Failed to push {filename}: {put.text}")

            pushed.append(repo_path)

    return {
        "success": True,
        "pushed": pushed,
        "repoUrl": f"https://github.com/{GH_USERNAME}/{GH_REPO}",
        "actionsUrl": f"https://github.com/{GH_USERNAME}/{GH_REPO}/actions",
        "projectName": project_name
    }
