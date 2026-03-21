# 🤖 pi-automate

An AI-driven automation hub built on **n8n**, designed with an **"Automation-as-Code"** philosophy. This system transforms raw data into structured action by leveraging specialized AI agents (Claude 4.5, Gemini 2.5) and local Edge AI (Whisper).

## 🧠 The AI "Staff" & Their Roles

* **The Tech Visionary (Claude 4.5)**: Audits your tech stack (Kotlin, Android, KMP) to generate daily project ideas.
* **The Market Analyst (Gemini 2.5)**: Monitors your portfolio and uses real-time Google Search to explain market movements.
* **The Strategy Strategist (Claude 4.5 + Whisper)**: Transcribes your voice thoughts and builds technical Action Plans in Notion.
* **The Hidden Gems Guide (Claude 4.5)**: Curates seasonal, non-touristy travel inspirations based on current weather.

---

## ⚙️ Setup & Installation

### 1. Requirements
* Docker & Docker Compose
* A registered Ngrok account (for Webhooks)
* API Keys for Anthropic and Google AI Studio

### 2. Environment Configuration ([Mandatory])
The system **will not start** without a properly configured `.env` file in the root directory. This file stores your credentials and model configurations.

**Step-by-step:**
1. Create the file: `touch .env`
2. Copy the template from the **Environment Variables** section below.
3. Fill in your private keys and IDs.

### 3. Deploy Infrastructure
```bash
docker-compose up -d
