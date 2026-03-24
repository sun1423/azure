import os
import json
import uuid
import socket
import subprocess
import tempfile
import textwrap
import requests
import paramiko
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Env vars set by deploy-backend.yml ──────────────────────────────────────
GEMINI_KEY   = os.getenv("GEMINI_KEY", "")
DH_USER      = os.getenv("DH_USERNAME", "")
DH_TOKEN     = os.getenv("DH_TOKEN", "")
VM_IP        = os.getenv("VM_IP", "")
VM_USER      = os.getenv("VM_USERNAME", "")
VM_PASS      = os.getenv("VM_PASSWORD", "")
# ────────────────────────────────────────────────────────────────────────────


# ── Helpers ──────────────────────────────────────────────────────────────────

def ssh_run(client: paramiko.SSHClient, cmd: str) -> tuple[str, str, int]:
    """Run a command over SSH, return stdout, stderr, exit_code."""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=300)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    code = stdout.channel.recv_exit_status()
    return out, err, code


def get_ssh_client() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=VM_IP,
        username=VM_USER,
        password=VM_PASS,
        timeout=30,
    )
    return client


def find_free_port(client: paramiko.SSHClient) -> int:
    """Find a free port on the VM between 8100-8999."""
    out, _, _ = ssh_run(client, "ss -tlnp | awk '{print $4}' | grep -oE '[0-9]+$' | sort -n")
    used = set(int(p) for p in out.splitlines() if p.isdigit())
    for port in range(8100, 8999):
        if port not in used:
            return port
    raise RuntimeError("No free ports available in range 8100-8999")


def call_gemini(system_prompt: str, user_msg: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-04-17:generateContent?key={GEMINI_KEY}"
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_msg}]}],
        "generationConfig": {"temperature": 0.2},
    }
    r = requests.post(url, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    status = {
        "status": "ok",
        "gemini": bool(GEMINI_KEY),
        "docker": bool(DH_USER and DH_TOKEN),
        "vm": bool(VM_IP and VM_USER and VM_PASS),
    }
    # Quick SSH reachability check
    if status["vm"]:
        try:
            c = get_ssh_client()
            c.close()
            status["vm_reachable"] = True
        except Exception as e:
            status["vm_reachable"] = False
            status["vm_error"] = str(e)
    return status


class DeployRequest(BaseModel):
    requirement: str


@app.post("/deploy")
def deploy(req: DeployRequest):
    requirement = req.requirement.strip()
    if not requirement:
        return {"success": False, "error": "Requirement is empty"}

    # ── 1. Generate code with Gemini ─────────────────────────────────────────
    system_prompt = textwrap.dedent("""
        You are an expert Python developer. Given a requirement, generate a complete
        deployable Python application. Respond with ONLY valid JSON, no markdown, no
        backticks. The JSON must have these keys:
          - app_name: lowercase, hyphens only, max 20 chars (e.g. "flask-api")
          - description: one sentence
          - main_py: full content of main.py
          - requirements_txt: content of requirements.txt (one package per line)
          - dockerfile: content of Dockerfile
          - port: integer port the app listens on inside the container (e.g. 8080)
        
        Rules:
        - Use Flask or FastAPI for web apps
        - The app must bind to 0.0.0.0 and use the port in the Dockerfile EXPOSE
        - Dockerfile must use python:3.11-slim base image
        - Include a /health endpoint that returns {"status": "ok"}
        - Keep the app simple and functional
    """)

    try:
        raw = call_gemini(system_prompt, requirement)
        # Strip potential markdown fences
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0]
        generated = json.loads(raw)
    except Exception as e:
        return {"success": False, "error": f"AI generation failed: {e}"}

    app_name        = generated.get("app_name", "myapp")
    main_py         = generated.get("main_py", "")
    requirements    = generated.get("requirements_txt", "")
    dockerfile      = generated.get("dockerfile", "")
    container_port  = int(generated.get("port", 8080))

    if not (main_py and requirements and dockerfile):
        return {"success": False, "error": "AI did not return complete files"}

    # ── 2. SSH into VM ───────────────────────────────────────────────────────
    try:
        client = get_ssh_client()
    except Exception as e:
        return {"success": False, "error": f"SSH connection failed: {e}"}

    try:
        build_id  = uuid.uuid4().hex[:8]
        image_tag = f"{DH_USER}/{app_name}:{build_id}"
        work_dir  = f"/tmp/build-{build_id}"

        # ── 3. Upload files to VM ─────────────────────────────────────────
        sftp = client.open_sftp()
        sftp.mkdir(work_dir)
        for fname, content in [
            (f"{work_dir}/main.py",           main_py),
            (f"{work_dir}/requirements.txt",  requirements),
            (f"{work_dir}/Dockerfile",         dockerfile),
        ]:
            with sftp.open(fname, "w") as f:
                f.write(content)
        sftp.close()

        # ── 4. Build Docker image on VM ───────────────────────────────────
        out, err, code = ssh_run(client, f"docker build -t {image_tag} {work_dir}")
        if code != 0:
            return {"success": False, "error": f"Docker build failed: {err or out}"}

        # ── 5. Push to Docker Hub ─────────────────────────────────────────
        out, err, code = ssh_run(
            client,
            f"echo '{DH_TOKEN}' | docker login --username '{DH_USER}' --password-stdin && "
            f"docker push {image_tag}"
        )
        if code != 0:
            return {"success": False, "error": f"Docker push failed: {err or out}"}

        # ── 6. Find free port & run container ─────────────────────────────
        host_port = find_free_port(client)
        out, err, code = ssh_run(
            client,
            f"docker run -d --name {app_name}-{build_id} "
            f"-p {host_port}:{container_port} "
            f"--restart unless-stopped {image_tag}"
        )
        if code != 0:
            return {"success": False, "error": f"Docker run failed: {err or out}"}

        # ── 7. Cleanup build dir ──────────────────────────────────────────
        ssh_run(client, f"rm -rf {work_dir}")

        app_url = f"http://{VM_IP}:{host_port}"
        return {
            "success":      True,
            "app_name":     app_name,
            "image":        image_tag,
            "url":          app_url,
            "host_port":    host_port,
            "files": {
                "main.py":          main_py,
                "requirements.txt": requirements,
                "Dockerfile":       dockerfile,
            },
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        client.close()


@app.get("/apps")
def list_apps():
    """List running containers on the VM."""
    try:
        client = get_ssh_client()
        out, err, code = ssh_run(
            client,
            "docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}|{{.Status}}'"
        )
        client.close()
        apps = []
        for line in out.splitlines():
            if "|" in line:
                parts = line.split("|")
                apps.append({
                    "name":   parts[0],
                    "image":  parts[1],
                    "ports":  parts[2],
                    "status": parts[3],
                })
        return {"apps": apps}
    except Exception as e:
        return {"apps": [], "error": str(e)}


@app.delete("/apps/{container_name}")
def stop_app(container_name: str):
    """Stop and remove a container on the VM."""
    try:
        client = get_ssh_client()
        ssh_run(client, f"docker stop {container_name}")
        ssh_run(client, f"docker rm {container_name}")
        client.close()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
