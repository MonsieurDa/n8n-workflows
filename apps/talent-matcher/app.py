"""Talent Matcher : serveur web.

Lancement :  uvicorn app:app --port 8000   puis ouvrir http://localhost:8000
"""

from __future__ import annotations

import html
import os
import smtplib
import threading
import time
import uuid
from dataclasses import asdict
from email.message import EmailMessage
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field

from matcher import MatchError, SearchRequest, SearchResult, find_candidates

try:  # .env facultatif
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

STATIC = Path(__file__).with_name("static")
JOB_TTL_SECONDS = 6 * 3600

app = FastAPI(title="Talent Matcher")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


class SearchIn(BaseModel):
    description: str = Field(min_length=30, max_length=20000)
    title: str = Field(default="", max_length=200)
    contractType: Literal["freelance", "employee", "any"] = "any"
    location: str = Field(default="", max_length=200)
    remote: bool = False
    seniority: str = Field(default="", max_length=100)
    maxCandidates: int = Field(default=15, ge=1, le=50)
    notifyEmail: EmailStr | None = None


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM"))


def send_email(to: str, result: SearchResult) -> None:
    flabel = {"yes": "Freelance", "no": "Salarié", "unknown": "?"}
    title = result.offer["title"] or "votre mission"
    rows = "".join(
        f"<tr><td style='padding:8px;border-bottom:1px solid #eee'>{i}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee'><a href='{html.escape(c['url'])}'>{html.escape(c['name'])}</a>"
        f"<br><span style='color:#666;font-size:12px'>{html.escape(c['headline'])}</span></td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:center'><b>{c['score']}</b></td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee'>{flabel.get(c['freelance'], '?')}</td>"
        f"<td style='padding:8px;border-bottom:1px solid #eee;font-size:13px'>{html.escape(c['summary'])}</td></tr>"
        for i, c in enumerate(result.candidates, 1)
    ) or "<tr><td colspan='5' style='padding:12px'>Aucun candidat pertinent trouvé.</td></tr>"
    body = (
        f"<div style='font-family:Arial,sans-serif;max-width:900px'>"
        f"<h2 style='margin:0 0 4px'>Candidats pour : {html.escape(title)}</h2>"
        f"<p style='color:#555'>{len(result.candidates)} profil(s) retenu(s) sur {result.stats['profilesFound']} repéré(s).</p>"
        f"<table style='border-collapse:collapse;width:100%;font-size:14px'><thead><tr style='background:#f4f4f6;text-align:left'>"
        f"<th style='padding:8px'>#</th><th style='padding:8px'>Candidat</th><th style='padding:8px'>Score</th>"
        f"<th style='padding:8px'>Statut</th><th style='padding:8px'>Pourquoi</th></tr></thead><tbody>{rows}</tbody></table>"
        f"<p style='color:#888;font-size:12px'>Scores estimés par IA à partir des informations publiques : à vérifier avant tout contact.</p></div>"
    )
    msg = EmailMessage()
    msg["Subject"] = f"[Sourcing] {len(result.candidates)} candidat(s) pour « {title} »"
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg.set_content("Ouvrez ce message dans un client e-mail compatible HTML.")
    msg.add_alternative(body, subtype="html")
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=30) as s:
        if port != 25:
            s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.getenv("SMTP_PASSWORD", ""))
        s.send_message(msg)


def run_job(job_id: str, payload: SearchIn) -> None:
    def progress(msg: str) -> None:
        with jobs_lock:
            jobs[job_id]["progress"] = msg

    try:
        result = find_candidates(
            SearchRequest(
                description=payload.description,
                title=payload.title,
                contract_type=payload.contractType,
                location=payload.location,
                remote=payload.remote,
                seniority=payload.seniority,
                max_candidates=payload.maxCandidates,
            ),
            on_progress=progress,
        )
        out = asdict(result)
        out["email"] = None
        if payload.notifyEmail:
            if not smtp_configured():
                out["email"] = "L'envoi par e-mail n'est pas configuré sur le serveur (SMTP_HOST, SMTP_FROM)."
            else:
                progress("Envoi de la liste par e-mail")
                try:
                    send_email(payload.notifyEmail, result)
                    out["email"] = f"Liste envoyée à {payload.notifyEmail}"
                except Exception as e:  # l'échec de l'e-mail ne doit pas perdre les résultats
                    out["email"] = f"La liste n'a pas pu être envoyée par e-mail : {e}"
        with jobs_lock:
            jobs[job_id].update(status="done", result=out)
    except MatchError as e:
        with jobs_lock:
            jobs[job_id].update(status="error", error=str(e))
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update(status="error", error=f"Erreur inattendue : {e}")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config() -> dict:
    return {"anthropic": bool(os.getenv("ANTHROPIC_API_KEY")), "email": smtp_configured()}


@app.post("/api/search", status_code=202)
def start_search(payload: SearchIn) -> dict:
    now = time.time()
    job_id = uuid.uuid4().hex
    with jobs_lock:
        for k in [k for k, j in jobs.items() if now - j["created"] > JOB_TTL_SECONDS]:
            del jobs[k]
        jobs[job_id] = {"status": "running", "progress": "Démarrage", "created": now}
    threading.Thread(target=run_job, args=(job_id, payload), daemon=True).start()
    return {"id": job_id}


@app.get("/api/search/{job_id}")
def get_search(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Recherche introuvable (le serveur a peut-être redémarré).")
        return {k: v for k, v in job.items() if k != "created"}
