#te
import os
import json
import re
import tempfile
import paramiko
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
GROQ_KEY     = os.environ.get("GROQ_KEY", "")
DH_USERNAME  = os.environ.get("DH_USERNAME", "")
DH_TOKEN     = os.environ.get("DH_TOKEN", "")
VM_IP        = os.environ.get("VM_IP", "")
VM_USERNAME  = os.environ.get("VM_USERNAME", "")
VM_PASSWORD  = os.environ.get("VM_PASSWORD", "")

GROQ_MODEL   = "llama-3.3-70b-versatile"


# ── SSH helper ─────────────────────────────────────────────────────────────────
def ssh_run(commands: list[str]) -> str:
    """Connect to VM via SSH and run commands. Returns combined output."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=VM_IP,
            username=VM_USERNAME,
            password=VM_PASSWORD,
            timeout=30
        )
        output = []
        for cmd in commands:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=300)
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                raise Exception(f"Command failed: {cmd}\nError: {err}")
            if out:
                output.append(out)
        return "\n".join(output)
    finally:
        client.close()


def ssh_copy_files(files: dict, remote_dir: str):
    """Copy project files to VM via SFTP."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=VM_IP,
            username=VM_USERNAME,
            password=VM_PASSWORD,
            timeout=30
        )
        sftp = client.open_sftp()

        # Create remote directory
        try:
            sftp.mkdir(remote_dir)
        except Exception:
            pass  # already exists

        # Upload each file
        for filename, content in files.items():
            remote_path = f"{remote_dir}/{filename}"
            with sftp.file(remote_path, 'w') as f:
                f.write(content)

        sftp.close()
    finally:
        client.close()


def find_free_port() -> int:
    """Find a free port on the VM between 8100-8999."""
    try:
        result = ssh_run([
            "ss -tlnp | awk '{print $4}' | grep -oP ':\\K[0-9]+' | sort -n | uniq"
        ])
        used_ports = set(int(p) for p in result.split('\n') if p.isdigit())
        for port in range(8100, 9000):
            if port not in used_ports:
                return port
        raise Exception("No free ports available in range 8100-8999")
    except Exception:
        return 8100  # fallback


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
        "status":  "ok",
        "groq":    bool(GROQ_KEY),
        "docker":  bool(DH_USERNAME and DH_TOKEN),
        "vm":      bool(VM_IP and VM_USERNAME and VM_PASSWORD),
    }


# ── Status ─────────────────────────────────────────────────────────────────────
@app.get("/status")
async def status():
    results = {}

    # Groq
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_KEY}"},
                json={
                    "model": GROQ_MODEL,
                    "max_tokens": 5,
                    "messages": [{"role": "user", "content": "say ok"}]
                }
            )
        results["groq"] = "✅ Connected" if res.is_success else f"❌ Error {res.status_code}"
    except Exception as e:
        results["groq"] = f"❌ {str(e)}"

    # Docker Hub
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(f"https://hub.docker.com/v2/users/{DH_USERNAME}/")
        results["dockerhub"] = f"✅ Connected ({DH_USERNAME})" if res.status_code in [200, 401] else f"❌ {res.status_code}"
    except Exception as e:
        results["dockerhub"] = f"❌ {str(e)}"

    # VM SSH
    try:
        out = ssh_run(["echo ok && docker --version"])
        results["vm"] = f"✅ Connected ({VM_IP}) — {out.split(chr(10))[-1]}"
    except Exception as e:
        results["vm"] = f"❌ {str(e)[:80]}"

    # Running containers
    try:
        containers = ssh_run([
            "docker ps --format '{{.Names}} | {{.Ports}}' 2>/dev/null || echo 'none'"
        ])
        results["running_apps"] = containers if containers else "No containers running"
    except Exception:
        results["running_apps"] = "Could not check"

    all_ok = all("✅" in str(results.get(k, "")) for k in ["groq", "dockerhub", "vm"])
    results["overall"] = "✅ All systems ready! Type your requirement to deploy." if all_ok else "⚠️ Some issues found"

    return results


# ── Generate ───────────────────────────────────────────────────────────────────
@app.post("/generate")
async def generate(req: GenerateRequest):
    if not GROQ_KEY:
        raise HTTPException(500, "GROQ_KEY not configured")

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
- Default port: 8080 inside container (host port assigned dynamically)
- Do NOT include GitHub Actions workflows"""

    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": GROQ_MODEL,
                "temperature": 0.2,
                "max_tokens": 4096,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": f"Requirement: {req.requirement}"}
                ]
            }
        )

    if not res.is_success:
        raise HTTPException(500, f"Groq error: {res.text}")

    text = res.json().get("choices", [{}])[0].get("message", {}).get("content", "")
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
    if not all([VM_IP, VM_USERNAME, VM_PASSWORD]):
        raise HTTPException(500, "VM credentials not configured (VM_IP, VM_USERNAME, VM_PASSWORD)")
    if not all([DH_USERNAME, DH_TOKEN]):
        raise HTTPException(500, "Docker Hub credentials not configured")

    project      = req.project
    project_name = project.get("projectName", "my-app")
    files        = project.get("files", {})
    container_port = project.get("port", 8080)
    image_tag    = f"{DH_USERNAME}/{project_name}:latest"
    remote_dir   = f"/tmp/{project_name}"

    # Step 1: Copy files to VM
    ssh_copy_files(files, remote_dir)

    # Step 2: Build image on VM
    ssh_run([
        f"cd {remote_dir} && docker build -t {image_tag} ."
    ])

    # Step 3: Login to Docker Hub and push image
    ssh_run([
        f"echo '{DH_TOKEN}' | docker login -u '{DH_USERNAME}' --password-stdin",
        f"docker push {image_tag}"
    ])

    # Step 4: Stop and remove old container if exists
    try:
        ssh_run([
            f"docker stop {project_name} 2>/dev/null || true",
            f"docker rm {project_name} 2>/dev/null || true"
        ])
    except Exception:
        pass

    # Step 5: Find free port and run container
    host_port = find_free_port()
    ssh_run([
        f"docker pull {image_tag}",
        f"docker run -d --name {project_name} --restart unless-stopped -p {host_port}:{container_port} {image_tag}"
    ])

    # Step 6: Cleanup temp files
    try:
        ssh_run([f"rm -rf {remote_dir}"])
    except Exception:
        pass

    return {
        "success":     True,
        "projectName": project_name,
        "image":       image_tag,
        "hostPort":    host_port,
        "appUrl":      f"http://{VM_IP}:{host_port}",
    }
