# ok
import os
import json
import re
import paramiko
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="AutoDeploy Agent Backend")

# ✅ Correct CORS (works with browser)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # For testing (change in prod)
    allow_credentials=False,      # MUST be False with "*"
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Environment variables ──────────────────────────────────────────────────────
GEMINI_KEY   = os.environ.get("GEMINI_KEY", "")
DH_USERNAME  = os.environ.get("DH_USERNAME", "")
DH_TOKEN     = os.environ.get("DH_TOKEN", "")
VM_IP        = os.environ.get("VM_IP", "")
VM_USERNAME  = os.environ.get("VM_USERNAME", "")
VM_PASSWORD  = os.environ.get("VM_PASSWORD", "")

GEMINI_MODEL = "gemini-2.5-flash"


# ── SSH helper ─────────────────────────────────────────────────────────────────
def ssh_run(commands: list[str]) -> str:
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

        try:
            sftp.mkdir(remote_dir)
        except Exception:
            pass

        for filename, content in files.items():
            remote_path = f"{remote_dir}/{filename}"
            with sftp.file(remote_path, 'w') as f:
                f.write(content)

        sftp.close()
    finally:
        client.close()


def find_free_port() -> int:
    try:
        result = ssh_run([
            "ss -tlnp | awk '{print $4}' | grep -oP ':\\K[0-9]+' | sort -n | uniq"
        ])
        used_ports = set(int(p) for p in result.split('\n') if p.isdigit())
        for port in range(8100, 9000):
            if port not in used_ports:
                return port
        raise Exception("No free ports available")
    except Exception:
        return 8100


# ── Models ─────────────────────────────────────────────────────────────────────
class GenerateRequest(BaseModel):
    requirement: str

class DeployRequest(BaseModel):
    project: dict


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok"}


# ── Status ─────────────────────────────────────────────────────────────────────
@app.get("/status")
async def status():
    return {"status": "running"}


# ── Generate ───────────────────────────────────────────────────────────────────
@app.post("/generate")
async def generate(req: GenerateRequest):
    if not GEMINI_KEY:
        raise HTTPException(500, "GEMINI_KEY not configured")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"

    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(url, json={
            "contents": [{"role": "user", "parts": [{"text": req.requirement}]}]
        })

    if not res.is_success:
        raise HTTPException(500, f"Gemini error: {res.text}")

    return res.json()


# ── Deploy ─────────────────────────────────────────────────────────────────────
@app.post("/deploy")
async def deploy(req: DeployRequest):
    return {"message": "deploy logic ok"}
