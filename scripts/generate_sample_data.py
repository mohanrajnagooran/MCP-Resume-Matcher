#!/usr/bin/env python3
"""Create the sample job description and resumes (TXT, MD, PDF, DOCX) used by the
demo and the tests. Safe to re-run: it overwrites the sample files only.

    python scripts/generate_sample_data.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESUMES = ROOT / "data" / "resumes"
JOBS = ROOT / "data" / "jobs"
INCOMING = ROOT / "data" / "incoming_samples"

JOB = """# Senior Python AI Engineer

**Location:** Chennai (Hybrid) | **Experience:** 5+ years

## About the role
We are building agentic AI systems for talent acquisition. You will design LLM-powered
agents, connect them to tools through the Model Context Protocol, and ship them to production.

## Required skills
- Python (5+ years)
- LangChain / LangGraph
- LLM application development, RAG
- FastAPI or Flask REST APIs
- Docker
- SQL / PostgreSQL
- Git and CI/CD

## Nice to have
- Model Context Protocol (MCP)
- Kubernetes
- AWS or GCP
- Vector databases (FAISS, Pinecone, Chroma)
- Prompt engineering
- Machine learning, PyTorch

## Education
Bachelor's degree in Computer Science or a related field.
"""

RESUMES_DATA = {
    "priya_raman": dict(fmt="pdf", text="""Priya Raman
priya.raman@example.com | +91 98400 11223 | Chennai, India | linkedin.com/in/priyaraman

SUMMARY
AI engineer with 7 years of experience building Python backends and LLM applications.
Led the design of multi-agent systems with LangGraph and tool integrations over the Model Context Protocol (MCP).

SKILLS
Python, LangChain, LangGraph, LLM, RAG, Prompt Engineering, FastAPI, Docker, Kubernetes,
PostgreSQL, SQL, FAISS, Pinecone, AWS, Git, CI/CD, PyTorch

EXPERIENCE
Lead AI Engineer, Nimbus Analytics (2022 - Present)
- Built a LangGraph agent platform serving 40k requests/day; exposed internal tools as MCP servers.
- Shipped RAG search over 2M documents using FAISS and PostgreSQL with pgvector.
Senior Python Developer, CloudKart (2019 - 2022)
- Designed FastAPI microservices on Docker and Kubernetes (AWS EKS); CI/CD with GitHub Actions.

EDUCATION
B.Tech in Computer Science, Anna University, 2018
"""),
    "arjun_mehta": dict(fmt="docx", text="""Arjun Mehta
arjun.mehta@example.com | +91 99887 66554 | Bengaluru, India

SUMMARY
Backend engineer with 6 years of experience in Python, Django and Flask, recently moving into LLM apps.

SKILLS
Python, Flask, Django, FastAPI, REST APIs, PostgreSQL, MySQL, Redis, Docker, Git, CI/CD, LangChain, Celery

EXPERIENCE
Senior Backend Engineer, PayStream (2021 - Present)
- Built Flask and FastAPI payment services handling 3M transactions/month.
- Prototyped a LangChain support bot for internal FAQs.
Software Engineer, Infobyte Solutions (2018 - 2021)
- Django REST APIs, PostgreSQL tuning, Dockerised deployments with Jenkins CI/CD.

EDUCATION
B.E. in Information Technology, VTU, 2018
"""),
    "sarah_chen": dict(fmt="txt", text="""Sarah Chen
sarah.chen@example.com | +1 415 555 0142 | Remote

SUMMARY
Data scientist with 4 years of experience in machine learning and NLP; built RAG prototypes with LLMs.

SKILLS
Python, Pandas, NumPy, Scikit-learn, PyTorch, Machine Learning, NLP, LLM, RAG, Prompt Engineering,
Chroma, SQL, Jupyter, GCP, Git

EXPERIENCE
Data Scientist, HealthLens (2022 - Present)
- Fine-tuned transformer models with PyTorch; built a RAG assistant over clinical guidelines using Chroma.
Junior Data Scientist, RetailIQ (2020 - 2022)
- Demand forecasting models in scikit-learn; SQL pipelines on BigQuery (GCP).

EDUCATION
M.S. in Data Science, University of Washington, 2020
"""),
    "rahul_verma": dict(fmt="md", text="""# Rahul Verma
rahul.verma@example.com | +91 90000 12345 | Pune, India

## Summary
Java developer with 8 years of experience building enterprise applications with Spring Boot.

## Skills
Java, Spring Boot, Hibernate, Microservices, Oracle, SQL, Maven, Jenkins, Docker, Git, Angular

