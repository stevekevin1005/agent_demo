# Government Citizen Agent Prototype

Run the backend with no API key:

```powershell
python .\server.py
```

Then open <http://127.0.0.1:8080>.

The default `mock` model is deterministic. To use an OpenAI-compatible model,
set environment variables instead of placing a key in source code:

```powershell
$env:AGENT_MODEL_PROVIDER = "openai-compatible"
$env:MODEL_API_KEY = "<your key>"
$env:MODEL_BASE_URL = "https://api.openai.com/v1"
$env:MODEL_NAME = "gpt-4.1-mini"
python .\server.py
```

For an Azure Foundry Responses API deployment:

```powershell
$env:AGENT_MODEL_PROVIDER = "azure-foundry-responses"
$env:MODEL_ENDPOINT = "https://<resource>.services.ai.azure.com/openai/v1/responses"
$env:MODEL_API_KEY = "<rotated key>"
$env:MODEL_NAME = "<model deployment name>"
python .\server.py
```

`MODEL_NAME` is the deployment name shown in Azure Foundry, not the endpoint
hostname. API keys should be rotated if they are pasted into chat, logs, or
source control.

This prototype also includes `start-azure.ps1`, preconfigured with the current
Foundry endpoint and `gpt-5.6-sol` deployment. After rotating the exposed key:

```powershell
$env:MODEL_API_KEY = "<new key>"
.\start-azure.ps1
```

If `MODEL_API_KEY` is not set, the script prompts for it securely without
echoing the value or writing it to disk.

For a plaintext local-only setup, copy `local-secrets.example.ps1` to
`local-secrets.ps1` and replace the placeholder. The real secrets file is
ignored by `.gitignore` and loaded automatically by `start-azure.ps1`.

The model only maps natural language to allowed workflow actions. Eligibility,
consent, account selection, and submission remain deterministic backend logic.
When the page is opened through the backend, the text box accepts freeform
citizen messages. The model receives only the current workflow state and a
limited catalog of allowed choices, then returns one structured action.

Newborn-related government records are stored in
`data/citizen-newborn-mock.json`. The backend reads them through a
`GovernmentDataAdapter` and returns only the evidence required by the selected
service. `GET /api/mock-government/citizen` exposes the fixture for Demo
inspection.

## HTTP interface

- `POST /api/sessions` starts a conversation.
- `POST /api/chat` sends a message or explicit workflow action.
- `GET /api/sessions/{id}` reads the complete current state.
- `GET /api/health` reports backend and model-provider status.

`POST /api/chat` request:

```json
{
  "sessionId": "...",
  "message": "我想申請育兒生活補助"
}
```

For deterministic UI actions:

```json
{
  "sessionId": "...",
  "action": "select_service",
  "payload": {
    "serviceId": "childcare-benefit-2026"
  }
}
```