## Experience
**Technical Lead, FinCore Systems** (2020 - Present)
- Led a team of 8 building Spring Boot microservices on Docker; Jenkins pipelines.

**Software Engineer, TechMahal** (2016 - 2020)
- Java EE applications with Oracle databases.

## Education
B.Tech in Electronics, NIT Trichy, 2016
"""),
    "meera_iyer": dict(fmt="txt", text="""Meera Iyer
meera.iyer@example.com | +91 98765 43210 | Chennai, India

SUMMARY
Frontend developer with 3 years of experience creating responsive web applications.

SKILLS
JavaScript, TypeScript, React, Next.js, HTML, CSS, Tailwind, Figma, Git, Jest

EXPERIENCE
Frontend Developer, PixelCraft Studio (2022 - Present)
- Built React and Next.js dashboards; component library with Storybook.
Intern, WebNova (2021 - 2022)
- Landing pages in HTML, CSS and JavaScript.

EDUCATION
B.Sc. in Computer Science, Madras Christian College, 2021
"""),
    "david_okafor": dict(fmt="pdf", text="""David Okafor
david.okafor@example.com | +44 20 7946 0958 | London, UK

SUMMARY
DevOps and platform engineer with 6 years of experience automating cloud infrastructure with Python.

SKILLS
Python, Bash, Docker, Kubernetes, Terraform, AWS, GCP, CI/CD, GitHub Actions, Prometheus, Grafana,
PostgreSQL, SQL, Git, Linux

EXPERIENCE
Senior Platform Engineer, Streamly (2021 - Present)
- Ran 300+ services on Kubernetes (EKS) with Terraform; Python tooling for deployments.
DevOps Engineer, CloudNine (2019 - 2021)
- Built CI/CD pipelines and Docker images; PostgreSQL backups and monitoring.

EDUCATION
B.Eng. in Computer Engineering, University of Lagos, 2018
"""),
}

INCOMING_DATA = {
    "kavya_nair": """Kavya Nair
kavya.nair@example.com | +91 94440 98765 | Chennai, India

SUMMARY
Generative AI engineer with 5 years of Python experience; builds LangGraph agents and MCP tool servers.

SKILLS
Python, LangGraph, LangChain, LLM, RAG, Model Context Protocol, MCP, FastAPI, Docker, PostgreSQL, SQL,
Pinecone, Prompt Engineering, AWS, Git, CI/CD

EXPERIENCE
GenAI Engineer, TalentWorks AI (2023 - Present)
- Built a resume-screening agent with LangGraph and MCP servers for file and database access.
Python Developer, DataNest (2020 - 2023)
- FastAPI services on Docker; PostgreSQL; GitHub Actions CI/CD on AWS.

EDUCATION
B.Tech in Computer Science, SRM University, 2020
""",
}


def write_pdf(path: Path, text: str) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    y = height - 50
    for line in text.splitlines():
        if y < 50:
            c.showPage()
            y = height - 50
        c.setFont("Helvetica-Bold" if line.isupper() and line.strip() else "Helvetica", 10)
        c.drawString(50, y, line)
        y -= 14
    c.save()


def write_docx(path: Path, text: str) -> None:
    import docx

    d = docx.Document()
    for line in text.splitlines():
        if line.isupper() and line.strip():
            d.add_heading(line.title(), level=2)
        elif line.strip():
            d.add_paragraph(line)
    d.save(str(path))


def main() -> int:
    for d in (RESUMES, JOBS, INCOMING):
        d.mkdir(parents=True, exist_ok=True)
    (JOBS / "senior_python_ai_engineer.md").write_text(JOB, encoding="utf-8")
    for name, spec in RESUMES_DATA.items():
        target = RESUMES / f"{name}.{spec['fmt']}"
        if spec["fmt"] == "pdf":
            write_pdf(target, spec["text"])
        elif spec["fmt"] == "docx":
            write_docx(target, spec["text"])
        else:
            target.write_text(spec["text"], encoding="utf-8")
        print("wrote", target.relative_to(ROOT))
    # A deliberately broken upload, to show per-file error handling.
    (RESUMES / "corrupted_upload.pdf").write_bytes(b"%PDF-1.4\n%garbage - truncated upload\x00\x01\x02")
    print("wrote", (RESUMES / "corrupted_upload.pdf").relative_to(ROOT))
    for name, text in INCOMING_DATA.items():
        (INCOMING / f"{name}.txt").write_text(text, encoding="utf-8")
        print("wrote", (INCOMING / f"{name}.txt").relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
